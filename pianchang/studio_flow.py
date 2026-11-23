"""Read the selected project's script, shared inputs, commissions and delivery evidence."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from pianchang.collab_store import CollabStore
from pianchang.story_spec import digest_json, script_structure
from pianchang.studio_assets import studio_asset_catalog
from trace_system import event, traced


def _artifact(service, path, sha=None, pid=None):
    if not path:
        return None
    try:
        return service.asset(path, expected_sha256=sha,project_id=pid) if Path(path).suffix.lower()==".mp4" else service.asset(path, expected_sha256=sha)
    except (OSError, ValueError) as exc:
        event("studio.artifact.unavailable", {"path": str(path), "expected": sha,
              "actual": str(exc), "status": "FAIL"}, kind="validation")
        return None


def _read_evidence(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        event("studio.evidence.unavailable", {"path": str(path), "expected": "readable JSON",
              "actual": str(exc), "status": "FAIL"}, kind="validation")
        return None


@traced("studio.video.detail", kind="tool", capture_inputs=False, capture_output=False)
def studio_video_detail(service, pid):
    """Read the selected story's video controls without opening attached review evidence."""
    from pianchang.studio_assets import studio_project_for_video
    from pianchang.video_execution import current_commission

    view = studio_project_for_video(service, pid, include_delivery=True)
    project = view["project"]
    delivery = view.pop("published_delivery")
    fields = ("id", "kind", "project_id", "run_id", "message_id", "commission_id", "commission_job_id",
              "status", "created_at", "updated_at", "message", "trace_id", "shot_id", "story_job_id",
              "plan_id", "cancel_requested")
    jobs = []
    source_jobs = []
    with service.lock:
        for job in service.state["jobs"]:
            result = job.get("result") or {}
            project_id = job.get("project_id") or (result.get("project_id") if isinstance(result, dict) else None)
            if project_id != pid:
                continue
            source_jobs.append({**job, "project_id":pid})
            row = {key: job[key] for key in fields if key in job}
            row["project_id"] = pid
            error = job.get("error")
            if isinstance(error, dict):
                error = {key: error[key] for key in ("message", "error") if isinstance(error.get(key), str)}
            if error:
                row["error"] = error
            if isinstance(result, dict):
                summary = {key: result[key] for key in ("status", "loop_status", "task_complete", "turn_complete",
                           "action_required", "summary", "report_url") if isinstance(result.get(key), (str, bool))}
                proposal = result.get("extra_retake")
                if isinstance(proposal, dict) and proposal.get("proposal_id"):
                    summary["extra_retake"] = {"proposal_id": proposal["proposal_id"]}
                    if job.get("message_id") and isinstance(proposal.get("partial"),dict) and proposal["partial"].get("request_id")==job["message_id"]:
                        summary["extra_retake"]["partial"] = {"request_id":job["message_id"]}
                if summary:
                    row["result"] = summary
            jobs.append(row)
    execution = current_commission(service, pid, view=view, jobs=source_jobs)
    plan, error = None, None
    if not execution["commission_id"]:
        from pianchang.studio_jobs import studio_video_plan
        previous = {**view["production_ref"], "content": delivery} if view["production_ref"] else None
        try:
            if view.get("pending_script_ref"):
                raise ValueError("新版剧本已发布，请先更新制作规格。")
            plan = studio_video_plan(view, previous)
        except (ValueError, KeyError) as exc:
            error = str(exc)
            event("studio.video_plan.unavailable", {"path": "video_plan", "expected": "可计算拍摄计划的制作规格",
                  "actual": error, "status": "FAIL"}, kind="validation")
    assets = {key: view[key] for key in ("spec", "script_ref", "production_ref", "pending_script_ref")}
    assets["shots"] = [{"id": shot["id"], "description": shot.get("description", ""),
                       "required_refs": [{key: pin[key] for key in ("asset_id", "revision_id", "sha256") if key in pin}
                                         for pin in shot.get("required_refs", [])]} for shot in project["shots"]]
    return {"projection": "video", "project_id": pid, "title": project["title"], "story_id": view["story_id"],
            "run_id": view["run_id"], "project_path": view["project_path"], "assets": assets,
            "video_execution": execution, "video_plan": plan, "video_plan_error": error, "jobs": jobs[::-1]}


def _read_records(catalog):
    store = Path(catalog["store_path"])
    scripts, packages, events = [], [], []
    if store.is_file():
        story = catalog["story_id"]
        with closing(sqlite3.connect(store.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            for kind, result in (("script", scripts), ("production", packages)):
                for row in db.execute("SELECT a.*, m.causation_id FROM assets a LEFT JOIN messages m ON m.message_id=a.event_id "
                                      "WHERE a.story_id=? AND a.asset_id=? AND a.kind=? ORDER BY a.seq DESC",
                                      (story, story + "/" + kind, kind)):
                    result.append(CollabStore._asset(db, row))
            for row in db.execute("SELECT * FROM messages WHERE story_id=? AND correlation_id=? ORDER BY seq DESC",
                                  (story, catalog["run_id"])):
                message = CollabStore._message(db, row)
                payload = message["payload"]
                if message["message_type"] == "asset.revision_published" and payload.get("kind") != "script":
                    continue
                script = payload.get("script") or payload.get("base")
                if payload.get("kind") == "script":
                    script = {k: payload[k] for k in ("asset_id", "revision_id", "sha256")}
                events.append({"id": row["message_id"], "type": row["message_type"], "producer": row["sender"],
                               "consumer": row["recipient"], "deliveries": message["deliveries"],
                               "script_ref": script, "spec_ref": payload.get("template"), "original_request_id": payload.get("original_request_id"),
                               "previous_ref": payload.get("previous"), "production_ref": payload.get("production"),
                               "trace_id": message["trace_id"], "created_at": row["created_at"]})
    return scripts, packages, events


def _script_view(service, catalog, scripts, folder):
    ref = catalog["script_ref"]
    asset = next((s for s in scripts if ref and s["revision_id"] == ref["revision_id"] and s["sha256"] == ref["sha256"]), None)
    path = Path(catalog["project_path"])
    url = None
    if asset:
        script = asset["content"]
        published = folder / ("script-" + asset["revision_id"]) / "published-script.json"
        location = f'{catalog["store_path"]} · {asset["asset_id"]}@{asset["revision_id"]}'
        if published.is_file():
            copy = _read_evidence(published)
            actual = digest_json(copy) if copy is not None else None
            matched = actual == asset["sha256"]
            event("studio.script.file.check", {"path": str(published), "expected": asset["sha256"],
                  "actual": actual, "status": "PASS" if matched else "FAIL"}, kind="validation")
            if matched:
                location, url = str(published), _artifact(service, published)
    else:
        _, original = service._project(catalog["project_id"])
        script = original.get("script_source", {}).get("script") or {}
        location = str(path)
    shots_by_source = {}
    for shot in catalog["shots"]:
        for source in shot["source_refs"]:
            ids = shots_by_source.setdefault(source["source_id"], [])
            if shot["id"] not in ids:
                ids.append(shot["id"])
    beats = [{"id": beat["id"], "text": beat["text"], "shot_ids": shots_by_source.get(beat["id"], [])}
             for episode in script.get("episodes", []) for scene in episode["scenes"] for beat in scene["beats"]]
    return {"title": script.get("title"), "idea": script.get("idea"), "script_ref": ref,
            "revision_id": ref["revision_id"] if ref else None, "path": location, "url": url,
            "trace_id": asset["trace_id"] if asset else None, "beats": beats, "structure": script_structure(script)}


def _cut_reports(service, folder, package,pid=None):
    results = []
    for path in sorted((folder / "cut-reviews").glob("*/cut-review.json")):
        report = _read_evidence(path)
        if not report:
            continue
        ref = report.get("production", {})
        if (ref.get("asset_id"), ref.get("revision_id"), ref.get("sha256")) != (
                package["asset_id"], package["revision_id"], package["sha256"]):
            continue
        pair = " → ".join(s["id"] for s in report["shots"])
        clip = report.get("window", {})
        results.append({"name": pair + " 实片复查", "status": report["audiovisual_continuity"],
                        "detail": f'切点 {report["cut"]["preview_time_s"]} 秒 · 技术检查 {report["technical_status"]}。{report["review_scope"]}',
                        "path": str(path), "url": _artifact(service, path), "trace_id": report["trace_id"],
                        "clip_url": _artifact(service, clip.get("path"), clip.get("sha256"),pid)})
    return results


def _delivery(service, package, folder, orders, pid=None):
    data = package["content"]
    impact, preview = data["impact"], data["preview"]
    shots = []
    for sid, clip in data["shots"].items():
        # Older candidates have no provider receipt. The distinction from an observed empty list matters.
        shots.append({"id": sid, "status": "复用" if sid in impact["reuse"] else "生成",
                      "media_ref": data["media_refs"].get(sid), "path": clip["media_path"],
                      "url": _artifact(service, clip["media_path"], clip["media_sha256"],pid),
                      "actual_refs": clip.get("submitted_refs"), "receipt_status": (clip.get("reference_receipt") or {}).get("status")})
    checks = [{"name": sid + " 镜头验收", "status": report["status"], "path": report["report_path"],
               "human_approval": (data.get("manual_approvals") or {}).get(sid),
               "url": _artifact(service, report["report_path"], report.get("report_sha256")),
               "detail": "待核对 " + "、".join(report["unresolved"]) if report["unresolved"] else "已完成记录中的检查"}
              for sid, report in data["reports"].items()]
    checks.extend(_cut_reports(service, folder, package,pid))
    # Take the spec from the causal commission, never infer it from today's editing copy.
    order = next((e for e in orders if e["id"] == package.get("causation_id")), None)
    if order and order["original_request_id"]:
        order = next((e for e in orders if e["id"] == order["original_request_id"]), order)
    spec = data.get("spec_ref") or (order["spec_ref"] if order else None)
    return {"revision_id": package["revision_id"], "script_ref": data["script_ref"], "spec_ref": spec,
            "previous_ref": order.get("previous_ref") if order else None,
            "affected": impact["regenerate"], "reused": impact["reuse"], "changed_sources": impact["changed_sources"],
            "recheck_edges": impact["recheck_edges"], "media_accepted": data["media_accepted"], "trace_id": package["trace_id"],
            "preview_path": preview["preview_path"], "preview_url": _artifact(service, preview["preview_path"], preview["preview_sha256"],pid),
            "shots": shots, "checks": checks}


def _stages(catalog, script, productions, work):
    last = productions[0] if productions else None
    entities, shots = catalog["entities"], catalog["shots"]
    refs = sum(len(e["references"]) for e in entities)
    pinned = sum(bool(s["required_refs"]) for s in shots)
    store = catalog["store_path"]
    statuses = [
        ("script", "剧本 Agent", script["revision_id"] or "工程原稿",
         [script["idea"] or "已有剧本或镜头工程"], [f'{len(script["beats"])} 个节拍 · {len(shots)} 个镜头'],
         [script["path"], catalog["project_path"]], "镜头和对象通过来源 ID 回指节拍或设定。"),
        ("assets", "资产制作", f'{len(entities)} 个对象 · {refs} 个媒体版本',
         ["剧本引文、对象设定、全剧风格与人工修改意见"],
         [f'{refs} 个媒体版本，{sum(bool(u.get("selected_pin") or u["status"] == "approved") for u in work["items"])} 项已选用，{pinned} 个镜头已绑定参考资产'],
         [store, str(Path(store).parent / "reference-blobs")], "Agent 自检通过后自动选用并绑定相关镜头。需处理的素材可在审看后人工通过，也可填写修改要求重新生成。"),
        ("video", "视频制作 Agent", "制作 " + last["revision_id"] if last else "待委托",
         ["指定版本的剧本、镜头规格、参考媒体和拍摄计划"], [f'{len(last["shots"])} 条镜头制作记录' if last else "待生成候选镜头"],
         [store] + ([s["path"] for s in last["shots"]] if last else []), "在同一 Agent 循环中自动验收、按证据修复并复验相邻衔接；全部必过项通过后导出。"),
        ("review", "人工预览", ("可人工预览" if last["media_accepted"] else "制作未完成") if last else "等待制作验收通过",
         ["选用的候选视频、相邻镜头与剧本验收项"], ["镜头报告与整集预览" if last else "待生成验收报告及预览"],
         [last["preview_path"]] if last else [], "按报告回到对应镜头修复，或将已验收版本交付。"),
    ]
    return [dict(zip(("id", "agent", "status", "inputs", "outputs", "storage", "next_use"), row)) for row in statuses]


@traced("studio.production.flow", kind="tool", capture_output=False)
def studio_flow(service, pid, *, denied_gate=None, jobs=None):
    from pianchang.asset_dispatch import asset_work_state
    from pianchang.asset_diagnostics import asset_diagnostic
    from pianchang.studio_jobs import studio_video_plan
    catalog = studio_asset_catalog(service, pid, include_project=True)
    project = catalog.pop("project")
    work = asset_work_state(service, pid, catalog)
    names = {e["id"]: e["name"] for e in catalog["entities"]}
    for item in work["items"]:
        if item["status"] != "needs_attention":
            continue
        diagnosis = asset_diagnostic(item, names.get(item["subject_id"]), span_id=item.get("error_span_id"))
        # Old reviews did not record a span ID. Resolve only the matching candidate in its original trace.
        if not diagnosis.get("span_id") and item.get("trace_id"):
            history = service.asset_diagnostics_for_trace(item["trace_id"]) or {}
            match = next((issue for issue in history.get("items", []) if issue.get("subject_id") == item["subject_id"]
                         and issue.get("purpose") == item["purpose"] and issue.get("candidate") == item.get("candidate")
                         and issue.get("trace_id") == item["trace_id"]), None)
            if match:
                diagnosis["span_id"] = match.get("span_id")
        item["diagnostic"] = diagnosis
    folder = Path(catalog["store_path"]).parent / "runs" / catalog["run_id"]
    scripts, packages, handoffs = _read_records(catalog)
    # A worker may publish between the catalog read and the history read. Keep the
    # selected script/spec, candidate list and preview on the same production revision.
    selected = catalog["production_ref"]
    if selected:
        index = next(i for i, p in enumerate(packages) if p["revision_id"] == selected["revision_id"]
                     and p["sha256"] == selected["sha256"])
        packages = packages[index:]
    else:
        packages = []
    script = _script_view(service, catalog, scripts, folder)
    productions = [_delivery(service, p, folder, handoffs,pid) for p in packages]
    from pianchang.studio_recovery import read_incomplete_production, incomplete_review_view, delivery_review_view
    incomplete = read_incomplete_production({**catalog, "project": project})
    partial_review = incomplete_review_view(service, {**catalog, "project": project}, incomplete, jobs=jobs)
    delivery_review = delivery_review_view(service, {**catalog, "project": project}, packages[0] if packages else None)
    video_plan, plan_error = None, None
    try:
        if catalog.get("pending_script_ref"):
            raise ValueError("新版剧本已发布，请先更新制作规格。")
        video_plan = studio_video_plan({**catalog, "project": project}, packages[0] if packages else None,incomplete_snapshot=incomplete)
    except (ValueError, KeyError) as exc:
        # A legacy or unfinished script must still be accessible for editing and repair.
        plan_error = str(exc)
        event("studio.video_plan.unavailable", {"path": "video_plan", "expected": "可计算拍摄计划的制作规格",
              "actual": plan_error, "status": "FAIL"}, kind="validation")
    title = script["title"] or service._project(pid)[1]["title"]
    event("studio.production.flow.read", {"project_id": pid, "story_id": catalog["story_id"], "script": script["script_ref"],
          "spec": catalog["spec"], "productions": [p["revision_id"] for p in productions], "messages": len(handoffs)})
    reviewed = partial_review or delivery_review
    current = productions[0] if productions else None
    from pianchang.preview_access import preview_status
    same_denial=(denied_gate and denied_gate.get("ready") is False and
        all(denied_gate.get(key)==catalog.get(key) for key in ("script_ref","production_ref")) and denied_gate.get("spec_ref")==catalog.get("spec"))
    access=denied_gate if same_denial else preview_status(service,pid)
    from pianchang.video_execution import current_commission
    execution=current_commission(service,pid,view=catalog)
    from pi_bridge.input_tools import reference_inputs_view
    try:execution["reference_inputs"]=reference_inputs_view(service,{**catalog,"project_id":pid},execution)
    except (KeyError,ValueError,OSError) as exc:
        execution["reference_inputs"]=[];execution["reference_input_error"]=str(exc)
    preview_ready=access["ready"]
    preview_gate = {**{k:v for k,v in access.items() if k not in {"allowed_media","preview"}},
                    "reviewed_count": (reviewed or {}).get("reviewed_count", 0),
                    "passed_count": sum(s.get("status") == "PASS" or s.get("human_approved") is True
                                        for s in (reviewed or {}).get("shots", [])),
                    "total": len(project["shots"])}
    for production in productions:
        if not preview_ready or production["revision_id"]!=catalog.get("production_ref",{}).get("revision_id"):
            production["preview_url"]=None
            for shot in production["shots"]:shot["url"]=None
    if not preview_ready:
        for review in (partial_review,delivery_review):
            for shot in (review or {}).get("shots",[]):
                if not (review is partial_review and (shot.get("can_approve") or shot.get("human_approved"))
                        and (shot.get("preview_url") or "").startswith(f"/api/projects/{pid}/studio/progress/media?")):
                    shot["preview_url"] = None
    return {"project_id": pid, "title": title, "story_id": catalog["story_id"], "run_id": catalog["run_id"],
            "project_path": catalog["project_path"], "script": script, "assets": catalog, "productions": productions,
            "asset_work": work, "video_plan": video_plan, "video_plan_error": plan_error, "video_execution":execution,"incomplete_production": partial_review,
            "production_review": delivery_review, "preview_gate": preview_gate,
            "stages": _stages(catalog, script, productions, work), "handoff": {"events": handoffs,
            "run_path": str(folder) if (folder / "overview.json").is_file() else None, "store_path": catalog["store_path"]}}
