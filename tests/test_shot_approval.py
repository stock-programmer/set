"""Offline receipt/CAS regressions for the manual shot review command."""
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pianchang.shot_approval import approve_shot, read_approvals, report_can_approve, selected_reviews, shot_review_panel
from pianchang.story_spec import digest_json
from trace_system import Tracer, use_context


class ShotApprovalTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        tracer = Tracer(root / "traces")
        self.enterContext(tracer.storage_session())
        self.enterContext(use_context(None, tracer=tracer))
        self.enterContext(tracer.trace("test.manual_shot", inputs={"source": "synthetic offline review"}))
        self.pid, self.cid = "fixture", "a" * 32
        self.work = root / "runs" / "run" / ("video-spec-" + self.cid)
        self.work.mkdir(parents=True)
        self.project = {"shots": [{"id": "S001", "description": "offline sample"}]}
        self.view = {"project_id": self.pid, "project": self.project, "run_id": "run",
                     "store_path": str(root / "db"), "published_delivery": None, "script_ref": "v1", "spec": "s1"}
        self.service = SimpleNamespace(lock=threading.RLock(), state={"jobs": []})
        path = self.work / "fixture.mp4"
        path.write_bytes(b"synthetic fixture bytes")
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        inspection = {"schema_version": "media-inspection/1", "media_sha256": sha, "media_path": str(path), "frames": []}
        contract = {"requirements": [{"criterion_id": "action_sequence", "kind": "continuous_video", "required": True}]}
        self.report = {"shot_id": "S001", "status": "FAIL", "project_sha256": digest_json(self.project),
                       "media_sha256": sha, "inspection_sha256": digest_json(inspection),
                       "contract_sha256": digest_json(contract), "results": [
                           {"criterion_id": cid, "status": status, "reason": "offline sample", "required": True}
                           for cid, status in [("evidence_integrity", "PASS"), ("candidate_binding", "PASS"), ("action_sequence", "FAIL")]]}
        self.contract = contract
        self.reports = {"S001": {"report_path": str(self.work / "report.json"), "report_sha256": digest_json(self.report),
                                 "inspection_path": str(self.work / "inspection.json")}}
        self.clips = {"S001": {"shot_id": "S001", "media_path": str(path), "media_sha256": sha}}
        self.body = {"request_id": self.cid, "shot_id": "S001", "report_sha256": digest_json(self.report), "media_sha256": sha}
        for name, data in [("report", self.report), ("reports", self.reports), ("inspection", inspection), ("contract", contract)]:
            (self.work / (name + ".json")).write_text(json.dumps(data), encoding="utf-8")
        self.enterContext(patch("pianchang.studio_assets.studio_project_for_video", side_effect=lambda *a, **kw: dict(self.view)))
        self.latest = self.enterContext(patch("pianchang.studio_retakes._latest_request", return_value=({"id": self.cid}, None)))
        self.enterContext(patch("pianchang.studio_recovery.read_incomplete_production", return_value={
            "request_id": self.cid, "available": {"S001": {"clip": self.clips["S001"]}}}))
        self.enterContext(patch("pianchang.video_collab_agent._validated_media", return_value={}))
        self.finish = self.enterContext(patch("pianchang.manual_delivery.finish_if_approved",
                                               return_value={"delivered": False, "issues": []}))

    def test_double_click_or_lost_response_replays_one_decision_even_after_publication(self):
        report_bytes = (self.work / "report.json").read_bytes()
        one = approve_shot(self.service, self.pid, self.body)
        two = approve_shot(self.service, self.pid, dict(self.body))
        self.assertEqual(one, two)
        self.assertEqual(len(list((self.work / "manual-approvals").glob("*.json"))), 1)
        self.assertEqual((self.work / "report.json").read_bytes(), report_bytes)
        receipts = read_approvals(self.work, self.project, self.clips, self.reports, project_id=self.pid)
        self.assertEqual(receipts["S001"]["approved_at"], one["approved_at"])
        self.latest.return_value = (None, None)
        self.view.update(production_ref={"revision_id": "r1"}, published_delivery={
            "request_id": self.cid, "shots": self.clips, "reports": self.reports, "manual_approvals": receipts,
            "script_ref": self.view["script_ref"], "spec_ref": self.view["spec"]})
        self.assertTrue(approve_shot(self.service, self.pid, self.body)["delivery"]["delivered"])
        self.latest.return_value = ({"id": self.cid}, {"production": self.view["production_ref"]})
        self.assertTrue(approve_shot(self.service, self.pid, self.body)["delivery"]["delivered"])
        self.view["spec"] = "s2"
        self.latest.return_value = (None, None)
        with self.assertRaises(ValueError):
            approve_shot(self.service, self.pid, self.body)
        self.assertEqual(self.finish.call_count, 2)

    def test_active_changed_and_tampered_candidates_cannot_acquire_a_decision(self):
        for key, replacement in [("request_id", "b" * 32), ("media_sha256", "c" * 64),
                                 ("report_sha256", "d" * 64), ("shot_id", "S002")]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                approve_shot(self.service, self.pid, {**self.body, key: replacement})
        for kind in ("studio_video", "asset_inspect", "pi_video"):
            self.service.state["jobs"] = [{"kind": kind, "status": "running", "project_id": self.pid, "commission_id": self.cid}]
            with self.assertRaises(ValueError):
                approve_shot(self.service, self.pid, self.body)
        self.service.state["jobs"] = []
        (self.work / "fixture.mp4").write_bytes(b"changed")
        with self.assertRaises(ValueError):
            approve_shot(self.service, self.pid, self.body)
        self.assertFalse((self.work / "manual-approvals").exists())
        self.finish.assert_not_called()

    def test_technical_failure_and_missing_binding_never_offer_manual_review(self):
        self.assertTrue(report_can_approve(self.report, self.contract))
        self.report["results"].append({"criterion_id": "file_decode", "status": "FAIL", "required": True})
        self.assertFalse(report_can_approve(self.report, self.contract))
        self.report["results"].pop()
        self.report["results"].pop(0)
        self.assertFalse(report_can_approve(self.report, self.contract))

    def test_recovery_only_offers_the_report_of_the_selected_take(self):
        current = self.work.parent / ("video-spec-" + "b" * 32)
        current.mkdir()
        snapshot = {"request_id": "b" * 32, "prior_continuation": {"request_id": self.cid},
                    "available": {"S001": {"clip": self.clips["S001"]}}}
        self.assertEqual(selected_reviews(self.view, snapshot), self.reports)
        snapshot["available"]["S001"]["clip"] = {**self.clips["S001"], "media_sha256": "f" * 64}
        self.assertEqual(selected_reviews(self.view, snapshot), {})

    def test_selection_changed_during_verification_rejects_an_old_click(self):
        def replace_selection(workspace):
            updated = {"S001": {**self.clips["S001"], "media_sha256": "e" * 64}}
            (self.work / "clips.json").write_text(json.dumps(updated), encoding="utf-8")
            return {"issues": []}
        with patch("pianchang.video_collab_agent.VideoWorkspace.acceptance_gate", side_effect=replace_selection):
            with self.assertRaisesRegex(ValueError, "审看期间"):
                approve_shot(self.service, self.pid, self.body)
        self.assertFalse((self.work / "manual-approvals").exists())

    def test_compact_panel_only_reads_indexed_failures_and_uses_the_actual_job(self):
        for name, obj in (("project", self.project), ("clips", self.clips)):
            (self.work / (name + ".json")).write_text(json.dumps(obj), encoding="utf-8")
        self.service.state["jobs"] = [{"kind": "studio_video", "status": "failed", "project_id": self.pid,
                                       "run_id": "run", "message_id": self.cid, "id": "c" * 32}]
        self.latest.return_value = ({"id": self.cid, "body": {"shooting_plan": {}}}, None)
        with patch("pianchang.studio_recovery.read_incomplete_production", side_effect=AssertionError("unexpected full scan")):
            panel = shot_review_panel(self.service, self.pid, self.cid)
        row, = panel["shots"]
        self.assertEqual((panel["available_count"], panel["total"], panel["all_shots_accepted"]), (1, 1, False))
        self.assertEqual(row["report_sha256"], self.body["report_sha256"])
        self.assertTrue(row["can_approve"])
        self.assertIn("job_id=" + "c" * 32, row["preview_url"])
        self.assertEqual([r["criterion_id"] for r in row["report"]["results"]], ["action_sequence"])
        approve_shot(self.service, self.pid, self.body)
        panel = shot_review_panel(self.service, self.pid, self.cid)
        self.assertTrue(panel["all_shots_accepted"])
        self.assertTrue(panel["shots"][0]["human_approved"])

    def test_technical_report_and_verified_clip_are_visible_without_an_approval_action(self):
        self.report["results"].append({"criterion_id":"file_decode","status":"FAIL","required":True,"reason":"unreadable frames"})
        summary = self.reports["S001"]
        summary["report_sha256"] = digest_json(self.report)
        for name, value in (("project",self.project),("report",self.report),("reports",self.reports),("clips",self.clips)):
            (self.work/(name+".json")).write_text(json.dumps(value),encoding="utf8")
        self.service.state["jobs"] = [{"id":"c"*32,"kind":"studio_video","project_id":self.pid,
            "run_id":"run","message_id":self.cid,"status":"failed"}]
        self.latest.return_value = ({"id":self.cid,"body":{"shooting_plan":{}}},None)
        media = self.clips["S001"]
        progress = {"shots":[{"shot_id":"S001","media_sha256":media["media_sha256"]}]}
        with patch("pianchang.studio_live_progress._snapshot",return_value=(progress,{"S001":media["media_path"]})) as scan:
            result = shot_review_panel(self.service,self.pid,self.cid)
            row, = result["shots"]
            self.assertFalse(result["delivered"])
            self.assertFalse(row["can_approve"])
            self.assertEqual((row["report_sha256"],row["media_sha256"]),(summary["report_sha256"],media["media_sha256"]))
            self.assertIn("file_decode",[r["criterion_id"] for r in row["report"]["results"]])
            self.assertIn("progress/media",row["preview_url"])
            scan.return_value = (progress,{})
            masked, = shot_review_panel(self.service,self.pid,self.cid)["shots"]
            self.assertNotIn("preview_url",masked)
            self.assertEqual(masked["report"],row["report"])
            self.assertFalse((self.work/"manual-approvals").is_dir())
