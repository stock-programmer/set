"""Offline interrupted-production tests, using local diagnostic video and no APIs."""
import copy
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pianchang.acceptance import file_sha256
from pianchang.app_service import AppService, read_json, write_json
from pianchang.bailian_text import TextConfig
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


class ReuseReviewStartupTests(unittest.TestCase):
    def test_startup_reuses_complete_failed_reports_and_rechecks_changed_inputs(self):
        from pianchang.content_review import NORMALIZATION_VERSION, STATE_AUDIT_VERSION, VERSION
        from pianchang.studio_recovery import _has_current_reuse_reviews
        with tempfile.TemporaryDirectory() as temp:
            ids = ["S001", "S002"]
            project = {"shots": [{"id": sid} for sid in ids]}
            cid = "c" * 32
            view = {"project": project, "run_id": "run", "store_path": str(Path(temp) / "assets.sqlite3")}
            work = Path(temp) / "runs" / "run" / ("video-spec-" + cid)
            media = {"S001": "a" * 64, "S002": "b" * 64}
            snapshot = {"request_id": cid, "available": {s: {"clip": {"media_sha256": sha}} for s, sha in media.items()}}
            plan = {"continuation": {"request_id": "old"}, "new_shots": [], "retake_shots": [], "reuse_shots": ids}
            records = {}
            for sid, status in zip(ids, ("UNKNOWN", "FAIL")):
                folder = work / sid
                req = {"criterion_id": "action_sequence", "kind": "continuous_video", "required": True}
                contract = {"shot_id": sid, "requirements": [req]}
                receipt = {"version": VERSION, "normalization_version": NORMALIZATION_VERSION}
                content = {"selected_media": {s: {"sha256": h} for s, h in media.items()},
                           "receipts": [{"path": str(folder / "receipt.json")}], "rows": ["action_sequence", "continuity:S001->S002"]}
                inspection = {"media_sha256": media[sid]}
                report = {"shot_id": sid, "media_sha256": media[sid], "project_sha256": digest_json(project),
                          "content_review_version": VERSION, "state_audit_version": STATE_AUDIT_VERSION,
                          "inspection_sha256": digest_json(inspection), "contract_sha256": digest_json(contract),
                          "content_evidence_sha256": digest_json(content),
                          "results": [{"criterion_id": rid, "required": True, "status": status} for rid in content["rows"]]}
                for name, data in (("contract", contract), ("content", content), ("inspection", inspection),
                                   ("receipt", receipt), ("report", report)):
                    write_json(folder / (name + ".json"), data)
                records[sid] = {"report_path": str(folder / "report.json"), "report_sha256": digest_json(report),
                                "inspection_path": str(folder / "inspection.json")}
            write_json(work / "reports.json", records)
            with patch("pianchang.video_collab_agent._validated_media", return_value={}), \
                 patch("pianchang.acceptance.verify_inspection"), patch("pianchang.acceptance.create_contract", return_value=contract), \
                 patch("pianchang.content_review.validate_content_evidence", side_effect=lambda p, c, i, e: dict.fromkeys(e["rows"])):
                self.assertTrue(_has_current_reuse_reviews(view, snapshot, plan))
                snapshot["available"]["S002"]["clip"]["media_sha256"] = "changed"
                self.assertFalse(_has_current_reuse_reviews(view, snapshot, plan))
                snapshot["available"]["S002"]["clip"]["media_sha256"] = media["S002"]
                report["results"].pop()
                write_json(folder / "report.json", report)
                records[sid]["report_sha256"] = digest_json(report)
                write_json(work / "reports.json", records)
                self.assertFalse(_has_current_reuse_reviews(view, snapshot, plan))
                write_json(work / "reports.json", {})
                self.assertFalse(_has_current_reuse_reviews(view, snapshot, plan))


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
                directory=self.root / "offline-continuation", text_config=None, video_config=self.service.video_config,
                template_project=self.project, client=OfflineDecisionClient(), shooting_plan=approved,
                partial=checkpoint, max_video_generations=4)
        self.assertFalse(result["loop"]["task_complete"], result["loop"])
        self.assertIsNone(result["production"])
        self.assertEqual(submitted, ["S003"])
        self.assertEqual(set(read_json(self.root / "offline-continuation/reports.json")), {"S001", "S002", "S003"})

    def test_continuation_inherits_a_retake_already_used_on_one_shot(self):
        from pianchang.studio_recovery import load_continuation
        before = read_incomplete_production(self.view)
        pointer = {"request_id": before["request_id"], "sha256": before["sha256"]}
        prepare_continuation(self.view, pointer)
        run = read_json(self.work / "agent/run.json")
        run["repair_reservations"] = {"S001": 1}
        write_json(self.work / "agent/run.json", run)
        inherited = load_continuation(self.root, self.view["run_id"], pointer, self.order["payload"])
        self.assertEqual(inherited["previously_retried_shots"], ["S001"])
        plan = studio_video_plan(self.view, None)
        self.assertEqual(plan["acceptance_loop"]["previously_retried_shots"], ["S001"])
        self.assertEqual(plan["execution_limits"]["max_video_generations"], 3)  # S003 first take plus S002/S003 retakes
        approved = _approved_plan(self.view, None, {"plan_id": plan["plan_id"], "repair_existing_only": True})
        self.assertEqual(approved["execution_limits"]["max_video_generations"], 1)
        self.assertEqual(approved["acceptance_loop"]["previously_retried_shots"], ["S001"])

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

    def test_all_reused_clips_are_bound_before_pi_decision_even_after_legacy_interruption(self):
        _, clip3 = self.make_manifest("S003", "SUCCEEDED")
        write_json(self.work / "clips.json", {"S001": self.clip1, "S003": clip3})
        plan = studio_video_plan(self.view, None)
        checkpoint = prepare_continuation(self.view, plan["continuation"])
        shots = ["S001", "S002", "S003"]
        self.assertEqual(plan["reuse_shots"], shots)
        self.assertEqual(plan["new_shots"] + plan["retake_shots"], [])
        historical = VideoWorkspace(script=self.project["script_source"]["script"], script_ref=self.view["script_ref"],
            directory=self.work, text_config=None, video_config=self.service.video_config, template_project=self.project,
            previous=None, decision_client=None, vision_config=None, progress=None)
        historical.project, historical.impact, historical.shots = self.project, self.impact, checkpoint["shots"]
        old_report = historical.review_shot("S001")
        self.assertFalse(old_report["media_accepted"])

        current = self.work.parent / ("video-spec-" + "5"*32)
        decisions = []
        def reject_decision(payload):
            clips_path = current / "clips.json"
            decisions.append(sorted(read_json(clips_path)) if clips_path.exists() else [])
            raise ValueError("offline decision provider: HTTP 400 Arrearage")
        client = SimpleNamespace(config=TextConfig(api_key="offline-model-key"), pi_exchange=reject_decision)
        negotiation = SimpleNamespace(tools=lambda: [], store=self.session.store, production_message_id="5"*32)
        with patch("pianchang.video_autonomy.VideoAutonomy.request_clarifications") as clarifications, \
             patch("pianchang.video_collab_agent.VideoRunner") as generator, \
             patch("pianchang.video_collab_agent.VideoWorkspace._review") as new_review, \
             patch("pianchang.content_review.run_content_reviews") as content_review:
            for resume in (False, True):
                if resume:
                    # Older versions saved the failed first Pi decision, before binding any clips.
                    for name in ("project.json", "impact.json", "clips.json", "reports.json"):
                        (current / name).unlink()
                result = run_video_agent(script=historical.script, script_ref=self.view["script_ref"], directory=current,
                    text_config=client.config, video_config=self.service.video_config, template_project=self.project,
                    client=client, shooting_plan=plan, partial=checkpoint, negotiation=negotiation,
                    max_video_generations=6, resume=resume)
                self.assertIn("Arrearage", result["loop"]["summary"])
                self.assertIsNone(result["production"])
                self.assertFalse(result["loop"]["task_complete"])
                self.assertEqual(result["loop"]["video_generations"], 0)
                bound = read_json(current / "clips.json")
                self.assertEqual(set(bound), set(shots))
                for sid, clip in bound.items():
                    self.assertEqual(clip["operation"], "reused")
                    self.assertEqual(clip["task_id"], checkpoint["shots"][sid]["task_id"])
                    self.assertEqual(clip["media_sha256"], checkpoint["shots"][sid]["media_sha256"])
                self.assertEqual(read_json(current / "reports.json"), {"S001": old_report})
        self.assertEqual(decisions, [shots, shots])
        clarifications.assert_not_called()
        generator.assert_not_called()
        new_review.assert_not_called()
        content_review.assert_not_called()

    def test_blocked_pi_decision_needs_explicit_continuation_and_no_pending_tool(self):
        from pianchang.collaboration_workflow import VideoAgentStopped

        _, clip3 = self.make_manifest("S003", "SUCCEEDED")
        write_json(self.work / "clips.json", {"S001": self.clip1, "S003": clip3})
        plan = studio_video_plan(self.view, None)
        prepare_continuation(self.view, plan["continuation"])
        script = self.session.store.get_asset(self.view["script_ref"]["asset_id"])
        spec = self.session.store.get_asset(self.view["spec"]["asset_id"])
        order = self.session.commission(script, spec, None, 6, shooting_plan=plan)
        cid = order["message_id"]
        self.session.store.claim("video", message_id=cid)
        self.session.store.ack("video", cid, "blocked", result={"reason":"offline Pi decision rejected"})
        record = {"status":"needs_input", "result":{"reason":"pi_stopped"}, "steps":1, "limits":{"max_steps":56},
                  "shooting_plan":plan, "messages":[], "video_generations":0, "reserved_shots":[], "repair_reservations":{}}
        path = self.session.folder / ("video-spec-" + cid) / "agent/run.json"
        write_json(path, record)
        with patch("pianchang.video_collab_agent.run_video_agent",
                   return_value={"production":None, "loop":{"summary":"offline decision rejected"}}) as run:
            with self.assertRaisesRegex(ValueError, "须核实原任务"):
                self.session.worker(cid, resume=True)
            write_json(path, {**record, "pending_tool":{"name":"regenerate_shot"}})
            with self.assertRaisesRegex(ValueError, "须核实原任务"):
                self.session.worker(cid, resume=True, explicit_continuation=True)
            run.assert_not_called()
            write_json(path, record)
            # The fake worker stands in for the already-tested Pi loop.
            with self.assertRaises(VideoAgentStopped):
                self.session.worker(cid, resume=True, explicit_continuation=True)
            self.assertTrue(run.call_args.kwargs["resume"])
            self.assertEqual(run.call_args.kwargs["max_video_generations"], 6)
            self.assertEqual(run.call_args.kwargs["shooting_plan"], plan)

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
            directory=self.work.parent / ("video-spec-" + "5"*32), text_config=None, video_config=self.service.video_config,
            template_project=self.project, previous=None, decision_client=None, vision_config=None, progress=None,
            shooting_plan=plan, partial=checkpoint)
        workspace.project = self.project
        workspace.impact = _production_impact(self.project, None, plan, checkpoint)
        with patch.object(workspace, "_review", side_effect=AssertionError("offline: retained conclusion must not call models again")):
            workspace.reuse("S001")
        self.assertEqual(workspace.reports["S001"]["report_sha256"], first["report_sha256"])


if __name__ == "__main__":
    unittest.main()
