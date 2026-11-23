"""Offline workbench integration with simulated model decisions and preview media."""
import copy
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pianchang.acceptance import file_sha256
from pianchang.app_service import AppService, read_json, write_json
from pianchang.collab_store import CollabStore
from pianchang.script_collab_agent import publish_script_revision
from pianchang.story_spec import digest_json, normalize_script
from pianchang.studio_assets import studio_project_for_video
from tests.test_script_collab_agent import FINISH, OfflineModel, action
from tests.test_story_agent import draft_from_script
from trace_system import use_context


class StudioJobTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {"AGENT_TRACE_DIR": str(self.root / "offline-traces")})
        env.start()
        self.addCleanup(env.stop)
        (self.root / ".env").write_text("# offline", encoding="utf8")
        self.project = read_json(Path(__file__).resolve().parents[1] / "fixtures" / "story_project.json")
        write_json(self.root / "fixtures/story_project.json", self.project)
        # This suite exercises the script-to-assets handoff. Separate asset tests
        # observe media and repairs in an isolated store without provider access.
        asset_worker = patch("pianchang.asset_dispatch.run_asset_order", return_value={"summary":"offline asset worker"})
        asset_worker.start()
        self.addCleanup(asset_worker.stop)
        self.service = AppService(self.root)
        self.addCleanup(self.service.close)
        self.pid = next(iter(self.service._discover()))
        trace_context = use_context(None, tracer=self.service.tracer)
        trace_context.__enter__()
        self.addCleanup(trace_context.__exit__, None, None, None)

    def run_job(self, kind, pid, body, timeout=120):
        jid = self.service.submit(kind, pid, body)["id"]
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            job = next(j for j in self.service.list_jobs() if j["id"] == jid)
            if job["status"] not in {"queued", "running"}:
                if kind == "studio_video":
                    # The single video worker records terminal status before it releases
                    # the Pi session. Finish that release before submitting another plan.
                    self.service.executor.submit(lambda: None).result(timeout=max(0, until - time.monotonic()))
                return job
            time.sleep(.02)
        self.fail("offline test job did not return")

    def test_video_detail_for_first_commission_uses_the_current_submittable_plan(self):
        from pianchang.studio_flow import studio_video_detail
        from pianchang.studio_jobs import studio_video_plan
        with patch("pianchang.studio_flow.studio_asset_catalog", side_effect=AssertionError("full catalog on first open")):
            detail = studio_video_detail(self.service, self.pid)
        self.assertIsNone(detail["video_execution"]["commission_id"])
        self.assertIsNone(detail["video_plan_error"])
        current = studio_video_plan(studio_project_for_video(self.service, self.pid), None)
        self.assertEqual(detail["video_plan"], current)
        self.assertEqual(current["new_shots"], [shot["id"] for shot in self.project["shots"]])

    def test_idea_alone_publishes_unequal_scene_lengths_and_starts_asset_work(self):
        idea = self.project["script_source"]["script"]["idea"]
        draft = draft_from_script(self.project["script_source"]["script"])
        opening = draft["episodes"][0]["scenes"][0]
        handover = {"title": "交接", "beats": opening["beats"][1:]}
        opening["beats"] = opening["beats"][:1]
        draft["episodes"].append({"title": "交还", "scenes": [handover]})
        script = normalize_script(draft, idea)
        compiled = copy.deepcopy(self.project)
        compiled["script_source"] = {"sha256": digest_json(script), "script": script}
        for shot in compiled["shots"][1:]:
            shot.update(episode_id="EP002", script_scene_id="SC002")
        model = OfflineModel([action("write_draft", **draft), action("check_script"), action("publish_script"), FINISH])
        with patch("pianchang.studio_jobs.BailianTextClient", return_value=model), \
             patch("pianchang.story_agent.StoryRunner._call", return_value={"data": compiled}):
            job = self.run_job("studio_script", None, {"idea": idea})
        self.assertEqual(job["status"], "succeeded", job["error"])
        self.assertEqual(job["story_input"], {"idea": idea})
        data = job["result"]
        self.assertTrue(data["ready_for_generation"])
        self.assertEqual(self.service._story_record(job)["inputs"], {"idea": idea})
        from pianchang.studio_flow import studio_flow
        shape = studio_flow(self.service, data["project_id"])["script"]["structure"]
        self.assertEqual([ep["scenes"][0]["beat_count"] for ep in shape["episodes"]], [1, 2])
        self.assertEqual(self.service.project_detail(data["project_id"])["script_structure"], shape)
        self.assertTrue(data["asset_job"]["id"])

    def test_independent_script_published_and_discoverable_without_video_order(self):
        script = self.project["script_source"]["script"]
        model = OfflineModel([action("check_script"), action("publish_script"), FINISH])
        with patch("pianchang.studio_jobs.BailianTextClient", return_value=model), \
             patch("pianchang.story_agent.StoryRunner._call", return_value={"data": self.project}):
            job = self.run_job("studio_script", None, {"script": script})
        self.assertEqual(job["status"], "succeeded", job["error"])
        data = job["result"]
        asset_job = next(j for j in self.service.list_jobs() if j["id"] == data["asset_job"]["id"])
        self.assertEqual((asset_job["kind"], asset_job["project_id"]), ("asset_production", data["project_id"]))
        view = studio_project_for_video(self.service, data["project_id"])
        self.assertEqual(view["script_ref"], data["script_ref"])
        self.assertEqual(view["run_id"], data["run_id"])
        store = CollabStore(view["store_path"])
        self.assertEqual(store.messages(data["run_id"], message_type="production.requested"), [])
        self.assertNotEqual(job["trace_id"], job["script_trace_id"])

    def test_published_script_survives_failed_compile_and_opens_from_sidebar(self):
        script = self.project["script_source"]["script"]
        model = OfflineModel([action("check_script"), action("publish_script"), FINISH])
        with patch("pianchang.studio_jobs.BailianTextClient", return_value=model), \
             patch("pianchang.story_agent.StoryRunner.compile", side_effect=ValueError("offline compile interrupted")):
            job = self.run_job("studio_script", None, {"script": script})
        self.assertEqual(job["status"], "failed")
        self.assertEqual(self.service.story_drafts()[0]["id"], job["id"])
        detail = self.service.story_draft_detail(job["id"])
        self.assertEqual(detail["content_status"], "published_script")
        self.assertEqual(detail["draft"], script)
        self.assertTrue(detail["script_text"])

    def offline_video(self, **inputs):
        project = inputs["template_project"]
        folder = inputs["directory"]
        folder.mkdir(parents=True, exist_ok=True)
        preview = folder / "offline.mp4"
        preview.write_bytes(b"explicit offline stub for transport test only")
        ids = [shot["id"] for shot in project["shots"]]
        production = {"project": project, "script_ref": inputs["script_ref"],
            "shots": {sid: {"operation": "generated", "media_sha256": "offline-" + sid} for sid in ids},
            "reports": {sid: {"unresolved": ["offline"]} for sid in ids},
            "impact": {"regenerate": ids, "reuse": [], "recheck_edges": [],
                       "shots": {sid: {"target": {"source_ids": []}} for sid in ids}},
            "media_accepted": True, "preview": {"preview_path": str(preview), "preview_sha256": file_sha256(preview)}}
        return {"production": production, "loop": {"summary": "offline delivery"}}

    def test_plan_commissions_twelve_then_distinguishes_changes_and_rejects_stale_button(self):
        from pianchang.collaboration_workflow import ref
        from pianchang.studio_flow import studio_flow
        from pianchang.studio_jobs import studio_video_plan

        shots = self.project["shots"]
        for index in range(4, 13):
            shot = copy.deepcopy(shots[-1])
            shot.update(id=f"S{index:03}", continuity_from=[shots[-1]["id"]])
            shots.append(shot)
        write_json(self.root / "fixtures/story_project.json", self.project)
        flow = studio_flow(self.service, self.pid)
        self.assertIsNone(flow["video_plan_error"])
        plan = flow["video_plan"]
        ids = [s["id"] for s in shots]
        self.assertEqual((plan["new_shots"], plan["retake_shots"], plan["reuse_shots"]), (ids, [], []))
        with patch("pianchang.video_collab_agent.run_video_agent", side_effect=self.offline_video) as worker:
            data = self.run_job("studio_video", self.pid,
                                {"plan_id": plan["plan_id"], "project_sha256": plan["project_sha256"]}, timeout=90)
            self.assertEqual(data["status"], "succeeded", data["error"])
            self.assertEqual(worker.call_args.kwargs["max_video_generations"], 24)
            view = studio_project_for_video(self.service, self.pid)
            store = CollabStore(view["store_path"])
            receipt = store.messages(view["run_id"], message_type="production.requested")[0]["payload"]
            self.assertEqual(receipt["shooting_plan"]["plan_id"], plan["plan_id"])
            self.assertEqual(receipt["shooting_plan"]["new_shots"], ids)
            previous = store.get_asset(view["production_ref"]["asset_id"])
            can_reuse = studio_video_plan(view, previous)
            self.assertEqual(can_reuse["reuse_shots"], ids)
            revised = copy.deepcopy(view["project"])
            revised["shots"][-1]["duration_s"] = 6
            store.publish_asset(view["spec"]["asset_id"], revised, kind="production_spec", producer="production",
                                story_id=view["story_id"], parent_revision=view["spec"]["revision_id"])
            current = studio_project_for_video(self.service, self.pid)
            after = studio_video_plan(current, previous)
            self.assertEqual((after["new_shots"], after["retake_shots"], after["reuse_shots"]), ([], ["S012"], ids[:-1]))
            self.assertNotEqual(after["plan_id"], can_reuse["plan_id"])
            stale = self.run_job("studio_video", self.pid,
                                 {"plan_id": can_reuse["plan_id"], "spec_ref": ref(current["spec"])}, timeout=90)
            self.assertEqual(stale["status"], "failed")
            self.assertIn("拍摄计划", stale["error"])
            self.assertEqual(worker.call_count, 1)
            self.assertEqual(len(store.messages(view["run_id"], message_type="production.requested")), 1)

    def test_extra_approval_freezes_only_the_displayed_proposal_in_shot_order(self):
        from pianchang.studio_jobs import _approved_plan
        from pianchang.video_repair import acceptance_policy
        view = {"project": self.project}
        base_id, proposal_id = "a" * 64, "b" * 64
        plan = {"plan_id": base_id, "new_shots": [], "retake_shots": ["S003"],
                "acceptance_loop": acceptance_policy([shot["id"] for shot in self.project["shots"]]),
                "reuse_shots": ["S001", "S002"], "extra_retake": {"proposal_id": proposal_id, "shot_ids": ["S001"]}}
        with patch("pianchang.studio_jobs.studio_video_plan", side_effect=lambda *args: copy.deepcopy(plan)):
            normal = _approved_plan(view, None, {"plan_id": base_id})
            self.assertEqual(normal["retake_shots"], ["S003"])
            accepted = _approved_plan(view, None, {"plan_id": base_id, "proposal_id": proposal_id})
            self.assertEqual((accepted["retake_shots"], accepted["reuse_shots"]), (["S001", "S003"], ["S002"]))
            self.assertNotEqual(accepted["plan_id"], normal["plan_id"])
            with self.assertRaisesRegex(ValueError, "额外重拍计划已更新"):
                _approved_plan(view, None, {"plan_id": base_id, "proposal_id": "d" * 64})
            plan["extra_retake"].update(partial={"request_id": "offline", "sha256": "f" * 64}, reused_shots=["S002", "S003"])
            with self.assertRaisesRegex(ValueError, "请查看并决定"):
                _approved_plan(view, None, {"plan_id": base_id})
            resumed = _approved_plan(view, None, {"plan_id": base_id, "proposal_id": proposal_id})
            self.assertEqual((resumed["new_shots"], resumed["retake_shots"], resumed["reuse_shots"]),
                             ([], ["S001"], ["S002", "S003"]))

    def test_explicit_budget_and_exact_refs_route_to_independent_worker_once(self):
        view = studio_project_for_video(self.service, self.pid)
        sha = view["spec"]["sha256"]
        with self.assertRaisesRegex(ValueError, "授权生成"):
            self.service.submit("studio_video", self.pid, {"project_sha256": sha})
        with patch("pianchang.video_collab_agent.run_video_agent", side_effect=self.offline_video) as executor:
            wrong = self.run_job("studio_video", self.pid, {"project_sha256": "0" * 64, "max_video_generations": 3})
            self.assertEqual(wrong["status"], "failed")
            over_budget = self.run_job("studio_video", self.pid, {"project_sha256": sha, "max_video_generations": 1})
            self.assertEqual(over_budget["status"], "failed")
            self.assertIn("S003", over_budget["error"])
            store = CollabStore(view["store_path"])
            self.assertFalse(store.messages(view["run_id"], message_type="production.requested"))
            job = self.run_job("studio_video", self.pid, {"project_sha256": sha, "max_video_generations": 3})
            self.assertEqual(job["status"], "succeeded", job["error"])
            data = job["result"]
            retry = self.run_job("studio_video", self.pid, {"spec_ref": data["spec_ref"], "max_video_generations": 3})
            self.assertEqual(retry["status"], "succeeded", retry["error"])
            self.assertEqual(retry["result"]["message_id"], data["message_id"])
            self.assertEqual(executor.call_count, 1)
            self.assertEqual(executor.call_args.kwargs["max_video_generations"], 3)
            self.assertEqual(executor.call_args.kwargs["script_ref"], data["script_ref"])
            deps = store.get_asset(data["production_ref"]["asset_id"], data["production_ref"]["revision_id"])["dependencies"]
            self.assertIn({"asset_id": data["spec_ref"]["asset_id"], "revision_id": data["spec_ref"]["revision_id"],
                           "role": "production_input"}, deps)
            self.assertNotEqual(data["video_trace_id"], job["trace_id"])
            self.assertTrue(data["media_accepted"])
            revised = copy.deepcopy(self.project["script_source"]["script"])
            revised["script_version"] += 1
            revised["episodes"][0]["scenes"][0]["beats"][-1]["text"] += " 离线测试修改。"
            base = data["script_ref"]
            publish_script_revision(store, revised, {"status": "PASS", "script_sha256": digest_json(revised),
                "base_revision": base["revision_id"]}, asset_id=base["asset_id"], story_id=data["story_id"],
                base_revision=base["revision_id"], source_sha256=base["sha256"], correlation_id=data["run_id"])
            view = studio_project_for_video(self.service, self.pid)
            self.assertEqual(view["pending_script_ref"]["revision_id"], "r2")
            old = self.run_job("studio_video", self.pid, {"script_ref": base, "spec_ref": data["spec_ref"],
                                                         "max_video_generations": 3})
            self.assertEqual(old["status"], "failed")
            self.assertIn("r2 已发布", old["error"])
            self.assertEqual(len(store.messages(data["run_id"], message_type="production.requested")), 1)
