"""Offline checks for extra takes offered from pinned local evidence."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from pianchang.story_spec import digest_json
from pianchang.studio_retakes import extra_retake_proposal
from trace_system import Tracer, use_context


class RetakeProjectionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        traces = Tracer(root / "offline-traces")
        context = use_context(None, tracer=traces)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        task = traces.trace("offline extra retake tests")
        task.__enter__()
        self.addCleanup(task.__exit__, None, None, None)
        self.folder = root / "runs" / "run"
        self.folder.mkdir(parents=True)
        self.store = root / "assets.sqlite3"
        self.view = {"store_path": str(self.store), "story_id": "story", "run_id": "run",
                     "spec": {"revision_id": "r2"}, "script_ref": {"revision_id": "r1"},
                     "production_ref": None, "project": {"shots": [{"id": "A"}, {"id": "B"}]}}

    def test_required_failure_only_for_current_reusable_shots_and_exact_saved_report(self):
        report = {"shot_id": "A", "results": [
            {"criterion_id": "performance", "status": "UNKNOWN", "required": True, "reason": "no observation"},
            {"criterion_id": "clothing", "status": "FAIL", "required": True, "reason": "red coat missing"},
            {"criterion_id": "optional", "status": "FAIL", "required": False, "reason": "optional"}]}
        file = self.folder / "report.json"
        file.write_text(json.dumps(report), encoding="utf-8")
        production = {"content": {"reports": {"A": {"status": "FAIL", "report_path": str(file),
                                                    "report_sha256": digest_json(report)}}}}
        proposal = extra_retake_proposal(self.view, production, ["A", "B"])
        self.assertEqual(proposal["shot_ids"], ["A"])
        self.assertEqual([row["criterion_id"] for row in proposal["evidence"]], ["clothing"])
        self.assertIsNone(extra_retake_proposal(self.view, production, ["B"]))
        file.write_text("{}", encoding="utf-8")
        self.assertIsNone(extra_retake_proposal(self.view, production, ["A","B"]))
        self.assertFalse(self.store.exists())

    def test_only_latest_confirmed_provider_failure_on_displayed_inputs_can_offer_extra(self):
        request = {"script": self.view["script_ref"], "template": self.view["spec"], "previous": None}
        packet = {"partial": {"request_id": "take", "sha256":"a"*64}, "failed_shots": {
            "B": {"status": "FAILED", "task_id":"cloud-b", "manifest_sha256":"b"*64, "reason":"render failed"}}}
        with closing(sqlite3.connect(self.store)) as db:
            db.execute("""CREATE TABLE messages(seq INTEGER PRIMARY KEY, story_id TEXT, correlation_id TEXT,
                          message_id TEXT, sender TEXT, recipient TEXT, message_type TEXT, causation_id TEXT, payload TEXT)""")
            db.execute("INSERT INTO messages VALUES(1,'story','run','take','studio','video','production.requested',NULL,?)",
                       (json.dumps(request),))
            db.execute("INSERT INTO messages VALUES(2,'story','run','result','video','studio','production.result','take',?)",
                       (json.dumps(packet),))
            db.commit()
            p = extra_retake_proposal(self.view, None, [])
            self.assertEqual((p["shot_ids"], p["reused_shots"]), (["B"], ["A"]))
            self.assertEqual(p["partial"], packet["partial"])
            self.assertIsNone(extra_retake_proposal({**self.view, "spec":{"revision_id":"r3"}}, None, []))
            packet["failed_shots"]["B"]["status"] = "UNKNOWN"
            db.execute("UPDATE messages SET payload=? WHERE seq=2", (json.dumps(packet),))
            db.commit()
            self.assertIsNone(extra_retake_proposal(self.view, None, []))
            db.execute("INSERT INTO messages VALUES(3,'story','run','next','studio','video','production.requested',NULL,?)",
                       (json.dumps(request),))
            db.commit()
            self.assertIsNone(extra_retake_proposal(self.view, None, []))
