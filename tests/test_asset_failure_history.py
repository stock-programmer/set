"""Offline trace reconstruction keeps past failures separate from repairs and other candidates."""
import tempfile
import unittest
from types import SimpleNamespace

from pianchang.asset_failure_history import read_asset_job_history
from trace_system import Tracer, use_context


class AssetFailureHistoryTests(unittest.TestCase):
    def test_only_final_unresolved_unit_and_its_exact_review_in_recorded_order(self):
        with tempfile.TemporaryDirectory() as folder:
            tracer = Tracer(folder, inline_bytes=500)
            job = {"id": "offline-job", "project_id": "offline-story"}
            def record(tr, subject, version, verdict, status):
                pin = {"subject_id": subject, "purpose": "portrait", "revision_id": version, "asset_id": subject,
                       "sha256": "a"*64, "media_sha256": "b"*64}
                report = {"candidate": pin, "trace_id": tr.trace_id, "verdict": verdict, "brief": {"name": subject},
                          "checks": [{"path": "subject", "status": verdict, "actual": f"offline observation {version}"}]}
                with tracer.span("asset.reference.review", inputs={"pid": job["project_id"], "pin": pin}) as review:
                    with tracer.span("collab.assets.publish") as publication:
                        publication.set_output({"kind": "reference_review", "content": report, "asset_id": "review/"+subject,
                                                "revision_id": "r1", "sha256": "c"*64})
                tracer.event("studio.assets.unit.transition", {"status": status, "job_id": job["id"], "candidate": pin,
                             "subject_id": subject, "purpose": "portrait"}, kind="state")
                return review.context["span_id"]
            with use_context(None, tracer=tracer):
                with tracer.trace("offline original order") as origin:
                    job["trace_id"] = origin.trace_id
                    record(origin, "A", "r1", "FAIL", "needs_attention")
                    final_span = record(origin, "A", "r2", "UNKNOWN", "needs_attention")
                    record(origin, "B", "r1", "UNKNOWN", "needs_attention")
                    record(origin, "B", "r2", "PASS", "approved")
                with tracer.trace("offline later repair") as later:
                    record(later, "A", "r3", "PASS", "approved")
                history = read_asset_job_history(SimpleNamespace(tracer=tracer), job)
            self.assertEqual(len(history["items"]), 1)
            issue = history["items"][0]
            self.assertEqual((issue["name"], issue["revision_id"], issue["attempt_count"], issue["verdict"]),
                             ("A", "r2", 2, "UNKNOWN"))
            self.assertEqual((issue["trace_id"], issue["span_id"]), (origin.trace_id, final_span))
            self.assertEqual(issue["evidence"][0]["actual"], "offline observation r2")
