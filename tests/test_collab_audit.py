"""Offline negative checks for the collaboration auditor. No provider calls."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from pianchang.acceptance import file_sha256
from tools.collab_audit import Audit, _changed_beat, _feedback_verified, audit_collaboration
from pianchang.collab_store import CollabStore
from trace_system import Tracer, use_context
from pianchang.story_spec import digest_json, validate_compilation


class AuditTests(unittest.TestCase):
    def test_edge_report_must_match_real_media_and_observed_trace(self):
        project = json.loads((Path(__file__).resolve().parents[1] / "fixtures/story_project.json").read_text(encoding="utf-8"))
        states = {s["shot_id"]:s for s in validate_compilation(project)["state_trace"]}
        baseline = {"asset_id":"offline-script","revision_id":"r2"}
        correct = {"from":"S002", "to":"S003", "script_ref":baseline, "preview":"offline-preview",
                   "media":["old-S002", "new-S003"], "audiovisual_continuity":"UNKNOWN",
                   "state_checks":{"path":"S002.after/S003.before","expected":states["S002"]["after"],
                                   "actual":states["S003"]["before"],"status":"PASS"}}
        data = {"project":project, "shots":{"S002":{"media_sha256":"old-S002"},"S003":{"media_sha256":"new-S003"}},
                "edge_reviews":[correct], "script_ref":baseline, "preview":{"preview_path":"offline-preview"}}
        audit = Audit.__new__(Audit)
        audit.checks = []
        audit.trace_results = [{"trace_id":"offline-video","edge_review_spans":[
            {"span_id":"offline-edge","output_sha256":digest_json([correct])}]}]
        impact = {"recheck_edges":[["S002","S003"]]}
        evidence = audit._checked_edges("r2",data,impact,"offline-video")
        self.assertEqual([r["status"] for r in audit.checks], ["PASS"] * len(audit.checks))
        self.assertEqual(evidence[0]["audiovisual_continuity"], "UNKNOWN")
        for wrong, failed in (([], "r2.衔接实际检查范围"),
                              ([{**correct,"media":["old-S002","old-S003"]}], "r2.S002→S003.选用媒体")):
            audit.checks.clear()
            audit._checked_edges("r2",{**data,"edge_reviews":wrong},impact,"offline-video")
            failures = {c["path"] for c in audit.checks if c["status"] == "FAIL"}
            self.assertIn(failed, failures)
            self.assertIn("r2.衔接结果绑定实际执行Trace", failures)

    def test_an_unrelated_edit_fails_even_if_requested_beat_changed(self):
        original = json.loads((Path(__file__).resolve().parents[1] / "fixtures/story_script.json").read_text(encoding="utf-8"))
        revised = copy.deepcopy(original)
        revised["script_version"] += 1
        revised["episodes"][0]["scenes"][0]["beats"][2]["text"] += " 他停了一秒。"
        self.assertTrue(_changed_beat(original, revised, "B003"))
        revised["setup"][0]["text"] += " 未经委托的改变。"
        self.assertFalse(_changed_beat(original, revised, "B003"))

    def test_feedback_requires_the_actual_result_in_the_immediately_following_decision(self):
        actual = {"type": "tool_result", "call_id": "step-003", "result": "offline observed result"}
        events = [{"name": "agent.loop.tool_result", "data": actual}]
        def request(result):
            return {"name": "agent.loop.model_requested", "data": {"messages": [
                {"role": "user", "content": json.dumps(result, sort_keys=True)}
            ]}}
        self.assertEqual(_feedback_verified(events + [request(actual)], lambda x:x), 1)
        wrong = {**actual, "result": "different"}
        self.assertEqual(_feedback_verified(events + [request(wrong), request(actual)], lambda x:x), 0)

    def test_incomplete_fixture_reports_failure_without_modifying_source_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            tracer = Tracer(directory / "traces")
            with use_context(None, tracer=tracer):
                db_path = directory / "assets.sqlite3"
                CollabStore(db_path)
                run_id = "a" * 32
                folder = directory / "runs" / run_id
                folder.mkdir(parents=True)
                index = folder / "overview.json"
                index.write_text(json.dumps({"id":run_id,"trace_id":"0"*32,"status":"blocked","stages":[]}), encoding="utf-8")
                original_hashes = (file_sha256(db_path), file_sha256(index))
                report = audit_collaboration(directory, run_id, source=directory / "absent.json")
                self.assertEqual(report["status"], "FAIL")
                self.assertTrue(report["failures"])
                self.assertEqual(original_hashes, (file_sha256(db_path), file_sha256(index)))
                self.assertEqual(json.loads((folder / "evidence.json").read_text(encoding="utf-8"))["status"], "FAIL")
                self.assertTrue(tracer.verify(report["audit_trace_id"])["ok"])

    def test_video_submission_ids_are_indexed_by_origin_trace_even_with_several_tasks(self):
        class OfflineTraceReader:
            @staticmethod
            def resolve(value):
                return value

            def export_trace(self, trace_id):
                # Simulated evidence reader, independent of all production trace databases.
                spans = []
                for index in (1, 2):
                    task_id = "offline-task-" + str(index)
                    parent = "offline-submit-" + str(index)
                    spans.append({"name":"bailian.video.submit", "span_id":parent, "parent_span_id":None,
                                  "status":"OK", "output":{"output":{"task_id":task_id}}})
                    spans.append({"name":"bailian.video.http", "parent_span_id":parent, "events":[
                        {"name":"http.response", "data":{"http_status":200, "body":{"output":{"task_id":task_id}}}}]})
                return {"trace":{"name":"agent.task.video", "status":"ERROR", "open_span_count":0,
                                 "spans":spans, "events":[]}, "integrity":{"ok":True, "checked_events":12}}
        audit = Audit.__new__(Audit)
        audit.checks, audit.trace_results, audit.submitted = [], [], {}
        audit.tracer = OfflineTraceReader()
        audit.trace_evidence("offline-origin-trace", "offline", require_success=False)
        self.assertEqual(audit.submitted, {"offline-origin-trace":["offline-task-1","offline-task-2"]})
