import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing, contextmanager
from unittest.mock import Mock, patch

from tools import workbench_loop_watch as watch
from tools.workbench_loop_trace_recovery import reconcile_readonly_pause
from trace_system import TraceError, get_tracer, use_context


class ReadOnlyPauseTests(unittest.TestCase):
    def batch_failure(self, root, *, foreign_event=False, after_begin=False):
        from tools.workbench_loop_dependency import recheck_dependency
        tracer = get_tracer(root/"outputs/traces")
        folder = watch._folder(root)
        folder.mkdir(parents=True)
        (folder/"instance.json").write_text(json.dumps({"pid":os.getpid()}),encoding="utf-8")
        dispatch = Mock()
        with tracer.storage_session(), use_context(None,tracer=tracer), closing(sqlite3.connect(tracer.path)) as occupied:
            with tracer._db() as db:
                db.execute("PRAGMA busy_timeout=10")
            def read(*_):
                with tracer.span("studio.assets.catalog"):
                    tracer.event("studio.assets.catalog.read",{})
                    if foreign_event:
                        tracer.event("http.request",{},kind="http")
                if not after_begin:
                    occupied.execute("BEGIN IMMEDIATE")
                return {"transport_failure":{"request_kind":"model"},"safe_to_continue":True}
            write_db, insert = tracer._write_db, tracer._insert_record
            @contextmanager
            def writer():
                try:
                    with write_db() as writer_db:
                        yield writer_db
                finally:
                    occupied.rollback()
            def write_event(conn, record, chain):
                if after_begin and record["name"] == "studio.assets.catalog":
                    self.assertTrue(conn.in_transaction)
                    exc = sqlite3.OperationalError("database is locked")
                    exc.sqlite_errorname = "SQLITE_BUSY"
                    raise exc
                return insert(conn,record,chain)
            with patch.object(tracer,"_write_db",writer), patch.object(tracer,"_insert_record",write_event), \
                 patch("tools.workbench_loop_dependency._recheck_dependency",side_effect=read), \
                 patch("tools.workbench_loop_connectivity.check_planner_route") as probe, self.assertRaises(TraceError):
                with tracer.trace("workbench.watch.run") as driver, tracer.span("cli.workbench_loop_watch"):
                    try:
                        value = recheck_dependency(root,{})
                        dispatch(value)
                    except TraceError as exc:
                        watch.mark_trace_pause(root,exc)
                        raise
            probe.assert_not_called()
            dispatch.assert_not_called()
        return tracer,driver.trace_id,folder

    def test_committed_error_from_pure_batch_before_begin_recovers_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracer,tid,folder = self.batch_failure(root)
            before = tracer.get_events(tid)
            end = next(e for e in before if e["name"]=="workbench_loop.recheck_dependency" and e["kind"]=="span.end")
            self.assertEqual(end["data"]["status"],"ERROR")
            self.assertEqual(len(tracer.resolve(end["data"]["read_batch_before_begin"])),3)
            marker = json.loads((folder/"trace-paused.json").read_bytes())
            self.assertEqual(marker["uncommitted_read_batch"]["span_id"],end["span_id"])
            self.assertTrue(reconcile_readonly_pause(root))
            self.assertFalse(reconcile_readonly_pause(root))
            self.assertEqual(tracer.get_events(tid),before)

    def test_unknown_staged_call_and_write_error_after_begin_remain_paused(self):
        for params in ({"foreign_event":True},{"after_begin":True}):
            with self.subTest(params=params), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                tracer,tid,folder = self.batch_failure(root,**params)
                self.assertFalse(reconcile_readonly_pause(root))
                if params.get("after_begin"):
                    marker = folder/"trace-paused.json"
                    self.assertNotIn("uncommitted_read_batch",json.loads(marker.read_bytes()))
                    # A copied marker coordinate cannot reclassify an error that occurred later.
                    end = next(e for e in tracer.get_events(tid) if e["name"]=="workbench_loop.recheck_dependency")
                    marker.write_text(json.dumps({"reason":"trace_incomplete","watcher_pid":os.getpid(),
                        "uncommitted_read_batch":{"trace_id":tid,"span_id":end["span_id"],"store_id":tracer.store_id}}))
                    self.assertFalse(reconcile_readonly_pause(root))

    def interrupted(self, root, *, event_name=None):
        tracer = get_tracer(root/"outputs/traces")
        folder = watch._folder(root)
        folder.mkdir(parents=True)
        (folder/"instance.json").write_text(json.dumps({"pid":os.getpid()}), encoding="utf-8")
        with tracer.storage_session(), use_context(None,tracer=tracer):
            with self.assertRaises(TraceError):
                with tracer.trace("workbench.watch.run") as driver, tracer.span("cli.workbench_loop_watch"):
                    reader = tracer.start_span("workbench_loop.recheck_dependency")
                    if event_name:
                        tracer.event(event_name,{},context=reader.context,kind="http")
                    else:
                        with tracer.span("studio.assets.catalog",parent=reader.context):
                            tracer.event("studio.assets.catalog.read",{})
                    with tracer._db() as db:
                        db.execute("PRAGMA busy_timeout=10")
                    with closing(sqlite3.connect(tracer.path)) as occupied:
                        occupied.execute("BEGIN IMMEDIATE")
                        try:
                            tracer.end_span(reader.context)
                        except TraceError as exc:
                            watch.mark_trace_pause(root,exc)
                            raise
        return tracer,driver.trace_id,folder

    def test_known_unwritten_read_is_archived_once_in_an_independent_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracer,tid,folder = self.interrupted(root)
            raw = (folder/"trace-paused.json").read_bytes()
            before = tracer.get_events(tid)
            self.assertTrue(reconcile_readonly_pause(root))
            self.assertFalse(reconcile_readonly_pause(root))
            self.assertEqual(next(folder.glob("trace-paused-*.json")).read_bytes(),raw)
            self.assertEqual(tracer.get_events(tid),before)
            with closing(sqlite3.connect(tracer.path)) as db:
                receipt, = db.execute("SELECT DISTINCT trace_id FROM events WHERE name='workbench.watch.archive_pause.response'").fetchone()
            self.assertNotEqual(receipt,tid)
            self.assertTrue(tracer.verify(receipt)["ok"])

    def test_unknown_effect_and_legacy_marker_remain_paused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _,_,folder = self.interrupted(root,event_name="http.request")
            self.assertFalse(reconcile_readonly_pause(root))
            path = folder/"trace-paused.json"
            self.assertTrue(path.is_file())
            path.write_text('{"reason":"trace_incomplete","at":1}',encoding="utf-8")
            self.assertFalse(reconcile_readonly_pause(root))
            self.assertTrue(path.is_file())
