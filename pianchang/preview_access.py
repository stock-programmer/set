"""Acceptance gate for whole-film delivery, exports and published media."""
from pathlib import Path
from types import SimpleNamespace
from contextlib import nullcontext

from pianchang.story_spec import digest_json
from trace_system import TraceError,event,traced


class PreviewBlocked(ValueError):
    def __init__(self, gate):
        self.gate=gate
        super().__init__(gate["reason"])


@traced("preview.access.evaluate",kind="validation",capture_inputs=False)
def preview_status(service,pid):
    from pianchang.app_service import read_json
    from pianchang.studio_assets import studio_project_for_video
    checks=[];allowed={};preview=None;view={};package=None;issues=[]
    def check(path,expected,actual):
        row={"path":path,"expected":expected,"actual":actual,"status":"PASS" if expected==actual else "FAIL"}
        checks.append(row);event("preview.access.check",row,kind="validation")
        return row["status"]=="PASS"
    try:
        view=studio_project_for_video(service,pid,include_delivery=True)
        package=view.pop("published_delivery")
        check("production.published",True,bool(view.get("production_ref")))
        check("script.pending_revision",None,view.get("pending_script_ref"))
        # Read task state directly; building the human activity feed also loads
        # historical diagnostics and must not be part of each video range read.
        with getattr(service,"lock",nullcontext()):
            raw_jobs=getattr(service,"state",{}).get("jobs")
            jobs=list(raw_jobs) if raw_jobs is not None else service.list_jobs()
            active=[j["id"] for j in jobs if j.get("project_id")==pid and j.get("kind") in
                {"studio_video","studio_review","asset_production","asset_regenerate"} and j.get("status") in {"queued","running"}]
            if package and package.get("manual_approvals"):
                from pianchang.shot_approval import active_production
                active = [j["id"] for j in jobs if active_production([j], pid, package["request_id"])]
        check("production.active_changes",[],active)
        if view.get("production_ref"):
            from pianchang.studio_recovery import read_incomplete_production
            check("production.unfinished_commission",False,bool(read_incomplete_production(view)))
        else:
            # A missing delivery already closes this gate. Do not hash every
            # unfinished candidate merely to make that same rejection again.
            row={"path":"production.unfinished_commission","expected":False,"actual":None,"status":"UNKNOWN",
                 "execution_status":"blocked_by_prerequisite","reason":"尚无发布的制作交付，整片预览待解锁。"}
            checks.append(row);event("preview.access.check",row,kind="validation")
        if view.get("production_ref"):
            check("production.media_accepted",True,package.get("media_accepted"))
            check("production.project",digest_json(view["project"]),digest_json(package["project"]))
            check("production.script_ref",view.get("script_ref"),package.get("script_ref"))
            if package.get("spec_ref") is None:
                # Older worker packages pinned the exact specification as an immutable dependency.
                check("production.spec_dependency", [{k:view["spec"][k] for k in ("asset_id","revision_id")}],
                      view.get("published_spec_dependencies", []))
            else:
                check("production.spec_ref",view.get("spec"),package["spec_ref"])
            # Run the existing exact validator against immutable receipts. No
            # Agent, model, repair operation or renderer is started by this read.
            evidence=SimpleNamespace(project=package["project"],shots=package["shots"],reports=package["reports"])
            if package.get("manual_approvals"):
                from pianchang.shot_approval import read_approvals
                from pianchang.studio_recovery import _work
                evidence.request_id = package["request_id"]
                decisions = read_approvals(_work(view, evidence.request_id), evidence.project, evidence.shots, evidence.reports, project_id=pid)
                check("production.manual_approvals", package["manual_approvals"], decisions)
                evidence.manual_approvals = decisions
            from pianchang.video_collab_agent import VideoWorkspace
            acceptance=VideoWorkspace.acceptance_gate(evidence)
            issues=acceptance["issues"]
            check("production.required_checks",True,acceptance["passed"])
            preview=package["preview"]
            check("preview.file_sha256",preview["preview_sha256"],service._hash(preview["preview_path"]))
            manifest=read_json(preview["manifest_path"])
            check("preview.manifest_sha256",preview.get("export_manifest_sha256"),digest_json(manifest))
            binding=[(s["id"],package["shots"][s["id"]]["media_sha256"]) for s in view["project"]["shots"]]
            check("preview.selected_media",binding,[(c["shot_id"],c["sha256"]) for c in manifest["clips"]])
            allowed[str(Path(preview["preview_path"]).resolve())]=preview["preview_sha256"]
            for sid,clip in package["shots"].items():
                allowed[str(Path(clip["media_path"]).resolve())]=clip["media_sha256"]
                report=read_json(package["reports"][sid]["report_path"])
                for row in report["results"]:
                    for item in row.get("evidence",[]):
                        if item.get("path") and item.get("sha256") and Path(item["path"]).suffix.lower()==".mp4":
                            allowed[str(Path(item["path"]).resolve())]=item["sha256"]
    except TraceError:raise
    except (OSError,ValueError,KeyError,TypeError) as exc:
        check("production.evidence_readable",True,False)
        issues.append({"criterion_id":"evidence_integrity","status":"UNKNOWN","reason":str(exc)})
    ready=bool(checks) and all(c["status"]=="PASS" for c in checks)
    return {"ready":ready,"status":"ready_for_preview" if ready else "production_incomplete",
        "reason":"全部镜头及衔接必过项通过，可预览整片。" if ready else "必过验收或当前版本交付尚未完成。可在视频制作页预览已生成的镜头并查看验收记录。",
        "project_id":pid,"production_ref":view.get("production_ref"),"script_ref":view.get("script_ref"),"spec_ref":view.get("spec"),
        "request_id":package.get("request_id") if ready else None,
        "total":len(view.get("project",{}).get("shots",[])),"checks":checks,"issues":issues,
        "preview":preview if ready else None,"allowed_media":allowed if ready else {}}


@traced("preview.access.require",kind="validation",capture_inputs=False)
def require_preview(service,pid,*,path=None,sha256=None):
    gate=preview_status(service,pid)
    if not gate["ready"]:raise PreviewBlocked(gate)
    if path is not None:
        expected=gate["allowed_media"].get(str(Path(path).resolve()))
        valid=bool(expected and (sha256 is None or sha256==expected) and service._hash(path)==expected)
        event("preview.access.media_binding",{"path":str(path),"expected":expected,"actual":sha256,"status":"PASS" if valid else "FAIL"},kind="validation")
        if not valid:raise PreviewBlocked({**gate,"ready":False,"reason":"该视频不属于当前已验收版本，或文件已变更，请重新验收。"})
    return gate


class ReadOnlyPreviewSource:
    """For the independent progress server; never opens an AppService worker."""
    def __init__(self,root,state_dir=None):
        self.root=Path(root);self.state_dir=Path(state_dir or self.root/"outputs/web")

    def _project(self,pid):
        from pianchang.app_service import project_id_for_path,read_json
        paths=[self.root/"fixtures/story_project.json",self.root/"outputs/stories/last-key-step2-final/project.json"]
        paths.extend(self.state_dir.glob("stories/*/project.json"))
        path=next((p for p in paths if p.is_file() and project_id_for_path(p)==pid),None)
        if path is None:raise KeyError("工程不存在")
        return path,read_json(path)

    def list_jobs(self):
        from pianchang.app_service import read_json
        return read_json(self.state_dir/"state.json")["jobs"]

    def _hash(self,path):
        from pianchang.acceptance import file_sha256
        return file_sha256(path)
