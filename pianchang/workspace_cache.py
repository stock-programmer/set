"""Bounded read projections. Commands and positive media authorization never use this cache."""
import copy
import re
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

from pianchang.story_spec import digest_json
from trace_system import current_context,event,traced


def _stat(path):
    try:
        row=Path(path).stat();return [row.st_dev,row.st_ino,row.st_size,row.st_mtime_ns,row.st_ctime_ns]
    except FileNotFoundError:return None


def _conversation_signature(registry, story_id):
    if not story_id or not registry.is_file():
        return None
    with closing(sqlite3.connect(registry.resolve().as_uri()+"?mode=ro",uri=True)) as db:
        db.execute("BEGIN")
        rows=db.execute("SELECT * FROM sessions WHERE story_id=? ORDER BY role",(story_id,)).fetchall()
        messages=db.execute("SELECT m.* FROM messages m JOIN sessions s ON s.id=m.session_id WHERE s.story_id=? ORDER BY m.created_at,m.rowid",(story_id,)).fetchall()
    return digest_json([rows,messages])


def _signatures(service, pid, dependencies):
    """Sample common inputs once, including every project shown in the sidebar."""
    scoped={"selections","reviews","exports","asset_work","asset_auto_dispatch","review_confirmations"}
    with service.lock:
        state={k:(v.get(pid) if k in scoped and isinstance(v,dict) else v) for k,v in service.state.items() if k!="jobs"}
        sidebar_fields = ("id", "kind", "project_id", "story_job_id", "status", "updated_at")
        state["jobs"] = []
        for job in service.state["jobs"]:
            if job.get("project_id") == pid:
                state["jobs"].append(job)
                continue
            fields = sidebar_fields + ("run_id", "plan_id", "message_id", "message") if job.get("kind") == "studio_video" else sidebar_fields
            state["jobs"].append({field: job.get(field) for field in fields})
        state_hash=digest_json(state)
    records=service._discover()
    if pid not in records:
        raise KeyError("工程不存在。")
    source=records[pid]["path"]
    store=service.root/"outputs/collaborations/assets.sqlite3"
    registry=service.root/"outputs/pi/registry.sqlite3"
    common={str(record["path"]) for record in records.values()}
    common.update(str(p) for p in [source.parent/"studio.json",service.root/".env",store,Path(str(store)+"-wal")])
    common.update(str(p) for p in Path(service.video_config.output_dir).glob("*/job.json"))
    files=common.union(*(paths for paths, _ in dependencies))
    stats={path:_stat(path) for path in files}
    conversations={story:_conversation_signature(registry,story) for story in {story for _,story in dependencies}}
    return [digest_json([state_hash, {path:stats[path] for path in common.union(paths)}, conversations[story]])
            for paths, story in dependencies]


def _paths(service,value):
    result=set();store=service.root/"outputs/collaborations"
    for project in value.get("projects", []):
        job = (project.get("opening") or {}).get("video_job") or {}
        run, message = job.get("run_id"), job.get("message_id")
        if all(isinstance(item, str) and re.fullmatch(r"[0-9a-f]{32}", item) for item in (run, message)):
            work = store / "runs" / run / ("video-spec-" + message)
            result.update(str(path) for path in (work / "clips.json", work / "agent/run.json"))
    roots=[service.root/"outputs",service.state_dir,Path(service.video_config.output_dir)]
    def walk(item):
        if isinstance(item,dict):
            media=item.get("media_sha256")
            if isinstance(media,str) and re.fullmatch(r"[a-f0-9]{64}",media):result.add(str(store/"reference-blobs"/media[:2]/media))
            for key,child in item.items():
                if isinstance(child,str):
                    if key=="path" or key.endswith("_path"):
                        candidate=Path(child)
                        if candidate.is_absolute() and any(candidate.is_relative_to(root) for root in roots) and not candidate.is_relative_to(service.root/"outputs/traces"):result.add(str(candidate))
                    if child.startswith("/assets/"):
                        asset=service.assets.get(child.rsplit("/",1)[-1])
                        if asset:result.add(str(asset[0]))
                walk(child)
        elif isinstance(item,list):
            for child in item:walk(child)
    walk(value);return result


@traced("workbench.snapshot.cache",kind="validation",capture_inputs=False,capture_output=False)
def cached_workspace(service,pid,build=None):
    with service.lock:
        if not hasattr(service,"_workspace_reads"):service._workspace_reads={};service._workspace_read_locks={}
        lock=service._workspace_read_locks.get(pid)
        if lock is None:
            if pid not in service._discover():
                raise KeyError("工程不存在。")
            lock=service._workspace_read_locks[pid]=threading.Lock()
    with lock:
        cached=service._workspace_reads.get(pid)
        if cached is None and build is None:
            return None
        story_id=cached["data"]["studio"].get("story_id") if cached else None
        previous_dependencies=(cached["paths"] if cached else (),story_id)
        before, = _signatures(service, pid, [previous_dependencies])
        if cached and before==cached["basis"]:
            event("workbench.snapshot.reused",{"project_id":pid,"source_context":cached["context"],"snapshot_sha256":cached["sha256"],
                "path":"workspace.read_projection.inputs","expected":cached["basis"],"actual":before,"status":"PASS",
                "scope":"read-only display reuse; no commands or positive media authorization"},kind="cache")
            return copy.deepcopy(cached["data"])
        if build is None:
            return None
        data=build(pid)
        paths=_paths(service,data)
        # Only a closed preview gate is reusable. Any actual preview permission
        # is evaluated live, and serving media always uses the unchanged gate.
        next_dependencies=(paths,data["studio"].get("story_id"))
        after, next_basis = _signatures(service, pid, [previous_dependencies,next_dependencies])
        stable=after==before
        if stable and not data["studio"]["preview_gate"]["ready"]:
            service._workspace_reads[pid]={"basis":next_basis,"paths":paths,"context":current_context(),
                "sha256":digest_json(data),"data":copy.deepcopy(data)}
            while len(service._workspace_reads)>8:service._workspace_reads.pop(next(iter(service._workspace_reads)))
        event("workbench.snapshot.built",{"project_id":pid,"inputs_stable":stable,"reusable_closed_gate":not data["studio"]["preview_gate"]["ready"]},kind="cache")
        return data
