"""Offline interrupted-production tests, using local diagnostic video and no APIs."""
import copy
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pianchang.acceptance import file_sha256
from pianchang.app_service import AppService, read_json, write_json
from pianchang.bailian_video import VideoConfig, build_request
from pianchang.collaboration_workflow import ref
from pianchang.media_tools import find_ffmpeg
from pianchang.story_spec import digest_json
from pianchang.studio_assets import studio_project_for_video
from pianchang.studio_jobs import _session, _source_script, _handoff_spec, _approved_plan, _production_order, studio_video_plan
from pianchang.studio_recovery import (read_incomplete_production, prepare_continuation, load_continuation,
                             run_review_job, incomplete_review_view, confirm_review)
from tests.test_video_collab_agent import OfflineDecisionClient
from trace_system import use_context
from pianchang.video_agent import compile_shot
from pianchang.video_collab_agent import analyze_impact, run_video_agent, VideoWorkspace, _production_impact


class StudioRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.enterContext(patch.dict(os.environ, {"AGENT_TRACE_DIR": str(self.root / "offline-traces")}))
        (self.root / ".env").write_text("# explicit offline test\n", encoding="utf-8")
        self.project = read_json(Path(__file__).resolve().parents[1] / "fixtures" / "story_project.json")
        write_json(self.root / "fixtures/story_project.json", self.project)
        self.service = AppService(self.root)
        self.addCleanup(self.service.close)
        self.enterContext(use_context(None, tracer=self.service.tracer))
        self.enterContext(self.service.tracer.trace("offline.studio_recovery", inputs={"mode": "offline"}))
        self.pid = next(iter(self.service._discover()))
        view = studio_project_for_video(self.service, self.pid)
        self.session = _session(self.service, view["run_id"], self.project["title"], self.pid)
        script = _source_script(self.session, view)
        spec = _handoff_spec(self.session, script, view, {"project_sha256": digest_json(self.project)})
        self.view = studio_project_for_video(self.service, self.pid)
        self.plan = studio_video_plan(self.view, None)
        self.order = self.session.commission(script, spec, None, 3, shooting_plan=self.plan)
        self.session.store.claim("video", message_id=self.order["message_id"])
        self.session.store.ack("video", self.order["message_id"], "unknown", result={"mode": "offline", "reason": "interrupted"})
        self.work = self.session.folder / ("video-spec-" + self.order["message_id"])
        write_json(self.work / "project.json", self.project)
        write_json(self.work / "agent/run.json", {"reserved_shots": ["S001", "S002"], "status": "needs_input"})
        self.job = {"id": "9"*32, "kind": "studio_video", "project_id": self.pid, "status": "failed",
                    "run_id": self.view["run_id"], "story_id": self.view["story_id"], "message_id": self.order["message_id"],
                    "plan_id": self.plan["plan_id"], "trace_id": "8"*32}
        self.service.state["jobs"].append(self.job)
        self.service._save()
        self.diagnostic = self.root / "offline-diagnostic.mp4"
        subprocess.run([str(find_ffmpeg()), "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                        "color=c=blue:s=160x240:r=10:d=0.5", "-c:v", "libx264", str(self.diagnostic)], check=True)
        self.impact = analyze_impact(self.project, None)
        write_json(self.work / "impact.json", self.impact)
        self.original1, self.clip1 = self.make_manifest("S001", "SUCCEEDED")
        self.original2, _ = self.make_manifest("S002", "RUNNING")
        write_json(self.work / "clips.json", {"S001": self.clip1})
        observation = self.root / "outputs/web/progress-observations" / self.job["id"] / "S002" / "offline-S002"
        observation.mkdir(parents=True)
        shutil.copyfile(self.diagnostic, observation / "video.mp4")
        self.receipt = observation / "receipt.json"
        write_json(self.receipt, {"project_id": self.pid, "job_id": self.job["id"], "message_id": self.order["message_id"],
            "plan_id": self.plan["plan_id"], "shot_id": "S002", "task_id": "offline-S002",
            "manifest_sha256": digest_json(read_json(self.original2)), "provider_status": "SUCCEEDED",
            "provider_response": {"output": {"task_id": "offline-S002", "task_status": "SUCCEEDED"}},
            "media_file": "video.mp4", "media_sha256": file_sha256(observation / "video.mp4"),
            "trace_id": "7"*32, "observed_at": "2026-11-08T00:00:00+00:00"})

    def make_manifest(self, sid, status, metadata=None, request=None):
        if metadata is None:
            compiled = compile_shot(self.project, sid)
            metadata = {**compiled["metadata"], "script_asset_ref": self.view["script_ref"],
                "shooting_plan_id": self.plan["plan_id"], "dependency_sha256": self.impact["shots"][sid]["target"]["sha256"]}
            request = build_request(VideoConfig(), compiled["prompt"], duration=compiled["duration"])
        folder = self.root / "outputs/videos" / digest_json([sid, metadata])[:24]
        folder.mkdir(parents=True)
        signature = {"base_url": "offline-fixture", "request": request, "metadata": metadata}
        manifest = {**signature, "request_sha256": digest_json(signature), "status": status, "task_id": "offline-" + sid,
                    "provider_response": {"output": {"task_id": "offline-" + sid, "task_status": status}}}
        if status == "SUCCEEDED":
            shutil.copyfile(self.diagnostic, folder / "video.mp4")
            manifest.update(local_video_path=str(folder / "video.mp4"), download_sha256=file_sha256(folder / "video.mp4"))
        path = folder / "job.json"
        write_json(path, manifest)
        clip = {"shot_id": sid, "operation": "generated", "origin_script_ref": self.view["script_ref"], "script_ref": self.view["script_ref"],
            "task_id": manifest["task_id"], "manifest_path": str(path), "generation_job_sha256": digest_json(manifest),
            "media_path": manifest.get("local_video_path"), "media_sha256": manifest.get("download_sha256"),
            "dependency_sha256": metadata["dependency_sha256"]}
        return path, clip

    def test_interrupted_progress_counts_and_continuation_only_submit_missing_shot(self):
        plan = studio_video_plan(self.view, None)
        self.assertEqual((plan["reuse_shots"], plan["new_shots"]), (["S001", "S002"], ["S003"]))
        original = self.original2.read_bytes()
        checkpoint = prepare_continuation(self.view, plan["continuation"])
        self.assertEqual(self.original2.read_bytes(), original)
        self.assertEqual(read_json(self.original2)["status"], "RUNNING")
        restored = load_continuation(self.root, self.view["run_id"], plan["continuation"], self.order["payload"])
        self.assertEqual(set(restored["shots"]), {"S001", "S002"})
        approved = _approved_plan(self.view, None, {"plan_id": plan["plan_id"]})
        script = self.session.store.get_asset(self.view["script_ref"]["asset_id"])
        spec = self.session.store.get_asset(self.view["spec"]["asset_id"])
        next_order = _production_order(self.session, script, spec, self.view, 1, approved)
        self.assertNotEqual(next_order["message_id"], self.order["message_id"])
        submitted, fixture = [], self
        class OfflineRunner:
            def __init__(self, config): pass
            def submit(self, request, metadata):
                submitted.append(metadata["shot_id"])
                path, _ = fixture.make_manifest(metadata["shot_id"], "SUCCEEDED", metadata, request)
                return {**read_json(path), "manifest_path": str(path)}
            def resume(self, path, wait=True):
                return {**read_json(path), "manifest_path": str(path)}
        with patch("pianchang.video_collab_agent.VideoRunner", OfflineRunner):
            result = run_video_agent(script=self.project["script_source"]["script"], script_ref=self.view["script_ref"],
                directory=self.root / "offline-continuation", text_config=None, video_config=VideoConfig(),
                template_project=self.project, client=OfflineDecisionClient(), shooting_plan=approved,
                partial=checkpoint, max_video_generations=7)
        self.assertFalse(result["loop"]["task_complete"], result["loop"])
        self.assertIsNone(result["production"])
        self.assertEqual(submitted, ["S003"])
        self.assertEqual(set(read_json(self.root / "offline-continuation/reports.json")), {"S001", "S002", "S003"})

    def test_unknown_existing_task_blocks_generation_and_changed_source_rejects_stale_plan(self):
        plan = studio_video_plan(self.view, None)
        self.receipt.unlink()
        blocked = studio_video_plan(self.view, None)
        self.assertEqual(blocked["unresolved_shots"], ["S002"])
        with self.assertRaisesRegex(ValueError, "暂不重复生成"):
            _approved_plan(self.view, None, {"plan_id": blocked["plan_id"]})
        with self.assertRaises(ValueError):
            prepare_continuation(self.view, plan["continuation"])
        with self.assertRaises(ValueError):
            _approved_plan(self.view, None, {"plan_id": plan["plan_id"]})

    def test_reviews_survive_interruption_and_confirmation_is_report_bound(self):
        snapshot = read_incomplete_production(self.view)
        body = {"continuation": {"request_id": snapshot["request_id"], "sha256": snapshot["sha256"]}}
        review_job = copy.deepcopy(self.job)
        review_job.update(id="6"*32, kind="studio_review")
        self.service.vision_config = None
        with patch("pianchang.video_collab_agent.VideoRunner", side_effect=AssertionError("offline: generation prohibited")), \
             patch("pianchang.video_collab_agent.observe", side_effect=AssertionError("offline: no paid model dependency")):
            result = run_review_job(self.service, review_job, body)
            self.assertEqual(set(result["reports"]), {"S001", "S002"})
            with patch("pianchang.video_collab_agent.VideoWorkspace._review", side_effect=AssertionError("completed review must be reused")):
                run_review_job(self.service, review_job, body)
        dto = incomplete_review_view(self.service, self.view, snapshot)
        self.assertEqual(dto["reviewed_count"], 2)
        first = dto["shots"][0]
        original = copy.deepcopy(first["report"])
        from pianchang.preview_access import PreviewBlocked
        with self.assertRaises(PreviewBlocked) as denied:
            confirm_review(self.service, self.pid, {"shot_id": "S001", "report_sha256": first["report_sha256"]})
        self.assertFalse(denied.exception.gate["ready"])
        self.assertEqual(first["report"], original)
        with self.assertRaises(ValueError):
            confirm_review(self.service, self.pid, {"shot_id": "S001", "report_sha256": "0"*64})
        plan = _approved_plan(self.view, None, {"plan_id": studio_video_plan(self.view, None)["plan_id"]})
        checkpoint = prepare_continuation(self.view, plan["continuation"])
        workspace = VideoWorkspace(script=self.project["script_source"]["script"], script_ref=self.view["script_ref"],
            directory=self.work.parent / ("video-spec-" + "5"*32), text_config=None, video_config=VideoConfig(),
            template_project=self.project, previous=None, decision_client=None, vision_config=None, progress=None,
            shooting_plan=plan, partial=checkpoint)
        workspace.project = self.project
        workspace.impact = _production_impact(self.project, None, plan, checkpoint)
        with patch.object(workspace, "_review", side_effect=AssertionError("offline: retained conclusion must not call models again")):
            workspace.reuse("S001")
        self.assertEqual(workspace.reports["S001"]["report_sha256"], first["report_sha256"])


if __name__ == "__main__":
    unittest.main()
