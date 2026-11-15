"""Offline durable connection reuse; no model/provider operations."""
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from trace_system import TraceError,Tracer,current_context,use_context


class StorageSessionTests(unittest.TestCase):
    def test_read_computation_does_not_block_other_durable_trace_writers(self):
        with tempfile.TemporaryDirectory() as temporary:
            tracer=Tracer(temporary)
            with tracer.storage_session(),use_context(None,tracer=tracer),tracer.trace("test.reader",inputs={"label":"offline"}) as root:
                with tracer.read_batch():
                    root.event("offline.staged_read",{"status":"PASS"})
                    def other_writer():
                        with use_context(None,tracer=tracer),tracer.trace("test.independent_writer",inputs={"label":"offline"}) as other:
                            other.event("offline.durable",{})
                        return other.trace_id
                    with ThreadPoolExecutor(max_workers=1) as executor:other_id=executor.submit(other_writer).result(timeout=3)
            self.assertTrue(tracer.verify(root.trace_id)["ok"]);self.assertTrue(tracer.verify(other_id)["ok"])

    def test_staged_large_payload_redaction_and_first_end_survive_atomic_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            tracer=Tracer(temporary);secret="opaque-private-material-12345";tracer.register_secret(secret)
            with tracer.storage_session(),use_context(None,tracer=tracer),tracer.trace("test.staged_payload",inputs={"label":"offline"}) as root:
                with tracer.read_batch():
                    child=tracer.start_span("offline.child",inputs={"text":"ordinary evidence "*2000,"credential":secret})
                    tracer.end_span(child.context,status="ERROR",error={"message":secret})
                    tracer.end_span(child.context,status="OK")
            detail=tracer.get_trace(root.trace_id)
            self.assertEqual(next(s for s in detail["spans"] if s["span_id"]==child.span_id)["status"],"ERROR")
            self.assertNotIn(secret,str(tracer.export_trace(root.trace_id)))
            self.assertTrue(tracer.verify(root.trace_id)["ok"])

    def test_read_batch_keeps_root_durable_and_commits_evidence_before_return(self):
        with tempfile.TemporaryDirectory() as temporary:
            tracer=Tracer(temporary)
            with tracer.storage_session(),use_context(None,tracer=tracer),tracer.trace("test.read_batch",inputs={"label":"offline"}) as root:
                with self.assertRaisesRegex(ValueError,"offline validation failure"):
                    with tracer.read_batch():
                        with tracer.span("offline.validation",kind="validation"):
                            tracer.event("offline.finding",{"path":"x","expected":1,"actual":2,"status":"FAIL"})
                            with closing(sqlite3.connect(tracer.path)) as observer:
                                self.assertEqual(observer.execute("SELECT count(*) FROM events WHERE trace_id=?",(root.trace_id,)).fetchone()[0],1,"The received request is already durable; unfinished pure-read evidence is not claimed complete")
                            raise ValueError("offline validation failure")
                with closing(sqlite3.connect(tracer.path)) as observer:
                    self.assertEqual(observer.execute("SELECT count(*) FROM events WHERE trace_id=?",(root.trace_id,)).fetchone()[0],4)
            self.assertTrue(tracer.verify(root.trace_id)["ok"])

    def test_read_batch_requires_root_and_storage_failure_stops_returning_a_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            tracer=Tracer(temporary)
            with self.assertRaises(TraceError):
                with tracer.read_batch():pass
            with tracer.storage_session(),use_context(None,tracer=tracer),tracer.trace("test.read_failure",inputs={"label":"offline"}) as root:
                returned=[]
                insert = tracer._insert_record
                def fail_last(db, record, chain):
                    if record["name"] == "offline.must_persist":
                        raise sqlite3.OperationalError("offline disk failure")
                    return insert(db, record, chain)
                with patch.object(tracer,"_insert_record",side_effect=fail_last):
                    with self.assertRaises(TraceError):
                        with tracer.read_batch():
                            staged = tracer.event("offline.staged", {"item": "offline evidence "*2048})
                            tracer.event("offline.must_persist",{})
                        returned.append(True)
                self.assertEqual(returned,[])
                self.assertTrue(staged["pending_read_batch"])
                self.assertEqual(len(tracer.get_events(root.trace_id)), 1)
                with closing(sqlite3.connect(tracer.path)) as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM artifacts").fetchone()[0], 0)
            self.assertTrue(tracer.verify(root.trace_id)["ok"])

    def test_large_batch_preparation_allows_other_writer_and_rechecks_new_secret(self):
        with tempfile.TemporaryDirectory() as temporary:
            tracer = Tracer(temporary, inline_bytes=256)
            ready = threading.Event()
            resume = threading.Event()
            token = "offline-late-sensitive-value"
            large = "offline fixture " * 512
            evidence = {"literal": {"$artifact": token, "bytes": 17, "media_type": token},
                        "report": large + token}
            prepare = tracer._prepare_read_record

            def paused_prepare(record, secrets, artifacts):
                result = prepare(record, secrets, artifacts)
                if not ready.is_set() and record["name"] == "offline.pending":
                    ready.set()
                    self.assertTrue(resume.wait(15))
                return result

            def run_batch():
                with use_context(None, tracer=tracer), tracer.trace("test.queued_batch") as root:
                    with tracer.read_batch():
                        root.event("offline.pending", evidence)
                return root.trace_id

            with tracer.storage_session(), patch.object(tracer,"_prepare_read_record",side_effect=paused_prepare):
                with ThreadPoolExecutor(max_workers=1) as executor:
                    pending = executor.submit(run_batch)
                    try:
                        self.assertTrue(ready.wait(15))
                        with tracer.trace("test.independent_writer") as other:
                            other.event("offline.write", {"ready": True})
                        tracer.register_secret(token)
                    finally:
                        resume.set()
                    trace_id = pending.result(timeout=15)
            self.assertTrue(tracer.verify(other.trace_id)["ok"])
            self.assertTrue(tracer.verify(trace_id)["ok"])
            with closing(sqlite3.connect(tracer.path)) as db:
                content = " ".join(r[0] for r in db.execute("SELECT content FROM artifacts"))
                self.assertNotIn(token, content)
                self.assertIn('"$artifact":"[REDACTED]"',content)
                self.assertIn('"$literal_reason":"user_artifact_shape"',content)

    def test_reuse_is_durable_across_threads_and_released_at_last_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            tracer=Tracer(Path(temporary)/"traces")
            with patch("trace_system.core.sqlite3.connect",wraps=sqlite3.connect) as connect:
                with tracer.storage_session(),use_context(None,tracer=tracer),tracer.trace("test.pooled",inputs={"label":"offline"}) as root:
                    context=current_context()
                    def write(index):
                        with use_context(context,tracer=tracer):tracer.event("offline.concurrent",{"index":index})
                    with tracer.storage_session(),ThreadPoolExecutor(max_workers=4) as executor:list(executor.map(write,range(24)))
                    with closing(sqlite3.connect(tracer.path)) as observer:
                        self.assertEqual(observer.execute("SELECT count(*) FROM events WHERE trace_id=? AND name='offline.concurrent'",(root.trace_id,)).fetchone()[0],24)
                    self.assertLess(connect.call_count,10,"Connections are reused, while each actual event is immediately visible to another connection")
                    self.assertTrue(tracer._pool)
                self.assertEqual(tracer._pool,[])
            self.assertTrue(tracer.verify(root.trace_id)["ok"])
            moved=tracer.path.with_suffix(".closed");tracer.path.rename(moved);moved.rename(tracer.path)

    def test_failed_transaction_is_rolled_back_before_reuse(self):
        with tempfile.TemporaryDirectory() as temporary:
            tracer=Tracer(temporary)
            with tracer.storage_session():
                with self.assertRaisesRegex(ValueError,"offline abort"):
                    with tracer._db() as db:
                        db.execute("INSERT INTO metadata(key,value) VALUES('offline_abort','must not persist')")
                        raise ValueError("offline abort")
                with tracer._db() as db:
                    self.assertIsNone(db.execute("SELECT value FROM metadata WHERE key='offline_abort'").fetchone())
                with tracer.trace("test.after_rollback",inputs={"label":"offline"}) as root:root.event("valid",{"status":"PASS"})
            self.assertTrue(tracer.verify(root.trace_id)["ok"])


if __name__=="__main__":unittest.main()
