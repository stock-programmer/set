"""Resume a paused reader only after independently checking its durable Trace."""
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from trace_system import event, get_tracer, use_context


def _only_catalog_reads(sid, operations):
    active = {sid:("workbench_loop.recheck_dependency",None)}
    seen = {sid}
    for span, parent, kind, name, status in operations:
        if (kind == "span.start" and parent in active and span not in seen and re.fullmatch("[0-9a-f]{32}",span)
                and name in {"studio.assets.catalog","studio.assets.work_state"}):
            active[span] = (name,parent)
            seen.add(span)
        elif kind == "span.end" and span != sid and active.get(span) == (name,parent) and status == "OK":
            del active[span]
        elif (active.get(span) == ("studio.assets.catalog",parent) and (kind,name) in {
                ("validation","studio.assets.check"), ("event","studio.assets.catalog.read")}):
            continue
        else:
            return False
    return set(active) == {sid}


def _readonly_tail(db, marker, ref, tracer):
    tid, sid = ref["trace_id"], ref["span_id"]
    start = db.execute("SELECT trace_seq,parent_span_id,data FROM events WHERE trace_id=? AND span_id=? "
                       "AND name='workbench_loop.recheck_dependency' AND kind='span.start'", (tid,sid)).fetchone()
    if not start:
        return False
    seq, cli, payload = start
    origin = json.loads(payload)
    if origin.get("process",{}).get("pid") != marker["watcher_pid"]:
        return False
    batch = "uncommitted_read_batch" in marker
    if batch:
        attrs = tracer.resolve(origin.get("attributes")) or {}
        if (origin.get("kind") != "validation" or attrs.get("module") != "tools.workbench_loop_dependency"
                or attrs.get("qualname") != "recheck_dependency" or attrs.get("process_id") != marker["watcher_pid"]
                or not re.fullmatch("[0-9a-f]{64}",str(attrs.get("loaded_code_sha256")))):
            return False
    control = db.execute("SELECT parent_span_id,data FROM events WHERE trace_id=? AND span_id=? "
                         "AND name='cli.workbench_loop_watch' AND kind='span.start'", (tid,cli)).fetchone()
    if not control or json.loads(control[1]).get("process",{}).get("pid") != marker["watcher_pid"]:
        return False
    root = control[0]
    root_start = db.execute("SELECT data FROM events WHERE trace_id=? AND span_id=? AND parent_span_id IS NULL "
                            "AND name='workbench.watch.run' AND kind='span.start'",(tid,root)).fetchone()
    if not root_start or json.loads(root_start[0]).get("process",{}).get("pid") != marker["watcher_pid"]:
        return False
    tail = db.execute("SELECT span_id,parent_span_id,kind,name,data FROM events WHERE trace_id=? "
                      "AND trace_seq>? ORDER BY trace_seq LIMIT 1025", (tid,seq)).fetchall()
    if not 2 <= len(tail) <= 1024:
        return False
    endings = [(cli,root,"span.end","cli.workbench_loop_watch"), (root,None,"span.end","workbench.watch.run")]
    if batch:
        if len(tail) != 3:
            return False
        endings.insert(0,(sid,cli,"span.end","workbench_loop.recheck_dependency"))
    for row, expected in zip(tail[-len(endings):], endings):
        data = json.loads(row[4])
        err = tracer.resolve(data.get("error")) or {}
        if (tuple(row[:4]) != expected or data.get("status") != "ERROR" or err.get("type") != "TraceError"
                or data.get("output") is not None or err.get("message") != "Trace database operation failed: OperationalError [SQLITE_BUSY]"):
            return False
    if batch:
        operations = tracer.resolve(json.loads(tail[0][4]).get("read_batch_before_begin"))
        if not isinstance(operations,list) or not 1 <= len(operations) <= 1024:
            return False
    else:
        operations = [(span,parent,kind,name,json.loads(raw).get("status") if kind == "span.end" else None)
                      for span,parent,kind,name,raw in tail[:-2]]
    return _only_catalog_reads(sid, operations)


def reconcile_readonly_pause(root):
    """An independent supervisor calls this only after the affected watcher has exited."""
    from tools.workbench_loop_watch import _folder, _lock
    folder = _folder(root)
    marker_file = folder / "trace-paused.json"
    with _lock(folder/"watch.lock",create=False) as free:
        if not free or not marker_file.is_file():
            return False
        try:
            raw = marker_file.read_bytes()
            marker = json.loads(raw)
            references = [marker[name] for name in ("unwritten_span_end","uncommitted_read_batch") if name in marker]
            if len(references) != 1:
                return False
            ref = references[0]
            if (marker.get("reason") != "trace_incomplete" or type(marker.get("watcher_pid")) is not int
                    or not all(re.fullmatch("[a-f0-9]{32}",str(ref.get(k))) for k in ("trace_id","span_id","store_id"))):
                return False
            if json.loads((folder/"instance.json").read_bytes())["pid"] != marker["watcher_pid"]:
                return False
        except (KeyError, AttributeError, TypeError, ValueError, OSError):
            return False
        tracer = get_tracer(Path(root)/"outputs/traces")
        if ref["store_id"] != tracer.store_id:
            return False
        try:
            with closing(sqlite3.connect(tracer.path.as_uri()+"?mode=ro",uri=True,timeout=2)) as db:
                if not _readonly_tail(db,marker,ref,tracer):
                    return False
        except (sqlite3.Error,KeyError,TypeError,ValueError):
            return False
        with use_context(None,tracer=tracer), tracer.trace("workbench.watch.reconcile_trace_pause",
                inputs={"watcher_pid":marker["watcher_pid"]}, links=[{**ref,"relation":"rechecks"}]):
            integrity = tracer.verify(ref["trace_id"])
            if not integrity["ok"] or marker_file.read_bytes() != raw:
                return False
            digest = hashlib.sha256(raw).hexdigest()
            archive = folder / ("trace-paused-" + digest + ".json")
            event("workbench.watch.archive_pause.request", {"marker_sha256":digest, "original":ref,
                  "checked_events":integrity["checked_events"], "operation":"restart_read_only_observation"}, kind="filesystem")
            if archive.exists():
                return False
            try:
                marker_file.rename(archive)
            except OSError as exc:
                event("workbench.watch.archive_pause.error", {"marker_sha256":digest,"type":type(exc).__name__},
                      kind="filesystem")
                return False
            event("workbench.watch.archive_pause.response", {"marker_sha256":digest, "archived":True},kind="filesystem")
        return True
