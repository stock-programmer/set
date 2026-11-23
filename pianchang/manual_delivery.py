"""Finish an existing commission using its reviewed clips and local FFmpeg export."""
from pathlib import Path
from uuid import uuid4

from pianchang.collaboration_workflow import Collaboration, open_ref, ref
from pianchang.story_spec import digest_json
from pianchang.studio_recovery import _read, _work
from pianchang.video_collab_agent import VideoWorkspace
from trace_system import TraceError, event, traced


def human_reviewable_criteria(contract):
    from pianchang.content_review import CONTENT_KINDS
    kinds = CONTENT_KINDS - {"sequence_review"}
    return {r["criterion_id"] for r in contract["requirements"]
            if r["required"] and r["kind"] in kinds and not r["criterion_id"].startswith("continuity:")}


def adjacent_cut_criteria(project, sid):
    order = [s["id"] for s in project["shots"]]
    return {f"continuity:{left}->{right}" for left, right in zip(order, order[1:]) if sid in (left, right)}


@traced("studio.manual_delivery.finish", kind="task", capture_inputs=False)
def finish_if_approved(service, view, request_id, shots, reports):
    from pianchang.shot_approval import read_approvals, selected_reviews
    from pianchang.studio_assets import studio_project_for_video
    from pianchang.studio_recovery import read_incomplete_production
    from pianchang.studio_retakes import _latest_request

    work = _work(view, request_id)
    project = view["project"]
    approvals = read_approvals(work, project, shots, reports, project_id=view["project_id"])
    if not approvals:
        raise ValueError("找不到与当前镜头和验收报告匹配的人工通过记录。")
    workspace = VideoWorkspace(script=project["script_source"]["script"], script_ref=view["script_ref"],
        directory=work, text_config=None, video_config=None, template_project=project, previous=None,
        decision_client=None, vision_config=None, progress=None)
    workspace.project, workspace.shots, workspace.reports = project, shots, reports
    workspace.manual_approvals, workspace.request_id = approvals, request_id
    try:
        gate = workspace.acceptance_gate()
    except TraceError:
        raise
    except (ValueError, OSError, KeyError, TypeError) as exc:
        # The decision about this shot is durable even if another shot's evidence is unavailable.
        issue = {"criterion_id":"evidence_integrity", "status":"UNKNOWN", "reason":str(exc)}
        event("studio.manual_delivery.evidence_unavailable", {"request_id":request_id, **issue}, kind="validation")
        return {"delivered": False, "production_ref": None, "issues": [issue]}
    if not gate["passed"]:
        return {"delivered": False, "production_ref": None, "issues": gate["issues"]}
    workspace.impact = _read(work / "impact.json")
    order = [s["id"] for s in project["shots"]]
    workspace.impact["recheck_edges"] = [list(edge) for edge in zip(order, order[1:])]
    selection = digest_json([(sid, shots[sid]["media_sha256"]) for sid in order])
    preview_dir = work / "manual-previews" / selection[:24]
    if preview_dir.exists() and not (preview_dir / "export.json").is_file():
        # An interrupted local render can leave a lock or incomplete output.
        # Preserve it for inspection while rebuilding from the same selected clips.
        preview_dir = preview_dir.with_name(selection[:24] + "-" + uuid4().hex)
    workspace._export(preview_dir)
    if not workspace.finish_check()["complete"]:
        raise ValueError("当前镜头、验收报告或导出文件在剪辑过程中发生变更。")
    session = Collaboration(Path(view["store_path"]).parent, view["run_id"])
    with session.store.transaction():
        current = studio_project_for_video(service, view["project_id"])
        latest, _ = _latest_request(current)
        if (not latest or latest["id"] != request_id or current.get("pending_script_ref")
                or any(current[k] != view[k] for k in ("spec", "script_ref", "production_ref", "project"))):
            raise ValueError("制作委托或版本已变更，请刷新后重新审看。")
        snapshot = read_incomplete_production(current)
        if not snapshot or snapshot["active"]:
            raise ValueError("制作已恢复或已更新，请刷新后审看当前候选。")
        current_reports = selected_reviews(current, snapshot)
        for sid in order:
            entry = snapshot["available"].get(sid, {})
            candidate = entry.get("clip") or entry.get("observation") or {}
            if (candidate.get("media_sha256") != shots[sid]["media_sha256"]
                    or current_reports.get(sid, {}).get("report_sha256") != reports[sid]["report_sha256"]):
                raise ValueError("剪辑期间选用镜头或报告已变更：" + sid)
        request = session.store.get_message(request_id)
        previous = open_ref(session.store, view["production_ref"]) if view["production_ref"] else None
        script = open_ref(session.store, view["script_ref"])
        production = {"schema_version":"video-production/1", "request_id":request_id, "project":project,
            "script_ref":view["script_ref"], "spec_ref":view["spec"], "impact":workspace.impact, "shots":shots,
            "reports":reports, "manual_approvals":approvals, "preview":workspace.preview, "media_accepted":True,
            "edge_reviews":workspace.edge_reviews(), "creative_review":"UNKNOWN",
            "repair_history":_read(work / "repair-history.json", {}).get("history", []),
            "reference_specs":session.lock_shot_references(project, script, previous, request)}
        package = session.register_delivery(production, script, previous, request)
        payload = {"production":ref(package), "script":ref(script), "media_accepted":True,
                   "regenerated":workspace.impact["regenerate"], "reused":workspace.impact["reuse"]}
        result = session.store.send("production.result", sender="video", recipient="script", category="result",
            story_id=view["story_id"], payload=payload, correlation_id=view["run_id"], causation_id=request_id)
        session.store.ack("video", request_id, "completed", recovery=True, result={"message_id": result["message_id"], **payload})
    event("studio.manual_delivery.published", {"request_id":request_id, "production":ref(package),
          "approved_shots":list(approvals), "selected_shots":order, "new_video_submissions":0}, kind="state")
    return {"delivered": True, "production_ref": ref(package), "issues": []}
