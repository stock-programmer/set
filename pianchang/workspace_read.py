"""One actual read snapshot for the homepage; never starts model/media work."""
from pi_bridge.service import get_sessions
from pianchang.studio_flow import studio_flow
from trace_system import event,traced


@traced("workbench.snapshot.read",kind="tool",capture_inputs=False,capture_output=False)
def workspace_snapshot(service,pid=None,*,cached_only=False):
    from pianchang.workspace_cache import cached_workspace
    if not pid:pid=next(iter(service._discover()),None)
    if cached_only:
        return cached_workspace(service,pid) if pid else None
    return cached_workspace(service,pid,lambda selected:_build_workspace(service,selected)) if pid else _build_workspace(service,None)


@traced("workbench.snapshot.build",kind="tool",capture_inputs=False,capture_output=False)
def _build_workspace(service,pid):
    sessions=get_sessions(service)
    with service.tracer.read_batch():
        bootstrap=service.bootstrap(include_jobs=False)
        if not pid and bootstrap["projects"]:pid=bootstrap["projects"][0]["id"]
        if not pid:return {**bootstrap,"jobs":service.list_jobs(),"project":None,"studio":None,"conversations":{}}
        if pid not in {p["id"] for p in bootstrap["projects"]}:raise KeyError("工程不存在。")
        bootstrap.pop("jobs_pending",None)
        bootstrap.update(jobs=service.list_jobs(detail_project=pid),jobs_detail_project=pid)
        project=service.project_detail(pid)
        studio=studio_flow(service,pid,denied_gate=project["preview_gate"],jobs=bootstrap["jobs"])
        for key in ("ready","production_ref","script_ref","spec_ref"):
            expected=project["preview_gate"].get(key);actual=studio["preview_gate"].get(key)
            event("workbench.snapshot.check",{"path":"snapshot.preview_gate."+key,"expected":expected,"actual":actual,
                "status":"PASS" if expected==actual else "FAIL"},kind="validation")
            if expected!=actual:raise ValueError("工程版本正在更新，请重新读取当前工作区。")
        source=next((j["id"] for j in bootstrap["jobs"] if j["id"]==studio["run_id"] and j["kind"] in {"story","studio_script"}),None)
        found={role:sessions.repo.find(studio["story_id"],role) for role in ("script","video")}
        missing=[role for role,row in found.items() if row is None or row["project_id"]!=pid or (source and not row["source_job"])]
        conversations={role:sessions.resolved_view(row) for role,row in found.items() if role not in missing}
    # Creation/linking is a real metadata write and needs a durable trace first.
    if missing:
        for role in missing:
            found[role]=sessions.repo.ensure(studio["story_id"],role,source_job=source,project_id=pid)
        with service.tracer.read_batch():
            for role in missing:
                conversations[role]=sessions.resolved_view(found[role])
    return {**bootstrap,"project":project,"studio":studio,"conversations":conversations}
