"""Recover pinned local clips and review them without replaying video generation."""
from __future__ import annotations

import copy
import json
import re
import shutil
from pathlib import Path

from pianchang.story_spec import digest_json
from trace_system import TraceError, current_context, event, traced


def _read(path, default=None):
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else default


def _check(path, expected, actual):
    valid = expected == actual
    event("studio.continuation.check", {"path": path, "expected": expected, "actual": actual,
          "status": "PASS" if valid else "FAIL"}, kind="validation")
    if not valid:
        raise ValueError("恢复素材或验收记录不符：" + path)


def _root(view):
    return Path(view["store_path"]).resolve().parents[2]


def _work(view, request_id):
    if not re.fullmatch(r"[a-f0-9]{32}", str(request_id)):
        raise ValueError("恢复委托标识无效。")
    return Path(view["store_path"]).parent / "runs" / view["run_id"] / ("video-spec-" + request_id)


@traced("studio.continuation.read", kind="validation", capture_inputs=False, capture_output=False)
def read_incomplete_production(view):
    from pianchang.studio_retakes import _latest_request
    from pianchang.studio_live_progress import _snapshot, _open_candidates

    request, result = _latest_request(view)
    if not request or (result or {}).get("production") or (result or {}).get("partial"):
        return None
    plan = request["body"].get("shooting_plan")
    if not plan:
        return None
    root = _root(view)
    state = _read(root / "outputs/web/state.json", {})
    job = next((j for j in reversed(state.get("jobs", [])) if j.get("message_id") == request["id"]
                and j.get("project_id") == view["project_id"]), None)
    if not job:
        return None
    work = _work(view, request["id"])
    project = _read(work / "project.json")
    if project is None:
        return None
    project_sha256 = digest_json(project)
    _check("source.project", digest_json(view["project"]), project_sha256)
    progress, files = _snapshot(root, view["project_id"], job, root / "outputs/videos", None)
    clips = _read(work / "clips.json", {})
    if plan.get("continuation"):
        source = _continuation_checkpoint(root, view["run_id"], plan["continuation"], request["body"])
        for sid, clip in source["shots"].items():
            clips.setdefault(sid, {**clip, "operation": "reused", "script_ref": view["script_ref"]})
    impact = _read(work / "impact.json")
    if not impact:
        raise ValueError("原委托缺少镜头依赖分析，不能推断恢复范围。")
    available, unknown, failed, missing = {}, [], [], []
    rows = {r["shot_id"]: r for r in progress["shots"]}
    for shot in project["shots"]:
        sid = shot["id"]
        row = rows.get(sid, {})
        if row.get("repair_pending"):
            unknown.append(sid)
            continue
        if sid in files:
            clip = clips.get(sid)
            if clip:
                manifest = _read(clip["manifest_path"])
                if clip.get("operation") == "generated":
                    _check(sid + ".project", project_sha256, manifest["metadata"].get("project_sha256"))
                _check(sid + ".dependency", impact["shots"][sid]["target"]["sha256"], clip["dependency_sha256"])
                _check(sid + ".references", shot.get("required_refs", []), clip.get("required_refs", []))
                available[sid] = {"clip": clip}
            else:
                candidates = _open_candidates(root / "outputs/videos", {sid}, plan, request["body"]["script"], job.get("created_at"))
                _check(sid + ".single_task", 1, len(candidates.get(sid, [])))
                source_job = candidates[sid][0]
                receipt_path = root / "outputs/web/progress-observations" / job["id"] / sid / row["task_id"] / "receipt.json"
                receipt = _read(receipt_path)
                _check(sid + ".observation", True, bool(receipt and row.get("independently_observed")))
                manifest = _read(source_job["file"])
                _check(sid + ".project", project_sha256, manifest["metadata"].get("project_sha256"))
                _check(sid + ".dependency", impact["shots"][sid]["target"]["sha256"], manifest["metadata"]["dependency_sha256"])
                _check(sid + ".references", shot.get("required_refs", []), manifest["metadata"].get("required_refs", []))
                available[sid] = {"observation": {"manifest_path": str(source_job["file"]), "manifest_sha256": source_job["hash"],
                    "receipt_path": str(receipt_path), "receipt_sha256": digest_json(receipt),
                    "media_path": str(files[sid]), "media_sha256": row["media_sha256"]}}
        elif row.get("status") == "failed":
            failed.append(sid)
        elif row.get("status") in {"unknown", "generating"}:
            unknown.append(sid)
        else:
            missing.append(sid)
    source = {"request_id": request["id"], "job_id": job["id"], "project_id": view["project_id"],
              "script_ref": view["script_ref"], "spec_ref": view["spec"], "project_sha256": project_sha256,
              "available": available, "missing_shots": missing, "unknown_shots": unknown, "failed_shots": failed}
    if plan.get("continuation"):
        source["prior_continuation"] = plan["continuation"]
    return {**source, "sha256": digest_json(source), "active": job["status"] in {"queued", "running"}}


@traced("studio.continuation.prepare", kind="tool", capture_inputs=False, capture_output=False)
def prepare_continuation(view, pointer):
    from pianchang.acceptance import file_sha256
    from pianchang.video_collab_agent import _validated_media, _write, analyze_impact

    snapshot = read_incomplete_production(view)
    _check("continuation.source", pointer, {"request_id": snapshot["request_id"], "sha256": snapshot["sha256"]} if snapshot else None)
    impact = analyze_impact(view["project"], None)
    root, shots = _root(view), {}
    for sid, entry in snapshot["available"].items():
        if "clip" in entry:
            clip = copy.deepcopy(entry["clip"])
        else:
            observed = entry["observation"]
            original, receipt = _read(observed["manifest_path"]), _read(observed["receipt_path"])
            _check(sid + ".original_manifest", observed["manifest_sha256"], digest_json(original))
            _check(sid + ".observation_receipt", observed["receipt_sha256"], digest_json(receipt))
            _check(sid + ".observed_media", observed["media_sha256"], file_sha256(observed["media_path"]))
            directory = root / "outputs/videos" / digest_json(observed)[:24]
            manifest_path, media_path = directory / "job.json", directory / "video.mp4"
            job = {**original, "status": "SUCCEEDED", "provider_response": receipt["provider_response"],
                   "local_video_path": str(media_path), "download_sha256": observed["media_sha256"],
                   "updated_at": receipt["observed_at"], "recovery_source": observed}
            if manifest_path.is_file():
                _check(sid + ".recovered_manifest", digest_json(job), digest_json(_read(manifest_path)))
                _check(sid + ".recovered_media", observed["media_sha256"], file_sha256(media_path))
            else:
                event("studio.continuation.adopt_observed_media", {"shot_id": sid, "source": observed,
                      "manifest_path": str(manifest_path), "media_path": str(media_path)}, kind="artifact")
                directory.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(observed["media_path"], media_path)
                _check(sid + ".copied_media", observed["media_sha256"], file_sha256(media_path))
                _write(manifest_path, job)
            clip = {"shot_id": sid, "operation": "generated", "origin_script_ref": snapshot["script_ref"],
                    "script_ref": snapshot["script_ref"], "task_id": job["task_id"], "manifest_path": str(manifest_path),
                    "generation_job_sha256": digest_json(job), "media_path": str(media_path), "media_sha256": job["download_sha256"],
                    "dependency_sha256": job["metadata"]["dependency_sha256"]}
            if job["metadata"].get("required_refs"):
                clip.update(required_refs=job["metadata"]["required_refs"], reference_receipt=job["reference_receipt"],
                            submitted_refs=job["reference_receipt"]["submitted_refs"])
        _validated_media(clip, require_origin=True)
        _check(sid + ".validated_dependency", impact["shots"][sid]["target"]["sha256"], clip["dependency_sha256"])
        shots[sid] = clip
    source = {k: v for k, v in snapshot.items() if k not in {"sha256", "active"}}
    checkpoint = {"source": source, "project": view["project"], "script_ref": view["script_ref"],
                  "shots": shots, "missing_shots": snapshot["missing_shots"], "failed_shots": {}}
    path = _work(view, pointer["request_id"]) / ("resume-" + pointer["sha256"][:16] + ".json")
    if path.is_file():
        _check("continuation.checkpoint", digest_json(checkpoint), digest_json(_read(path)))
    else:
        _write(path, checkpoint)
        event("studio.continuation.checkpoint", {"path": str(path), "sha256": digest_json(checkpoint),
              "reused_shots": list(shots), "missing_shots": checkpoint["missing_shots"]}, kind="artifact")
    return checkpoint


@traced("studio.continuation.load", kind="validation", capture_inputs=False, capture_output=False)
def load_continuation(root, run_id, pointer, order):
    from pianchang.video_collab_agent import _validated_media
    checkpoint = _continuation_checkpoint(root, run_id, pointer, order)
    for clip in checkpoint["shots"].values():
        _validated_media(clip, require_origin=True)
    return checkpoint


def _continuation_checkpoint(root, run_id, pointer, order):
    if not re.fullmatch(r"[a-f0-9]{64}", str(pointer.get("sha256"))) or not re.fullmatch(r"[a-f0-9]{32}", str(pointer.get("request_id"))):
        raise ValueError("恢复记录标识无效。")
    path = Path(root) / "outputs/collaborations/runs" / run_id / ("video-spec-" + pointer["request_id"]) / ("resume-" + pointer["sha256"][:16] + ".json")
    checkpoint = _read(path)
    if not checkpoint:
        raise ValueError("恢复检查点不存在。")
    source = checkpoint["source"]
    _check("continuation.source.sha256", pointer["sha256"], digest_json(source))
    _check("continuation.script", order["script"], source["script_ref"])
    _check("continuation.spec", order["template"], source["spec_ref"])
    _check("continuation.project", source["project_sha256"], digest_json(checkpoint["project"]))
    _check("continuation.shots", list(source["available"]), list(checkpoint["shots"]))
    _check("continuation.no_unresolved_tasks", [], source["unknown_shots"] + source["failed_shots"])
    return checkpoint


@traced("studio.review.run", kind="task", capture_inputs=False, capture_output=False)
def run_review_job(service, job, body):
    from pianchang.collab_store import CollabStore
    from pianchang.studio_assets import studio_project_for_video
    from pianchang.video_collab_agent import VideoWorkspace, analyze_impact
    view = studio_project_for_video(service, job["project_id"])
    checkpoint = prepare_continuation(view, body["continuation"])
    work = _work(view, body["continuation"]["request_id"])
    workspace = VideoWorkspace(script=view["project"]["script_source"]["script"], script_ref=view["script_ref"],
        directory=work, text_config=service.text_config, video_config=service.video_config, template_project=view["project"],
        previous=None, decision_client=None, vision_config=service.vision_config, progress=None,
        reference_store=CollabStore(view["store_path"]), story_id=view["story_id"])
    workspace.project = view["project"]
    workspace.impact = analyze_impact(view["project"], None)
    workspace.shots = checkpoint["shots"]
    workspace.defer_content_review = True
    errors = {}
    for sid in workspace.shots:
        service._update(job, message="正在自动验收 " + sid + "；已生成素材会逐个保存结论")
        try:
            workspace.review_shot(sid)
        except TraceError:
            raise
        except Exception as exc:
            errors[sid] = str(exc)
            event("studio.review.failed", {"shot_id": sid, "error": str(exc)}, kind="error")
    if errors:
        raise ValueError("部分自动验收未完成：" + json.dumps(errors, ensure_ascii=False))
    if workspace.vision_config is not None:
        from pianchang.content_review import run_content_reviews
        service._update(job, message="正在对照参考审看完整片段，并检查已有相邻镜头的画面与声音衔接")
        run_content_reviews(workspace)
    return {"reports": workspace.reports, "summary": f"已检查 {len(workspace.reports)} 个已有镜头；未通过项保留在视频 Agent 的待修复清单。"}


@traced("studio.review.enqueue", kind="task", capture_inputs=False)
def enqueue_incomplete_reviews(service):
    from pianchang.studio_assets import studio_project_for_video
    seen, jobs = set(), []
    for prior in reversed(service.list_jobs()):
        pid = prior.get("project_id")
        if prior.get("kind") != "studio_video" or not pid or pid in seen:
            continue
        seen.add(pid)
        try:
            view = studio_project_for_video(service, pid)
            snapshot = read_incomplete_production(view)
            if snapshot and snapshot["available"] and not snapshot["active"]:
                from pianchang.studio_retakes import _latest_request
                request, _ = _latest_request(view)
                if ((request or {}).get("body", {}).get("shooting_plan") or {}).get("repair_existing_only"):
                    event("studio.review.repair_owned_reviews_retained", {"project_id":pid,
                          "request_id":snapshot["request_id"],"new_model_call":False,
                          "reason":"修复任务的报告和未确认调用保留，由明确的继续修复任务接管，不在启动时重放。"}, kind="cache")
                    continue
                pointer = {"request_id": snapshot["request_id"], "sha256": snapshot["sha256"]}
                from pianchang.content_review import VERSION
                key = "auto-review-" + snapshot["sha256"] + "-content2-states1-p4"
                # list_jobs hides private request keys; the durable store keeps them.
                with service.lock:
                    interrupted = [j for j in service.state["jobs"] if (j.get("_idempotency_key") or "").startswith(key)
                                   and j.get("status") == "interrupted"]
                if interrupted:
                    key += "-" + str(len(interrupted))
                jobs.append(service.submit("studio_review", pid, {"continuation": pointer}, key)["id"])
        except TraceError:
            raise
        except (ValueError, KeyError, OSError) as exc:
            event("studio.review.enqueue_unavailable", {"project_id": pid, "error": str(exc)}, kind="error")
    return {"jobs": jobs}


@traced("studio.review.read", kind="tool", capture_inputs=False, capture_output=False)
def incomplete_review_view(service, view, snapshot, *, jobs=None):
    if not snapshot:
        return None
    work = _work(view, snapshot["request_id"])
    reports = _read(work / "reports.json", {})
    prior_work = _work(view, snapshot["prior_continuation"]["request_id"]) if snapshot.get("prior_continuation") else None
    prior_reports = _read(prior_work / "reports.json", {}) if prior_work else {}
    confirmations = service.state.get("review_confirmations", {}).get(view["project_id"], {})
    shots = []
    for sid, entry in snapshot["available"].items():
        clip = entry.get("clip") or entry["observation"]
        row = {"shot_id": sid, "preview_url": service.asset(clip["media_path"], expected_sha256=clip["media_sha256"],project_id=view["project_id"]),
               "review_status": "pending", "human_confirmed": False}
        summary = reports.get(sid) or prior_reports.get(sid)
        if summary:
            path = Path(summary["report_path"]).resolve()
            _check(sid + ".report_location", True, path.is_relative_to(work.resolve())
                   or bool(prior_work and path.is_relative_to(prior_work.resolve())))
            report = _read(path)
            _check(sid + ".report_hash", summary["report_sha256"], digest_json(report))
            _check(sid + ".report_media", clip["media_sha256"], report["media_sha256"])
            row.update(review_status="completed", status=report["status"], report=_display_report(service, summary, report,view["project_id"]),
                       content_review_version=report.get("content_review_version"),
                       report_sha256=summary["report_sha256"], report_url=service.asset(path),
                       unresolved=summary["unresolved"], trace_id=summary.get("trace_id"),
                       human_confirmed=confirmations.get(sid, {}).get("report_sha256") == summary["report_sha256"])
        shots.append(row)
    repair_data = _read(work / "repair-history.json", {})
    repairs = repair_data.get("history", [])
    repair_rows = [{"shot_id": h["shot_id"], "strategy": h["strategy"], "status": h["status"],
                    "repair_id": h["repair_id"], "source_status": (h.get("source_report") or {}).get("status"),
                    "result_status": (h.get("result_report") or {}).get("status"),
                    "trace_id": (h.get("trace_context") or {}).get("trace_id"),
                    "strategy_design":repair_data.get("plans",{}).get(h["repair_id"],{}).get("strategy_design")} for h in repairs]
    if jobs is None:
        jobs = service.list_jobs(detail_project=view["project_id"])
    return {"request_id": snapshot["request_id"], "available_count": len(shots), "total": len(view["project"]["shots"]),
            "autonomy": _read(work / "autonomy.json", {}),
            "repair_history": repair_rows,
            "reviewed_count": sum(s["review_status"] == "completed" for s in shots), "shots": shots,
            "content_reviewed_count": sum(bool(s.get("content_review_version")) for s in shots),
            "missing_shots": snapshot["missing_shots"], "unknown_shots": snapshot["unknown_shots"],
            "review_jobs": [j for j in jobs if j.get("project_id") == view["project_id"] and j["kind"] == "studio_review"]}


def _display_report(service, summary, report,pid=None):
    from pianchang.acceptance import verify_inspection
    inspection = _read(summary["inspection_path"])
    _check(report["shot_id"] + ".inspection_sha256", report["inspection_sha256"], digest_json(inspection))
    verify_inspection(inspection)
    frames = [{"id": frame["id"], "timestamp_s": frame["time_s"],
               "url": service.asset(frame["path"], expected_sha256=frame["sha256"])} for frame in inspection["frames"]]
    displayed = copy.deepcopy(report)
    content_path = Path(summary["report_path"]).parent / "content.json"
    if report.get("content_evidence_sha256") and content_path.is_file():
        content = _read(content_path)
        _check(report["shot_id"] + ".content_evidence_sha256", report["content_evidence_sha256"], digest_json(content))
        state_row = next((r for r in displayed["results"] if r["criterion_id"] == "start_end_state"), None)
        if state_row and not state_row.get("state_checks"):
            for descriptor in content["receipts"]:
                receipt = _read(descriptor["path"])
                _check("display.content_receipt", descriptor["sha256"], digest_json(receipt))
                audited = next((r for r in receipt.get("rows", []) if r["criterion_id"] == "start_end_state" and r.get("state_checks")), None)
                if audited:
                    state_row["state_checks"] = audited["state_checks"]
                    state_row["supplemental_state_note"] = "本项保留此前有明确画面依据的失败结论；以下另列本次逐字段复核，未能确认的字段单独保留。"
                    break
    for row in displayed["results"]:
        for evidence in row.get("evidence", []):
            if evidence.get("path") and evidence.get("sha256"):
                evidence["url"] = service.asset(evidence["path"], expected_sha256=evidence["sha256"],project_id=pid) if Path(evidence["path"]).suffix.lower()==".mp4" else service.asset(evidence["path"], expected_sha256=evidence["sha256"])
    return {**displayed, "frames": frames}


@traced("studio.review.confirm", kind="tool", capture_inputs=False)
def confirm_review(service, pid, body):
    from pianchang.app_service import now
    from pianchang.studio_assets import studio_project_for_video
    from pianchang.preview_access import require_preview
    require_preview(service,pid)
    if set(body) != {"shot_id", "report_sha256"}:
        raise ValueError("请确认当前镜头的具体报告版本。")
    if any(j.get("project_id") == pid and j.get("kind") in {"studio_review", "studio_video"} and j["status"] in {"queued", "running"}
           for j in service.list_jobs()):
        raise ValueError("本故事的完整检查仍在进行，请等待更新后的结论再确认。")
    view = studio_project_for_video(service, pid)
    data = incomplete_review_view(service, view, read_incomplete_production(view))
    if not data and view.get("production_ref"):
        from pianchang.collab_store import CollabStore
        from pianchang.collaboration_workflow import open_ref
        data = delivery_review_view(service, view, open_ref(CollabStore(view["store_path"]), view["production_ref"]))
    row = next((r for r in (data or {}).get("shots", []) if r["shot_id"] == body["shot_id"]), None)
    _check("confirmation.report_sha256", (row or {}).get("report_sha256"), body["report_sha256"])
    if not row or row["review_status"] != "completed":
        raise ValueError("自动验收尚未完成。")
    if data["available_count"] != data["total"] or any(s.get("status") != "PASS" for s in data["shots"]):
        raise ValueError("视频 Agent 的必过验收尚未全部通过，当前仍属于自动修复阶段，不能进入人工预览确认。")
    if data.get("request_id") or not view.get("production_ref"):
        raise ValueError("视频 Agent 尚未完成已验收版本的交付，请等待人工预览解锁。")
    decision = {"report_sha256": row["report_sha256"], "status": row["status"], "confirmed_at": now(),
                "trace_id": current_context()["trace_id"], "decision": "acknowledged"}
    event("studio.review.human_acknowledged", {"project_id": pid, "shot_id": body["shot_id"], **decision}, kind="state")
    with service.lock:
        service.state.setdefault("review_confirmations", {}).setdefault(pid, {})[body["shot_id"]] = decision
        service._save()
    return decision


@traced("studio.review.delivery_read", kind="tool", capture_inputs=False, capture_output=False)
def delivery_review_view(service, view, package):
    if not package or package["content"]["project"] != view["project"]:
        return None
    data = package["content"]
    confirmations = service.state.get("review_confirmations", {}).get(view["project_id"], {})
    shots = []
    folder = (Path(view["store_path"]).parent / "runs" / view["run_id"]).resolve()
    for sid, clip in data["shots"].items():
        summary = data.get("reports", {}).get(sid)
        if not summary or not summary.get("report_path") or not summary.get("report_sha256"):
            continue
        path = Path(summary["report_path"]).resolve()
        _check(sid + ".delivery_report_location", True, path.is_relative_to(folder))
        report = _read(path)
        _check(sid + ".delivery_report_hash", summary["report_sha256"], digest_json(report))
        _check(sid + ".delivery_report_media", clip["media_sha256"], report["media_sha256"])
        shots.append({"shot_id": sid, "preview_url": service.asset(clip["media_path"], expected_sha256=clip["media_sha256"],project_id=view["project_id"]),
            "review_status": "completed", "status": report["status"], "report": _display_report(service, summary, report,view["project_id"]),
            "report_sha256": summary["report_sha256"], "report_url": service.asset(path), "trace_id": summary.get("trace_id") or package["trace_id"],
            "human_confirmed": confirmations.get(sid, {}).get("report_sha256") == summary["report_sha256"]})
    return {"available_count": len(shots), "total": len(view["project"]["shots"]), "reviewed_count": len(shots),
            "shots": shots, "missing_shots": [], "unknown_shots": [], "review_jobs": []}
