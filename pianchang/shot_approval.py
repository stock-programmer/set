"""Record the operator's decision about one selected, reviewed commission candidate."""
import json
import os
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

from pianchang.story_spec import digest_json
from pianchang.studio_recovery import _read, _work
from trace_system import TraceError, current_context, event, traced


def approval_matches(decision, request_id, project, sid, clip, summary, *, project_id=None):
    if not decision or not clip or not summary:
        return False
    expected = {"decision": "approved", "request_id": request_id, "project_sha256": digest_json(project),
                "shot_id": sid, "media_sha256": clip["media_sha256"], "report_sha256": summary["report_sha256"]}
    if project_id is not None:
        expected["project_id"] = project_id
    return (all(decision.get(key) == value for key, value in expected.items())
            and bool(decision.get("project_id") and decision.get("trace_id") and decision.get("approved_at")))


def _decision_path(directory, request_id, sid, clip, summary):
    key = digest_json([request_id, sid, clip["media_sha256"], summary["report_sha256"]])
    return Path(directory) / "manual-approvals" / (key + ".json")


def read_approvals(directory, project, shots, reports, *, project_id=None):
    directory = Path(directory)
    request_id = directory.name.removeprefix("video-spec-")
    if not (directory / "manual-approvals").is_dir():
        return {}
    result = {}
    for sid in shots.keys() & reports.keys():
        clip, summary = shots[sid], reports[sid]
        receipt = _read(_decision_path(directory, request_id, sid, clip, summary))
        if receipt is None:
            continue
        if not approval_matches(receipt, request_id, project, sid, clip, summary, project_id=project_id):
            raise ValueError("人工验收记录与本次委托或素材版本不符：" + sid)
        result[sid] = receipt
    return result


def _record_once(path, decision):
    # Publish a complete, immutable receipt. Concurrent replays read back the first decision.
    previous = _read(path)
    if previous is not None:
        return previous
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".decision-", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(decision, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        event("studio.shot_approval.write_requested", {"path": str(path), "decision": decision}, kind="artifact")
        try:
            if os.name == "nt":
                os.rename(temporary, path)  # Windows rename refuses to replace an existing receipt.
            else:
                os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        temporary.unlink(missing_ok=True)
    return _read(path)


def report_can_approve(report, contract):
    from pianchang.manual_delivery import human_reviewable_criteria
    if (not contract or report.get("status") not in {"FAIL", "UNKNOWN"}
            or report.get("contract_sha256") != digest_json(contract)):
        return False
    required = [r for r in report["results"] if r.get("required")]
    rows = {r["criterion_id"]: r["status"] for r in required}
    declared = {r["criterion_id"] for r in contract["requirements"] if r["required"]}
    if (len(rows) != len(required) or not set(rows.values()) <= {"PASS", "FAIL", "UNKNOWN"}
            or not declared <= rows.keys() or any(rows.get(key) != "PASS" for key in ("evidence_integrity", "candidate_binding"))):
        return False
    human = human_reviewable_criteria(contract)
    edges = {r["criterion_id"] for r in contract["requirements"] if r["kind"] == "sequence_review"}
    unresolved = {cid for cid, status in rows.items() if status != "PASS"}
    return bool(unresolved & human) and unresolved <= human | edges


def selected_reviews(view, snapshot):
    work = _work(view, snapshot["request_id"])
    records = _read(work / "reports.json", {})
    source = snapshot.get("prior_continuation")
    if source:
        previous = _read(_work(view, source["request_id"]) / "reports.json", {})
        for sid in (snapshot["available"].keys() & previous.keys()) - records.keys():
            summary = previous[sid]
            path = Path(summary["report_path"]).resolve()
            if not path.is_relative_to(work.parent.resolve()):
                raise ValueError("历史验收报告不属于当前故事。")
            report = _read(path)
            if digest_json(report) != summary["report_sha256"]:
                raise ValueError("历史验收报告哈希不符。")
            item = snapshot["available"][sid]
            clip = item.get("clip") or item["observation"]
            if report["media_sha256"] == clip["media_sha256"] and report["project_sha256"] == digest_json(view["project"]):
                records[sid] = summary
    return {sid: records[sid] for sid in snapshot["available"] if sid in records}


def active_production(jobs, pid, request_id):
    from pianchang.asset_dispatch import ASSET_ACTIONS
    return any(j.get("project_id") == pid and j.get("status") in {"queued", "running"}
               and (j.get("kind") in ASSET_ACTIONS | {"studio_video", "studio_review"}
                    or request_id in (j.get("_pi_continue_commission"), j.get("commission_id"), j.get("message_id")))
               for j in jobs)


def shot_preview_url(pid, job_id, sid, media_sha256):
    query = urlencode({"job_id": job_id, "shot_id": sid, "sha256": media_sha256})
    return f"/api/projects/{pid}/studio/progress/media?{query}"


def _with_versions(result, progress):
    rows = {row["shot_id"]: row for row in result["shots"]}
    for item in progress["shots"]:
        sid, versions = item["shot_id"], item.get("versions",[])
        if not versions:
            continue
        row = rows.get(sid)
        if row is None:
            row = {"shot_id":sid, "job_id":result["job_id"], "request_id":result["request_id"],
                   "can_approve":False, "human_approved":False}
            result["shots"].append(row)
            current = next((v for v in versions if v["selected"]),{})
            row.update({k:current[k] for k in ("media_sha256","preview_url","report","report_sha256") if k in current})
            if row.get("report"):
                row.update(status=row["report"]["status"],review_status="completed")
        for version in versions:
            if version["selected"] and version["media_sha256"] == row.get("media_sha256") and row.get("report_sha256"):
                version.update(report_sha256=row["report_sha256"], report=row["report"])
        row["versions"] = versions
    result["version_count"] = sum(len(r.get("versions",[])) for r in result["shots"])
    return result


@traced("studio.shot_approval.panel", kind="validation", capture_inputs=False, capture_output=False)
def shot_review_panel(service, pid, request_id):
    """Read the current commission's indexes; playing media and approving it verify actual bytes."""
    from pianchang.studio_assets import studio_project_for_video
    from pianchang.studio_live_progress import _snapshot, local_failure_before_generation
    from pianchang.studio_recovery import _continuation_checkpoint, _root
    from pianchang.studio_retakes import _latest_request

    if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
        raise ValueError("请指定本次制作委托。")
    view = studio_project_for_video(service, pid, include_delivery=True)
    project = view["project"]
    result = {"project_id": pid, "request_id": request_id, "job_id": None,
              "available_count": 0, "version_count":0, "total": len(project["shots"]), "shots": [],
              "delivered": False, "all_shots_accepted": False}
    with service.lock:
        jobs = service.state["jobs"]
        source = next((j for j in reversed(jobs) if j.get("kind") == "studio_video" and j.get("project_id") == pid
                       and j.get("run_id") == view["run_id"] and j.get("message_id") == request_id), None)
        busy = active_production(jobs, pid, request_id)
    if not source or view.get("pending_script_ref"):
        result["blocked_reason"] = "当前剧本或制作委托已变更，请查看最新制作计划。"
        return result
    result["job_id"] = source["id"]
    request, outcome = _latest_request(view)
    package = view.get("published_delivery")
    def read_versions():
        return _snapshot(_root(view), pid, source, getattr(getattr(service,"video_config",None),"output_dir",None),
                         lambda sid,sha:shot_preview_url(pid,source["id"],sid,sha),include_reviews=False,version_paths={})

    if ((not request or request["id"] == request_id) and package and package.get("request_id") == request_id
            and package.get("script_ref") == view["script_ref"] and package.get("spec_ref") == view["spec"]):
        from pianchang.preview_access import preview_status
        gate = preview_status(service, pid)
        if gate["ready"] and gate.get("production_ref") == view["production_ref"]:
            result.update(delivered=True, all_shots_accepted=True, available_count=result["total"])
        else:
            result["blocked_reason"] = "当前交付版本正在核对预览门禁。"
        return _with_versions(result,read_versions()[0])
    if not request or request["id"] != request_id or outcome and (outcome.get("production") or outcome.get("partial")):
        result["blocked_reason"] = "这份委托已更新或已有制作结果，请核对当前拍摄计划和回执。"
        if request and request["id"] == request_id and outcome and outcome.get("partial"):
            progress,_ = read_versions()
            result["available_count"] = progress["completed"]
            return _with_versions(result,progress)
        return result
    work = _work(view, request_id)
    if _read(work / "project.json") != project:
        result["blocked_reason"] = "工作目录的制作规格与当前委托不一致，等待重新核对。"
        return result
    plan = request["body"].get("shooting_plan") or {}
    clips = _read(work / "clips.json", {})
    snapshot = {"request_id": request_id, "available": {}}
    if plan.get("continuation"):
        snapshot["prior_continuation"] = plan["continuation"]
        checkpoint = _continuation_checkpoint(_root(view), view["run_id"], plan["continuation"], request["body"])
        clips = {**checkpoint["shots"], **clips}
    reports = _read(work / "reports.json", {})
    progress, files = read_versions()
    progress_files = {r["shot_id"]:r.get("media_sha256") for r in progress["shots"] if r["shot_id"] in files}
    unindexed = reports.keys() - clips.keys()
    if unindexed:
        # Older interrupted downloads can have a review before adoption into clips.json.
        for row in progress["shots"]:
            sid = row["shot_id"]
            if sid in unindexed and sid in files and not row.get("repair_pending"):
                clips[sid] = {"media_path": str(files[sid]), "media_sha256": row["media_sha256"]}
    snapshot["available"] = {sid: {"clip": clip} for sid, clip in clips.items()}
    reports = selected_reviews(view, snapshot)
    approvals = read_approvals(work, project, clips, reports, project_id=pid)
    project_hash, accepted = digest_json(project), set()
    history = _read(work / "repair-history.json", {}).get("history", [])
    pending = {h["shot_id"] for h in history if h.get("status") == "started_or_unknown"
               and not local_failure_before_generation(h, work)["not_dispatched"]}
    for shot in project["shots"]:
        sid = shot["id"]
        clip, summary = clips.get(sid), reports.get(sid)
        row = {"shot_id":sid,"job_id":source["id"],"request_id":request_id,
               "can_approve":False,"human_approved":False,"review_status":"pending"}
        if not clip or not re.fullmatch("[a-f0-9]{64}",str(clip.get("media_sha256"))):
            result["shots"].append({**row,"blocked_reason":"此镜头尚无可核对的本地视频。"})
            continue
        has_media = bool(clip.get("media_path")) and Path(clip["media_path"]).is_file()
        row["media_sha256"] = clip["media_sha256"]
        if has_media:
            result["available_count"] += 1
            row["preview_url"] = shot_preview_url(pid, source["id"], sid, clip["media_sha256"])
        if not summary:
            result["shots"].append({**row,"blocked_reason":"当前视频的验收报告尚未就绪。"})
            continue
        path = Path(summary["report_path"]).resolve()
        if not path.is_relative_to(work.parent.resolve()):
            result["shots"].append({**row,"blocked_reason":"验收报告不属于当前故事的证据目录，等待核对。"})
            continue
        report = _read(path)
        if (not report or digest_json(report) != summary["report_sha256"] or report["shot_id"] != sid
                or report["project_sha256"] != project_hash or report["media_sha256"] != clip["media_sha256"]):
            result["shots"].append({**row,"blocked_reason":"现有报告与当前镜头版本不匹配，等待重新核对。"})
            continue
        if has_media and report["status"] == "PASS" and report["task_complete"]:
            accepted.add(sid)
            continue
        eligible = has_media and report_can_approve(report, _read(path.parent / "contract.json"))
        human = eligible and sid in approvals
        if human:
            accepted.add(sid)
        issues = [{key: row.get(key) for key in ("criterion_id", "status", "reason", "required")}
                  for row in report["results"] if row["required"] and row["status"] != "PASS"]
        row.update(report_sha256=summary["report_sha256"],status=report["status"],review_status="completed",
                   can_approve=eligible and not human and not busy and sid not in pending,human_approved=human,
                   report={"status":report["status"],"results":issues})
        if not has_media:
            row["blocked_reason"] = "当前候选的视频文件缺失，已保留对应的验收报告。"
        elif not eligible:
            row["blocked_reason"] = "以下技术、证据或相邻衔接项仍需制作流程复验。"
        elif not human and (busy or sid in pending):
            row["blocked_reason"] = "当前制作或重拍回执仍在处理中，等待选用版本确定。"
        result["shots"].append(row)
    readonly = [r for r in result["shots"] if r.get("preview_url") and not r["can_approve"] and not r["human_approved"]]
    for row in readonly:
        if progress_files.get(row["shot_id"]) != row["media_sha256"]:
            row.pop("preview_url",None)
            row["blocked_reason"] += " 单段播放回执尚未与这个版本匹配。"
    result["all_shots_accepted"] = bool(accepted) and not pending and len(accepted) == result["total"]
    return _with_versions(result,progress)


@traced("studio.shot_approval.record", kind="tool", capture_inputs=False)
def approve_shot(service, pid, body):
    from pianchang.app_service import now
    from pianchang.manual_delivery import finish_if_approved
    from pianchang.studio_assets import studio_project_for_video
    from pianchang.studio_recovery import prepare_continuation, read_incomplete_production
    from pianchang.studio_retakes import _latest_request
    from pianchang.video_collab_agent import VideoWorkspace

    if (not isinstance(body, dict) or set(body) != {"request_id", "shot_id", "media_sha256", "report_sha256"}
            or not all(isinstance(v, str) for v in body.values())
            or not re.fullmatch(r"[a-f0-9]{32}", body["request_id"])
            or any(not re.fullmatch(r"[a-f0-9]{64}", body[key]) for key in ("media_sha256", "report_sha256"))):
        raise ValueError("人工通过需要本次委托、镜头、素材和验收报告的精确版本。")
    sid, request_id = body["shot_id"], body["request_id"]
    event("studio.shot_approval.request", {"project_id": pid, **body}, kind="state")
    with service.lock:
        view = studio_project_for_video(service, pid, include_delivery=True)
        package = view.pop("published_delivery")
        if view.get("pending_script_ref") or active_production(service.state["jobs"], pid, request_id):
            raise ValueError("故事版本或制作进度正在更新，请刷新后审看当前候选。")
        project = view["project"]
        work = _work(view, request_id)
        request, result = _latest_request(view)
        # An interrupted HTTP response after publication can be replayed safely.
        if ((not request or (request["id"] == request_id and (result or {}).get("production") == view.get("production_ref")))
                and package and view.get("production_ref") and package.get("request_id") == request_id
                and package["script_ref"] == view["script_ref"] and package["spec_ref"] == view["spec"]):
            receipt = read_approvals(work, project, package["shots"], package["reports"], project_id=pid).get(sid)
            if receipt and receipt == (package.get("manual_approvals") or {}).get(sid) and all(receipt[k] == v for k, v in body.items()):
                return {**receipt, "delivery": {"delivered": True, "production_ref": view["production_ref"], "issues": []}}
        if not request:
            raise ValueError("已无匹配的待验收制作委托，请刷新。")
        if request["id"] != request_id:
            raise ValueError("委托已改变，请审看当前委托。")
        snapshot = read_incomplete_production(view)
        if not snapshot or snapshot["request_id"] != request_id or snapshot.get("active"):
            raise ValueError("没有可由人工验收的已停止委托。")
        clips = {key: item["clip"] for key, item in snapshot["available"].items() if "clip" in item}
        if len(clips) != len(snapshot["available"]):
            # Independently observed downloads become normal, manifest-bound clips before approval.
            clips = prepare_continuation(view, {"request_id": request_id, "sha256": snapshot["sha256"]})["shots"]
        if snapshot.get("prior_continuation"):
            indexed = _read(work / "clips.json", {})
            for key in clips.keys() - indexed.keys():
                if "clip" in snapshot["available"][key]:
                    clips[key] = {**clips[key], "continuation_source": snapshot["prior_continuation"]}
                    clips[key].pop("partial_source", None)
        reports = selected_reviews(view, snapshot)
        clip, summary = clips.get(sid), reports.get(sid)
        if (not clip or not summary or clip["media_sha256"] != body["media_sha256"]
                or summary["report_sha256"] != body["report_sha256"]):
            raise ValueError("选用的素材或报告已改变，请重新审看。")
        folder = Path(summary["report_path"]).resolve().parent
        if not folder.is_relative_to(work.parent.resolve()):
            raise ValueError("报告不属于当前故事。")
        report, contract = _read(summary["report_path"]), _read(folder / "contract.json")
        inspection = _read(summary["inspection_path"])
        if (not report_can_approve(report, contract) or digest_json(report) != body["report_sha256"]
                or report["shot_id"] != sid or report["project_sha256"] != digest_json(project)
                or report["media_sha256"] != clip["media_sha256"] or inspection["media_sha256"] != clip["media_sha256"]):
            raise ValueError("此候选的媒体、报告或验收依据不支持人工通过。")
        evidence = SimpleNamespace(project=project, shots=clips, reports={sid: summary})
        gate = VideoWorkspace.acceptance_gate(evidence)
        if any(i["shot_id"] == sid and i["criterion_id"] in {"acceptance_binding", "required_coverage", "media_or_report"}
               for i in gate["issues"]):
            raise ValueError("候选绑定或验收证据不完整。")
        # Recheck selection after the potentially long media/evidence verification.
        current = studio_project_for_video(service, pid)
        latest, _ = _latest_request(current)
        selected = _read(work / "clips.json", {}).get(sid)
        current_summary = selected_reviews(current, snapshot).get(sid)
        if (not latest or latest["id"] != request_id or current.get("pending_script_ref")
                or any(current.get(k) != view.get(k) for k in ("script_ref", "spec", "project", "production_ref"))
                or (selected and selected["media_sha256"] != body["media_sha256"])
                or not current_summary or current_summary["report_sha256"] != body["report_sha256"]):
            raise ValueError("审看期间选用的镜头、报告或委托发生变更，请刷新。")
        decision = {**body, "decision": "approved", "project_id": pid, "project_sha256": digest_json(project),
                    "approved_at": now(), "trace_id": current_context()["trace_id"]}
        receipt = _record_once(_decision_path(work, request_id, sid, clip, summary), decision)
        if not approval_matches(receipt, request_id, project, sid, clip, summary, project_id=pid):
            raise ValueError("已存的人工验收决定与请求版本不符。")
        event("studio.shot_approval.recorded", receipt, kind="state")
        return {**receipt, "delivery": finish_if_approved(service, view, request_id, clips, reports)}
