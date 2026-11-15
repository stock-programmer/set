"""Real durable work orders and immutable approvals with simulated media observations."""
import base64
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from pianchang.app_service import AppService, project_id_for_path, read_json, write_json
from pianchang.asset_dispatch import (approve_asset, asset_work_state, check_reference_approval, check_video_asset_readiness,
                            enqueue_library_assets)
from pianchang.story_spec import digest_json
from pianchang.studio_assets import studio_asset_catalog, studio_project_for_video, upload_studio_reference
from tests.test_reference_assets import offline_png
from trace_system import use_context


class AssetDispatchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        env = patch.dict(os.environ, {"AGENT_TRACE_DIR": str(root / "offline-trace")})
        env.start()
        self.addCleanup(env.stop)
        (root / ".env").write_text("# offline", encoding="utf8")
        self.project = read_json(Path(__file__).resolve().parents[1] / "fixtures" / "story_project.json")
        write_json(root / "fixtures" / "story_project.json", self.project)
        self.service = AppService(root)
        self.addCleanup(self.service.close)
        self.pid = next(iter(self.service._discover()))
        tracer = use_context(None, tracer=self.service.tracer)
        tracer.__enter__()
        self.addCleanup(tracer.__exit__, None, None, None)

    def upload(self):
        data = base64.b64encode(offline_png((1,2,3))).decode()
        return upload_studio_reference(self.service, self.pid, {"subject_id":"char:lin", "purpose":"portrait",
                                       "data_uri":"data:image/png;base64,"+data})["pin"]

    def wait_job(self, kind, body, key=None):
        job = self.service.submit(kind,self.pid,body,key)
        for _ in range(200):
            current = next(j for j in self.service.list_jobs() if j["id"] == job["id"])
            if current["status"] not in {"queued","running"}:
                return current
            time.sleep(.1)
        self.fail("Offline work order still running")

    @staticmethod
    def pass_observation(service, brief, criteria, files):
        return ([{"criterion_id":k,"verdict":"PASS","reason":"offline visual evidence","frame_ids":["candidate"]}
                 for k in criteria], {"offline": True})

    def historical_verified_item(self):
        """Persist the same pending snapshot a previous release wrote after self-check."""
        from pianchang.asset_review import review_studio_candidate
        pin = self.upload()
        style = "离线制作风格"
        with patch("pianchang.asset_review._observe_images", side_effect=self.pass_observation):
            report = review_studio_candidate(self.service, self.pid, pin, {"style": style})
        self.assertEqual(report["status"], "PASS")
        state = asset_work_state(self.service, self.pid)
        item = next(i for i in state["items"] if (i["subject_id"], i["purpose"]) == (pin["subject_id"], pin["purpose"]))
        item.update(status="awaiting_review", candidate=pin, style_sha256=digest_json(style),
                    review=report, trace_id=report["trace_id"])
        self.service.state["asset_work"] = {self.pid: {"basis": state["basis"], "style": style,
            "items": {digest_json([pin["subject_id"], pin["purpose"]]): item}, "production_started": True}}
        self.service._save()
        return pin, item

    def test_startup_selects_old_pass_and_binds_exact_pin_once_without_provider_or_key(self):
        pin, item = self.historical_verified_item()
        self.service.video_config = replace(self.service.video_config, api_key="")
        with patch.object(self.service, "submit") as submit, patch("pianchang.asset_review._observe_images") as observe:
            enqueue_library_assets(self.service)
            selected = studio_project_for_video(self.service, self.pid)
            bound = [s["id"] for s in selected["project"]["shots"] if pin in s.get("required_refs", [])]
            self.assertEqual(bound, [s["id"] for s in self.project["shots"] if "char:lin" in s["entity_ids"]])
            self.assertTrue(bound)
            self.assertEqual(selected["spec"]["revision_id"], "r1")
            check_reference_approval(self.service, self.pid, pin)
            active = next(i for i in asset_work_state(self.service, self.pid)["items"] if i["candidate"] == pin)
            self.assertEqual(active["approval"]["review_ref"], item["review"]["review_ref"])
            enqueue_library_assets(self.service)
            self.assertEqual(studio_project_for_video(self.service, self.pid)["spec"], selected["spec"])
            submit.assert_not_called()
            observe.assert_not_called()

    def test_damaged_legacy_review_does_not_bind_or_block_startup(self):
        pin, item = self.historical_verified_item()
        item["review"]["review_ref"] = {**item["review"]["review_ref"], "sha256": "0" * 64}
        self.service.video_config = replace(self.service.video_config, api_key="")
        with patch.object(self.service, "submit") as submit:
            enqueue_library_assets(self.service)
            enqueue_library_assets(self.service)
            submit.assert_not_called()
        failed = next(i for i in asset_work_state(self.service, self.pid)["items"] if i["candidate"] == pin)
        self.assertEqual(failed["status"], "needs_attention")
        self.assertTrue(failed["error"])
        self.assertFalse(any(pin in s.get("required_refs", []) for s in studio_project_for_video(self.service, self.pid)["project"]["shots"]))

    def test_workbench_and_other_stories_are_available_during_migration_probe(self):
        from pianchang.asset_dispatch import reconcile_asset_selections
        from pianchang.collab_store import CollabStore
        from pianchang.reference_assets import _media_info
        from pianchang.studio_assets import studio_reference_media
        pin, item = self.historical_verified_item()
        entered, release = threading.Event(), threading.Event()

        def pause_probe(*args):
            entered.set()
            if not release.wait(45):
                raise AssertionError("offline probe was not released")
            return _media_info(*args)

        def migrate():
            with use_context(None, tracer=self.service.tracer):
                return reconcile_asset_selections(self.service, self.pid, [item])

        def browse_and_publish():
            with use_context(None, tracer=self.service.tracer):
                projects = self.service.bootstrap()["projects"]
                work = asset_work_state(self.service, self.pid)
                media, _ = studio_reference_media(self.service, self.pid, pin["asset_id"], pin["revision_id"])
                store = CollabStore(studio_project_for_video(self.service, self.pid)["store_path"])
                store.publish_asset("other-story/script", {"offline":True}, kind="script", producer="scripts",
                                    story_id="other-story")
                return projects, work, media

        with patch("pianchang.reference_assets._media_info", side_effect=pause_probe), ThreadPoolExecutor(2) as workers:
            adoption = workers.submit(migrate)
            try:
                self.assertTrue(entered.wait(20), "offline migration did not reach probe")
                projects, work, media = workers.submit(browse_and_publish).result(timeout=10)
                self.assertIn(self.pid, [p["id"] for p in projects])
                self.assertEqual(work["counts"]["awaiting_review"], 1)
                self.assertTrue(media.is_file())
            finally:
                release.set()
            self.assertEqual(adoption.result(timeout=30)[0]["status"], "selected")

    def test_library_startup_queues_compiled_stories_once_without_http_side_effects(self):
        path = self.service.state_dir / "stories" / "offline-second" / "project.json"
        write_json(path, self.project)
        broken = self.service.state_dir / "stories" / "offline-broken" / "project.json"
        write_json(broken, {"entities": {}, "shots": []})
        self.assertFalse(asset_work_state(self.service, self.pid)["jobs"])
        self.service.video_config = replace(self.service.video_config, api_key="offline-test-key")
        with patch.object(self.service.library_asset_executor, "submit") as worker:
            first = enqueue_library_assets(self.service)
            again = enqueue_library_assets(self.service)
        self.assertEqual(worker.call_count, 2)
        self.assertEqual([d["status"] for d in first.values()], ["queued", "blocked", "queued"])
        self.assertEqual(first[self.pid]["job_id"], again[self.pid]["job_id"])
        self.assertEqual(len(self.service.list_jobs()), 2)
        self.assertEqual(asset_work_state(self.service, self.pid)["auto_dispatch"], again[self.pid])

    def test_published_intent_is_recovered_only_when_production_server_starts(self):
        state_dir = self.service.root / "offline-second-workbench"
        source = state_dir / "stories" / "offline-published" / "project.json"
        write_json(source, self.project)
        pid = project_id_for_path(source)
        key = "assets-offline-published"
        write_json(source.with_name("asset-intent.json"), {
            "project_id": pid, "key": key, "body": {"style": "离线风格"}, "context": None})
        with patch("pianchang.app_service.ThreadPoolExecutor.submit") as worker:
            service = AppService(self.service.root, state_dir)
            self.addCleanup(service.close)
            self.assertFalse(service.list_jobs())
            self.assertFalse(asset_work_state(service, pid)["jobs"])
            worker.assert_not_called()
            result = enqueue_library_assets(service)[pid]
            self.assertEqual(result["status"], "queued")
            self.assertEqual(worker.call_count, 1)
            saved_jobs = [j for j in service.state["jobs"] if j["project_id"] == pid]
            self.assertEqual([j["_idempotency_key"] for j in saved_jobs], [key])

    def test_library_resumes_only_untouched_units_after_interruption(self):
        self.service.video_config = replace(self.service.video_config, api_key="offline-test-key")
        with patch.object(self.service.library_asset_executor, "submit"):
            first = enqueue_library_assets(self.service)[self.pid]
            prior = next(j for j in self.service.state["jobs"] if j["id"] == first["job_id"])
            self.service._update(prior, status="interrupted")
            work = asset_work_state(self.service, self.pid)
            uncertain = work["items"][0]
            subject = uncertain["subject_id"], uncertain["purpose"]
            style = "离线制作方向"
            uncertain.update(status="generating", job_id=prior["id"], style_sha256=digest_json(style))
            self.service.state["asset_work"] = {self.pid: {"items": {digest_json(list(subject)): uncertain},
                "style": style, "basis": work["basis"], "production_started": True}}
            self.service._save()
            second = enqueue_library_assets(self.service)[self.pid]
            self.assertNotEqual(second["job_id"], prior["id"])
        job = next(j for j in self.service.state["jobs"] if j["id"] == second["job_id"])
        from pianchang.asset_dispatch import run_asset_order
        with patch("pianchang.asset_dispatch._one") as perform:
            run_asset_order(self.service, job, {"basis": work["basis"]})
        targets = [(c.args[4]["subject_id"], c.args[4]["purpose"]) for c in perform.call_args_list]
        self.assertNotIn(subject, targets)
        self.assertEqual(len(targets), len(work["items"]) - 1)

    def test_old_basis_keeps_verified_approval_but_rechecks_changed_local_script(self):
        def legacy_catalog_hash(catalog):
            entities = [[e["id"], e["version"], e["canonical"], e["source_refs"]]
                        for e in sorted(catalog["entities"], key=lambda e: e["id"])]
            return digest_json([catalog["story_id"], catalog.get("script_ref"), entities])
        with patch("pianchang.asset_dispatch._basis", side_effect=legacy_catalog_hash):
            pin = self.upload()
            with patch("pianchang.asset_review._observe_images", side_effect=self.pass_observation):
                job = self.wait_job("asset_inspect", {"pin": pin}, "offline-old-basis")
                self.assertEqual(job["status"], "succeeded", job["error"])
            body = {k: pin[k] for k in ("asset_id", "revision_id", "sha256")}
            receipt = approve_asset(self.service, self.pid, body)
        current = asset_work_state(self.service, self.pid)
        current_row = next(row for row in current["items"] if row["candidate"] == pin)
        self.assertEqual(current_row["status"], "approved")
        self.assertNotEqual(receipt["basis"], current_row["basis"])
        check_reference_approval(self.service, self.pid, pin)
        self.assertEqual(receipt, approve_asset(self.service, self.pid, body))

        # An older unpublished script had a null reference. Matching entities alone
        # cannot transfer its approval after an edit to the local screenplay.
        self.project["script_source"]["script"]["idea"] += "（离线改稿）"
        write_json(self.service.root / "fixtures" / "story_project.json", self.project)
        updated = asset_work_state(self.service, self.pid)
        row = next(i for i in updated["items"] if i["subject_id"] == pin["subject_id"] and i["purpose"] == pin["purpose"])
        self.assertEqual(row["status"], "planned")
        with self.assertRaises(ValueError):
            check_reference_approval(self.service, self.pid, pin)

    def test_library_batch_does_not_block_new_story_or_overlap_same_story_repairs(self):
        self.service.video_config = replace(self.service.video_config, api_key="offline-test-key")
        started, release, new_work, repair = (threading.Event() for _ in range(4))
        def offline_work(service, job, order):
            if job["kind"] == "asset_regenerate":
                repair.set()
            elif job["project_id"] == self.pid:
                started.set()
                if not release.wait(30):
                    raise AssertionError("offline library batch timed out")
            else:
                new_work.set()
            return {"summary": "offline asset worker"}
        with patch("pianchang.asset_dispatch.run_asset_order", side_effect=offline_work):
            try:
                enqueue_library_assets(self.service)
                self.assertTrue(started.wait(10))
                self.service.submit("asset_regenerate", self.pid, {"subject_id":"char:lin", "purpose":"portrait"})
                file = self.service.state_dir / "stories" / "offline-new" / "project.json"
                write_json(file, self.project)
                self.service.submit("asset_production", project_id_for_path(file), {})
                self.assertTrue(new_work.wait(10), "new story waited behind library migration")
                self.assertFalse(repair.is_set(), "repair overlapped this story's running batch")
            finally:
                release.set()
            self.assertTrue(repair.wait(10))

    def test_other_story_video_work_starts_during_asset_batch(self):
        file = self.service.state_dir / "stories" / "offline-second" / "project.json"
        write_json(file, {**self.project, "title":"离线并发制作"})
        other = next(pid for pid,r in self.service._discover().items() if r["path"] == file)
        first_started, video_started, release_assets = (threading.Event() for _ in range(3))

        def hold_assets(service, job, body):
            first_started.set()
            if not release_assets.wait(20):
                raise AssertionError("offline batch timed out")
            return {"summary":"offline asset work finished"}

        def run_video(service, job, body):
            video_started.set()
            return {"summary":"offline video work finished"}

        with patch("pianchang.asset_dispatch.run_asset_order", side_effect=hold_assets), \
             patch("pianchang.studio_jobs.run_video_job", side_effect=run_video):
            self.service.submit("asset_production",self.pid,{})
            try:
                self.assertTrue(first_started.wait(10))
                args = {"project_sha256":"0"*64, "max_video_generations":0}
                with self.assertRaisesRegex(ValueError,"此故事已有制作任务"):
                    self.service.submit("studio_video",self.pid,args)
                self.service.submit("studio_video",other,args)
                self.assertTrue(video_started.wait(10), "Another story shared the blocked asset worker")
            finally:
                release_assets.set()

    def test_failed_replacement_preserves_selected_pin_and_only_success_rebinds(self):
        first = self.upload()
        with patch("pianchang.asset_review._observe_images", side_effect=self.pass_observation):
            accepted = self.wait_job("asset_inspect", {"pin": first})
        self.assertEqual(accepted["status"], "succeeded", accepted["error"])
        initial_spec = studio_project_for_video(self.service, self.pid)["spec"]
        latest = upload_studio_reference(self.service, self.pid, {
            "subject_id": "char:lin", "purpose": "portrait", "asset_id": first["asset_id"], "parent_revision": "r1",
            "data_uri": "data:image/png;base64," + base64.b64encode(offline_png((4,5,6))).decode()})["pin"]
        with patch("pianchang.asset_review._observe_images", return_value=([], {"offline": True})):
            failed = self.wait_job("asset_inspect", {"pin": latest})
        self.assertEqual(failed["status"], "failed")
        unit = next(i for i in asset_work_state(self.service, self.pid)["items"] if i["candidate"] == latest)
        self.assertEqual((unit["status"], unit["selected_pin"]), ("needs_attention", first))
        self.assertEqual(studio_project_for_video(self.service, self.pid)["spec"], initial_spec)
        check_reference_approval(self.service, self.pid, first)
        with self.assertRaises(ValueError):
            self.service.submit("asset_inspect", self.pid, {"pin": first})
        with patch("pianchang.asset_review._observe_images", side_effect=self.pass_observation):
            recovered = self.wait_job("asset_inspect", {"pin": latest})
        self.assertEqual(recovered["status"], "succeeded", recovered["error"])
        self.assertEqual(recovered["result"]["status"], "approved")
        view = studio_project_for_video(self.service, self.pid)
        self.assertEqual(view["spec"]["revision_id"], "r2")
        for shot in view["project"]["shots"]:
            self.assertNotIn(first, shot.get("required_refs", []))
            if first["subject_id"] in shot["entity_ids"]:
                self.assertIn(latest, shot["required_refs"])
        with self.assertRaises(ValueError):
            check_reference_approval(self.service, self.pid, first)
        # Reinspection of the selected pixels invalidates the old evidence even without a new media revision.
        with patch("pianchang.asset_review._observe_images", return_value=([], {"offline": True})):
            self.assertEqual(self.wait_job("asset_inspect", {"pin": latest})["status"], "failed")
        unit = next(i for i in asset_work_state(self.service, self.pid)["items"] if i["candidate"] == latest)
        self.assertIsNone(unit["selected_pin"])
        with self.assertRaises(ValueError):
            check_reference_approval(self.service, self.pid, latest)

    def test_review_only_approval_and_atomic_multi_shot_binding(self):
        pin = self.upload()
        with self.assertRaises(ValueError):
            check_reference_approval(self.service,self.pid,pin)
        with patch("pianchang.asset_review._observe_images",side_effect=self.pass_observation) as observe:
            job = self.wait_job("asset_inspect",{"pin":pin},"offline-review")
            self.assertEqual(job["status"],"succeeded",job["error"])
            self.assertEqual(self.wait_job("asset_inspect",{"pin":pin},"offline-review")["id"],job["id"])
            self.assertEqual(observe.call_count,1)
        body = {k:pin[k] for k in ("asset_id","revision_id","sha256")}
        confirmed = approve_asset(self.service,self.pid,body)
        self.assertEqual(confirmed["authority"], "asset_agent")
        self.assertEqual(confirmed["spec"]["revision_id"],"r1")
        self.assertEqual(approve_asset(self.service,self.pid,body),confirmed)
        view = studio_project_for_video(self.service,self.pid)
        self.assertTrue(confirmed["shot_ids"])
        for shot in view["project"]["shots"]:
            if "char:lin" in shot["entity_ids"]:
                self.assertIn(pin,shot["required_refs"])
        check_reference_approval(self.service,self.pid,pin)
        self.assertEqual(asset_work_state(self.service,self.pid)["counts"]["approved"],1)

    def test_human_approval_selects_reviewed_candidate_without_rewriting_failed_job(self):
        from pianchang.asset_dispatch import manually_approve_asset
        from pianchang.collab_store import CollabStore
        pin = self.upload()
        with patch("pianchang.asset_review._observe_images", return_value=([], {"offline": True})):
            failed_job = self.wait_job("asset_inspect", {"pin": pin})
        self.assertEqual(failed_job["status"], "failed")
        state = asset_work_state(self.service, self.pid)
        unit = next(row for row in state["items"] if row["candidate"] == pin)
        self.assertEqual(unit["status"], "needs_attention")
        report = unit["review"]
        self.assertEqual(report["verdict"], "UNKNOWN")
        body = {key: pin[key] for key in ("asset_id", "revision_id", "sha256")}
        body["review_ref"] = report["review_ref"]
        store = CollabStore(studio_project_for_video(self.service, self.pid)["store_path"])
        original_evidence = store.get_asset(report["review_ref"]["asset_id"])
        with self.assertRaises(ValueError):
            approve_asset(self.service, self.pid, body)

        with patch.object(self.service, "submit") as submit, patch("pianchang.asset_review._observe_images") as observe:
            approval = manually_approve_asset(self.service, self.pid, body)
            self.assertEqual(approval, manually_approve_asset(self.service, self.pid, body))
            submit.assert_not_called()
            observe.assert_not_called()
        self.assertEqual((approval["selection_mode"], approval["authority"], approval["review_verdict"]),
                         ("human", "human", "UNKNOWN"))
        self.assertEqual(approval["review_ref"], report["review_ref"])
        self.assertEqual(approval["pin"], pin)
        self.assertNotEqual(approval["trace_id"], report["trace_id"])
        audit = self.service.tracer.get_events(approval["trace_id"], names=["studio.assets.human_approved"])
        self.assertEqual(len(audit), 1)
        decision = self.service.tracer.resolve(audit[0]["data"])
        self.assertEqual((decision["pin"], decision["original_verdict"], decision["review_trace_id"]),
                         (pin, "UNKNOWN", report["trace_id"]))
        self.assertEqual(store.get_asset(report["review_ref"]["asset_id"]), original_evidence)
        job = next(job for job in self.service.list_jobs() if job["id"] == failed_job["id"])
        self.assertEqual((job["status"], job["trace_id"]), ("failed", failed_job["trace_id"]))
        updated = asset_work_state(self.service, self.pid)
        unit = next(row for row in updated["items"] if row["candidate"] == pin)
        self.assertEqual((unit["status"], unit["selected_pin"]), ("approved", pin))
        self.assertEqual(unit["review"], report)
        self.assertEqual(updated["counts"].get("needs_attention", 0), state["counts"]["needs_attention"] - 1)
        after = studio_project_for_video(self.service, self.pid)
        self.assertEqual(after["spec"]["revision_id"], "r1")
        self.assertEqual([s["id"] for s in after["project"]["shots"] if pin in s.get("required_refs", [])],
                         [s["id"] for s in self.project["shots"] if pin["subject_id"] in s["entity_ids"]])
        self.assertEqual(len(store.revisions(after["spec"]["asset_id"])), 1)
        check_reference_approval(self.service, self.pid, pin)

    def test_human_approval_rejects_stale_review_media_revision_or_changed_hash(self):
        from pianchang.asset_dispatch import manually_approve_asset
        first = self.upload()
        def mismatch(service, brief, criteria, files):
            rows, provider = self.pass_observation(service, brief, criteria, files)
            rows[0].update(verdict="FAIL", reason="offline: left eyebrow is hidden")
            return rows, provider

        def review_request(pin, key):
            with patch("pianchang.asset_review._observe_images", side_effect=mismatch):
                job = self.wait_job("asset_inspect", {"pin": pin}, key)
            self.assertEqual(job["status"], "failed")
            item = next(i for i in asset_work_state(self.service, self.pid)["items"] if i["candidate"] == pin)
            self.assertEqual((item["status"], item["review"]["verdict"]), ("needs_attention", "FAIL"))
            return {**{k: pin[k] for k in ("asset_id", "revision_id", "sha256")},
                    "review_ref": item["review"]["review_ref"]}

        first_review = review_request(first, "offline-manual-first")
        next_review = review_request(first, "offline-manual-reinspect")
        self.assertNotEqual(first_review["review_ref"], next_review["review_ref"])
        for stale in (first_review, {**next_review, "sha256": "0" * 64}, {**next_review, "review_ref": None}):
            with self.subTest(stale=stale), self.assertRaises(ValueError):
                manually_approve_asset(self.service, self.pid, stale)
        second = upload_studio_reference(self.service, self.pid, {
            "subject_id": first["subject_id"], "purpose": first["purpose"], "parent_revision": first["revision_id"],
            "asset_id": first["asset_id"],
            "data_uri": "data:image/png;base64," + base64.b64encode(offline_png((30, 40, 50))).decode()})["pin"]
        with self.assertRaises(ValueError):
            manually_approve_asset(self.service, self.pid, next_review)
        self.assertIsNone(studio_project_for_video(self.service, self.pid)["spec"]["revision_id"])
        new_request = review_request(second, "offline-manual-second")
        approval = manually_approve_asset(self.service, self.pid, new_request)
        self.assertEqual((approval["pin"], approval["review_verdict"]), (second, "FAIL"))
        with self.assertRaises(ValueError):
            check_reference_approval(self.service, self.pid, first)

    def test_batch_adopts_existing_then_preserves_result_and_uncertain_units(self):
        pin = self.upload()
        with patch("pianchang.asset_review._observe_images",side_effect=self.pass_observation) as observe, \
             patch("pianchang.asset_review.run_reference_loop",side_effect=ValueError("offline provider unreachable")) as generate:
            one = self.wait_job("asset_production", {"style":"离线统一摄影要求"})
            state = asset_work_state(self.service,self.pid)
            count = len(state["items"])
            self.assertEqual(one["result"]["processed"],count)
            self.assertEqual(one["status"],"failed")
            self.assertEqual(observe.call_count,1)
            self.assertEqual(generate.call_count,count-1)
            two = self.wait_job("asset_production", {"style":"离线统一摄影要求"})
            self.assertEqual(two["result"]["processed"],0)
            self.assertEqual(generate.call_count,count-1)
            self.assertTrue(all(call.args[2]["style"]=="离线统一摄影要求" for call in generate.call_args_list))
            self.assertEqual(next(i for i in state["items"] if i["status"]=="approved")["selected_pin"],pin)
        with self.assertRaisesRegex(ValueError,"S002"):
            check_video_asset_readiness(self.service,self.pid,self.project)
