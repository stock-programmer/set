"""Offline cooperation tests: scripted decisions and locally generated diagnostic media."""
import copy
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pianchang.acceptance import file_sha256
from pianchang.bailian_text import TextConfig, TextError
from pianchang.bailian_video import VideoConfig
from pianchang.media_tools import MediaError, find_ffmpeg, find_ffprobe
from pianchang.story_spec import digest_json
from trace_system import Tracer, use_context
from pianchang.video_collab_agent import (adapt_project, analyze_impact, recover_preview, run_video_agent, _one_model_call,
                               _production_impact, _validated_media)


class OfflineDecisionClient:
    agent_backend="offline_legacy"
    def __init__(self):
        self.snapshots = []
        self.actions = []

    def complete_json(self, messages):
        if "视频制作规格编辑" in messages[0]["content"]:
            data = json.loads(messages[1]["content"])
            updates = [{"group": "shots", "id": sid, "values": {"description": obj["description"] + " 他停步回望。"}}
                       for sid, obj in data["affected_objects"]["shots"].items()]
            return {"data": {"edits": updates}, "model": "offline-fixture"}
        states = [json.loads(m["content"]) for m in messages if m["role"] == "user"]
        state = [entry["state"] for entry in states if entry.get("type") == "state_snapshot"][-1]
        self.snapshots.append(state)
        args = {}
        if not state["project_ready"]:
            tool = "plan_production"
        elif state["generate_needed"]:
            tool, args = "generate_shot", {"shot_id": state["generate_needed"][0]}
        elif state["reuse_needed"]:
            tool, args = "reuse_shot", {"shot_id": state["reuse_needed"][0]}
        elif not state["review_and_export_done"] and any(entry.get("tool") == "review_and_export" for entry in states if entry.get("type") == "tool_result"):
            return {"data": {"type": "finish", "outcome": "needs_input", "summary": "offline fixture has no semantic repair model; acceptance remains incomplete"}}
        elif not state["review_and_export_done"]:
            tool = "review_and_export"
        else:
            return {"data": {"type": "finish", "outcome": "completed", "summary": "offline preview"}}
        self.actions.append((tool, args))
        return {"data": {"type": "tool", "tool": tool, "arguments": args, "summary": "offline test action"}}


class CollabVideoTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.scope = use_context(None, tracer=Tracer(self.root / "traces"))
        self.scope.__enter__()
        self.addCleanup(self.scope.__exit__, None, None, None)
        self.project = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "story_project.json").read_text(encoding="utf-8"))
        self.script = self.project["script_source"]["script"]
        self.changed = copy.deepcopy(self.script)
        self.changed["script_version"] = 2
        self.changed["episodes"][0]["scenes"][0]["beats"][-1]["text"] += " 他停步回望。"

    def test_changed_beat_has_local_regeneration_and_adjacent_review(self):
        new = adapt_project(self.project, self.changed, OfflineDecisionClient())
        plan = analyze_impact(new, self.project)
        self.assertEqual(plan["regenerate"], ["S003"])
        self.assertEqual(plan["reuse"], ["S001", "S002"])
        self.assertEqual(plan["changed_sources"], ["B003"])
        self.assertIn(["S002", "S003"], plan["recheck_edges"])
        self.assertIn("B003", plan["shots"]["S003"]["changed_sources"])
        self.assertEqual(self.script["script_version"], 1)

    def test_changed_entity_invalidates_every_consuming_shot(self):
        project = copy.deepcopy(self.project)
        project["entities"]["prop:key"]["canonical"]["appearance"] = "一把带有鲜明红色挂绳的铜钥匙。"
        plan = analyze_impact(project, self.project)
        self.assertEqual(plan["regenerate"], ["S001", "S002", "S003"])
        self.assertEqual(plan["changed_sources"], [])
        self.assertIn("entities", plan["shots"]["S003"]["changed_components"])

    def test_existing_only_repair_preserves_project_and_defers_missing_media(self):
        partial={"project":self.project,"shots":{"S001":{}},"missing_shots":["S002","S003"]}
        plan={"plan_id":"offline-existing-only","new_shots":[],"retake_shots":[],"reuse_shots":["S001"],
              "continuation":{"request_id":"offline"},"repair_existing_only":True,"deferred_shots":["S002","S003"]}
        impact=_production_impact(self.project,None,plan,partial)
        self.assertEqual(impact["regenerate"],[])
        self.assertEqual(impact["reuse"],["S001"])
        self.assertEqual(len(self.project["shots"]),3)

    def test_uncertain_model_request_is_not_replayed(self):
        path, calls = self.root / "offline-call.json", []

        def interrupted():
            calls.append(1)
            raise TextError("offline simulated interruption")

        with self.assertRaises(TextError):
            _one_model_call(path, {"fixture": True}, interrupted)
        with self.assertRaisesRegex(ValueError, "未确认"):
            _one_model_call(path, {"fixture": True}, interrupted)
        self.assertEqual(calls, [1])

    def test_only_an_approved_failed_check_can_override_an_unchanged_shot(self):
        previous = {"project": self.project, "reports": {"S002": {"status": "FAIL"}}}
        plan = {"plan_id": "offline-extra", "new_shots": [], "retake_shots": ["S002"],
                "reuse_shots": ["S001", "S003"], "approval_proposal": {"shot_ids": ["S002"]}}
        impact = _production_impact(self.project, previous, plan)
        self.assertEqual(impact["regenerate"], ["S002"])
        self.assertEqual(impact["reuse"], ["S001", "S003"])
        self.assertEqual(impact["recheck_edges"], [["S001", "S002"], ["S002", "S003"]])
        previous["reports"]["S002"]["status"] = "UNKNOWN"
        with self.assertRaisesRegex(ValueError, "失败"):
            _production_impact(self.project, previous, plan)

    def test_terminal_failure_retakes_only_failed_shots_uncertain_result_never_offers_retry(self):
        calls = []
        root = self.root
        terminal = {"value": "FAILED"}
        class OfflineRunner:
            def __init__(self, config):
                pass
            def submit(self, request, metadata):
                sid = metadata["shot_id"]
                calls.append(sid)
                folder = root / metadata["shooting_plan_id"] / sid
                folder.mkdir(parents=True)
                job = {"base_url": "offline", "request": request, "metadata": metadata}
                job.update(request_sha256=digest_json(job), task_id="offline-" + sid, status="PENDING")
                path = folder / "job.json"
                path.write_text(json.dumps(job), encoding="utf8")
                return dict(job, manifest_path=str(path))
            def resume(self, manifest_path, wait=True):
                path = Path(manifest_path)
                job = json.loads(path.read_text())
                if job["metadata"]["shot_id"] == "S001":
                    media = path.with_name("video.mp4")
                    media.write_bytes(b"offline clip for checkpoint hash only")
                    job.update(status="SUCCEEDED", download_sha256=file_sha256(media), local_video_path=str(media))
                    path.write_text(json.dumps(job))
                    return dict(job, manifest_path=str(path))
                job["status"] = terminal["value"]
                job["provider_response"] = {"output": {"task_status": job["status"], "message": "offline simulated failure"}}
                path.write_text(json.dumps(job))
                raise ValueError("offline simulated provider response")

        first = {"plan_id":"first", "new_shots":["S001","S002","S003"], "retake_shots":[], "reuse_shots":[]}
        common = dict(script=self.script, script_ref={"asset_id":"offline", "revision_id":"r1"}, text_config=None,
                      template_project=self.project, video_config=VideoConfig(), client=OfflineDecisionClient())
        def run(plan, partial=None):
            return run_video_agent(**common, shooting_plan=plan, partial=partial,
                inbound_message={"message_id":plan["plan_id"]}, directory=self.root / ("run-" + plan["plan_id"]),
                max_video_generations=len(plan["retake_shots"] + plan["new_shots"]))
        with patch("pianchang.video_collab_agent.VideoRunner", OfflineRunner), \
             patch("pianchang.video_collab_agent.VideoWorkspace.review_shot", return_value={"mode": "offline hash-only checkpoint fixture"}):
            result = run(first)
            self.assertEqual(result["loop"]["reason"], "retake_approval_required", result["loop"])
            checkpoint = result["partial"]
            self.assertEqual(set(checkpoint["failed_shots"]), {"S002","S003"})
            self.assertEqual(list(checkpoint["shots"]), ["S001"])
            retry = {"plan_id":"retry", "new_shots":[], "retake_shots":["S002","S003"], "reuse_shots":["S001"],
                     "partial":{"request_id":"first", "sha256":digest_json(checkpoint)}}
            second = run(retry, partial=checkpoint)
            self.assertEqual(second["partial"]["shots"]["S001"]["media_sha256"], checkpoint["shots"]["S001"]["media_sha256"])
            self.assertEqual(calls, ["S001","S002","S003","S002","S003"])
            terminal["value"] = "UNKNOWN"
            unknown = run({**first, "plan_id":"unknown"})
            self.assertEqual(unknown["loop"]["reason"], "video_result_uncertain")
            self.assertIsNone(unknown["partial"])

    def test_offline_model_r1_and_r2_media_provenance(self):
        try:
            ffmpeg = find_ffmpeg()
            find_ffprobe()
        except MediaError:
            self.skipTest("FFmpeg unavailable for the offline diagnostic-media fixture")
        diagnostic = self.root / "offline-diagnostic.mp4"
        subprocess.run([str(ffmpeg), "-nostdin", "-hide_banner", "-v", "error", "-f", "lavfi", "-i",
                        "color=c=blue:s=160x240:r=10:d=0.5", "-c:v", "libx264", str(diagnostic)], check=True)
        submitted = []
        root = self.root

        class OfflineRunner:
            def __init__(self, config):
                self.config = config

            def submit(self, request, metadata):
                sid = metadata["shot_id"]
                version = metadata["script_asset_ref"]["revision_id"]
                submitted.append((version, sid))
                folder = root / "offline-jobs" / version / sid
                folder.mkdir(parents=True)
                video = folder / "video.mp4"
                shutil.copyfile(diagnostic, video)
                job = {"base_url": "offline-fixture", "request": request, "metadata": metadata}
                job.update(request_sha256=digest_json(job), task_id="offline-" + version + sid, status="SUCCEEDED",
                           local_video_path=str(video), download_sha256=file_sha256(video), schema_version="bailian-video-job/1")
                job["video_url"] = "https://offline.example.invalid/media?Signature=offline-credential-for-redaction-test"
                manifest = folder / "job.json"
                manifest.write_text(json.dumps(job), encoding="utf-8")
                return {**job, "manifest_path": str(manifest)}

            def resume(self, path, wait=True):
                return {**json.loads(Path(path).read_text(encoding="utf-8")), "manifest_path": path}

        with patch("pianchang.video_collab_agent.VideoRunner", OfflineRunner):
            common = {"text_config": None, "video_config": VideoConfig(output_dir=self.root / "offline-jobs"),
                      "inbound_message": {"id": "offline-request", "scope": "preview"}}
            first_client = OfflineDecisionClient()
            first = run_video_agent(script=self.script, script_ref={"asset_id": "story", "revision_id": "r1"},
                 template_project=self.project, directory=self.root / "r1", client=first_client,
                 shooting_plan={"plan_id": "offline-first", "new_shots": ["S001", "S002", "S003"],
                                "retake_shots": [], "reuse_shots": []}, **common)
            self.assertFalse(first["loop"]["task_complete"], first["loop"])
            self.assertIsNone(first["production"])
            self.assertEqual(first_client.snapshots[0]["received_production_request"]["id"], "offline-request")
            source = self.root / "r1"
            previous = {"project": self.project, "script_ref": {"asset_id":"story", "revision_id":"r1"},
                        "shots": json.loads((source / "clips.json").read_text()),
                        "reports": json.loads((source / "reports.json").read_text())}
            with patch("pianchang.video_collab_agent.observe", side_effect=TextError("offline observer unavailable")) as observer:
                second = run_video_agent(script=self.changed, script_ref={"asset_id":"story", "revision_id":"r2"},
                     previous=previous, directory=self.root / "r2", client=OfflineDecisionClient(),
                     vision_config=TextConfig(), **common)
                self.assertEqual(observer.call_count, 3)
        self.assertFalse(second["loop"]["task_complete"], second["loop"])
        self.assertIsNone(second["production"])
        self.assertEqual(submitted, [("r1", "S001"), ("r1", "S002"), ("r1", "S003"), ("r2", "S003")])
        clips = json.loads((self.root / "r2/clips.json").read_text())
        self.assertEqual(clips["S002"]["manifest_path"], previous["shots"]["S002"]["manifest_path"])
        self.assertEqual(clips["S002"]["operation"], "reused")
        self.assertFalse((self.root / "r1/preview/export.json").exists())
        self.assertFalse((self.root / "r2/preview/export.json").exists())
        self.assertEqual(previous["shots"]["S002"]["script_ref"]["revision_id"], "r1")
        Path(clips["S002"]["media_path"]).write_bytes(b"offline deliberate tamper")
        with self.assertRaisesRegex(ValueError, "不一致"):
            _validated_media(clips["S002"], require_origin=True)



if __name__ == "__main__":
    unittest.main()
