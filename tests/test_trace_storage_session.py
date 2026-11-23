"""Offline durable connection reuse; no model/provider operations."""
import sqlite3
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from trace_system import TraceError,Tracer,current_context,use_context


class StorageSessionTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows workbench handoff")
    def test_another_process_waits_before_writing_but_wal_readers_continue(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracer = Tracer(tmp)
            with use_context(None,tracer=tracer), tracer.trace("test.cross_process") as root:
                script = ("from trace_system.windows_writer import writer_mutex\nimport sys\n"
                          "with writer_mutex(sys.argv[1]):\n print('locked',flush=True)\n sys.stdin.readline()")
                with subprocess.Popen([sys.executable,"-u","-c",script,tracer._writer_mutex],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                        creationflags=subprocess.CREATE_NO_WINDOW) as child, ThreadPoolExecutor(max_workers=2) as pool:
                    self.assertEqual(pool.submit(child.stdout.readline).result(timeout=30).strip(),"locked")
                    entered = threading.Event()
                    def writer():
                        entered.set()
                        return tracer.event("test.durable_after_handoff", {}, context=root.context)
                    pending = pool.submit(writer)
                    try:
                        self.assertTrue(entered.wait(5))
                        self.assertEqual(len(tracer.get_events(root.trace_id)),1)
                        with self.assertRaises(TimeoutError):
                            pending.result(timeout=.2)
                    finally:
                        child.stdin.write("\n")
                        child.stdin.flush()
                    pending.result(timeout=20)
                self.assertEqual(len(tracer.get_events(root.trace_id)),2)
            self.assertTrue(tracer.verify(root.trace_id)["ok"])

    def test_queued_asset_writes_precede_later_refresh_batches_across_tracers(self):
        with tempfile.TemporaryDirectory() as temporary:
            tracer = Tracer(temporary)
            other = Tracer(Path(temporary) / ".")
            self.assertIs(tracer._writer_gate, other._writer_gate)
            ready, release = threading.Event(), threading.Event()
            refresh_prepared, enqueue_refresh = threading.Event(), threading.Event()
            queued = {name: threading.Event() for name in ("event", "start", "end", "refresh")}
            worker = threading.local()
            condition = tracer._writer_gate._condition
            wait, insert, prepare = condition.wait, tracer._insert_record, tracer._prepare_read_record

            def observe_wait(timeout=None):
                # Observe an actual queued waiter, not merely a started thread.
                label = getattr(worker, "label", None)
                if label in queued:
                    queued[label].set()
                if not hasattr(worker, "deadline"):
                    worker.deadline = time.monotonic() + 10
                remaining = worker.deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("offline writer queue did not drain")
                return wait(min(remaining, timeout) if timeout is not None else remaining)

            def paused_insert(db, record, chain):
                result = insert(db, record, chain)
                if record["name"] == "offline.holding_batch":
                    ready.set()
                    if not release.wait(10):
                        raise RuntimeError("offline batch was not released")
                return result

            def paused_prepare(record, secrets, artifacts):
                result = prepare(record, secrets, artifacts)
                if record["name"] == "offline.refresh":
                    # Commit holds the secret registry lock, so stage this later
                    # refresh before holding the earlier transaction open.
                    refresh_prepared.set()
                    if not enqueue_refresh.wait(10):
                        raise RuntimeError("offline refresh was not queued")
                return result

            with tracer.storage_session(), other.storage_session(), use_context(None, tracer=tracer):
                with tracer.trace("test.asset_and_refresh") as root:
                    child = tracer.start_span("offline.existing_child")

                    def write(label):
                        worker.label, worker.deadline = label, time.monotonic() + 10
                        owner = other if label in {"event", "end"} else tracer
                        with use_context(root.context, tracer=owner):
                            if label == "event":
                                return owner.event("offline.pending_asset", {})
                            if label == "start":
                                return owner.start_span("offline.asset_start")
                            if label == "end":
                                return owner.end_span(child.context)
                            with owner.read_batch():
                                owner.event("offline.holding_batch" if label == "holder" else "offline.refresh", {})

                    with patch.object(tracer, "_insert_record", side_effect=paused_insert), \
                         patch.object(tracer, "_prepare_read_record", side_effect=paused_prepare), \
                         patch.object(condition, "wait", side_effect=observe_wait), \
                         ThreadPoolExecutor(max_workers=6) as executor:
                        pending = {"refresh": executor.submit(write, "refresh")}
                        try:
                            self.assertTrue(refresh_prepared.wait(5))
                            holder = executor.submit(write, "holder")
                            self.assertTrue(ready.wait(5), "Fixture must hold a real uncommitted write")
                            for label in queued:
                                if label == "refresh":
                                    enqueue_refresh.set()
                                else:
                                    pending[label] = executor.submit(write, label)
                                self.assertTrue(queued[label].wait(5), label + " must enter the writer queue")
                            # WAL readers should see durable history without joining the writer queue.
                            snapshot = executor.submit(other.get_events, root.trace_id).result(timeout=5)
                            self.assertEqual([row["name"] for row in snapshot],
                                             ["test.asset_and_refresh", "offline.existing_child"])
                        finally:
                            enqueue_refresh.set()
                            release.set()
                        holder.result(timeout=5)
                        results = {label: future.result(timeout=5) for label, future in pending.items()}
                    tracer.end_span(results["start"].context)
                events = tracer.get_events(root.trace_id)
                order = [(row["name"], row["kind"]) for row in events]
                self.assertEqual(order[2:7], [
                    ("offline.holding_batch", "event"),
                    ("offline.pending_asset", "event"),
                    ("offline.asset_start", "span.start"),
                    ("offline.existing_child", "span.end"),
                    ("offline.refresh", "event"),
                ])
                self.assertTrue(tracer.verify(root.trace_id)["ok"])

    def test_cancelled_waiter_does_not_block_the_next_writer(self):
        with tempfile.TemporaryDirectory() as temporary:
            tracer = Tracer(temporary)
            with tracer.storage_session(), use_context(None, tracer=tracer), tracer.trace("test.cancelled_writer") as root:
                gate = tracer._writer_gate
                with gate.hold(), patch.object(gate._condition, "wait", side_effect=RuntimeError("offline cancelled")):
                    with ThreadPoolExecutor(max_workers=1) as executor:
                        failed = executor.submit(tracer.event, "offline.cancelled", {}, context=root.context)
                        with self.assertRaisesRegex(RuntimeError, "offline cancelled"):
                            failed.result(timeout=5)
                    self.assertEqual(len(gate._queue), 1, "Only the active writer may retain its ticket")
                self.assertFalse(gate._queue)
                root.event("offline.after_cancellation", {})
            events = tracer.get_events(root.trace_id)
            self.assertNotIn("offline.cancelled", [row["name"] for row in events])
            self.assertIn("offline.after_cancellation", [row["name"] for row in events])
            self.assertTrue(tracer.verify(root.trace_id)["ok"])

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
