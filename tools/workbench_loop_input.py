"""Versioned questions and durable replies for the local workbench watcher."""
import re
from pathlib import Path

from pianchang.app_service import read_json, write_json
from pianchang.story_spec import digest_json
from trace_system import current_context, event, traced


def _draft_id(job):
    if job.get("kind") == "studio_script":
        return job["id"]
    if job.get("kind") == "pi_script":
        return job.get("story_job_id")
    return None


def human_request(job, kind, basis, title, message, *, fields=(), submit_label="提交并继续"):
    project_id = job.get("project_id") or None
    scope, draft = project_id, {}
    if not project_id:
        story_id = _draft_id(job)
        if kind != "script_creative_input" or not re.fullmatch("[a-f0-9]{32}", str(story_id)):
            raise ValueError("找不到这份委托所属的项目或剧本草稿。")
        draft = {"story_job_id": story_id}
        scope = draft
    # Existing project questions keep their identity across a watcher upgrade.
    identity = digest_json([scope, job["id"], job.get("trace_id"), kind, basis, fields])
    return {"id": identity, "project_id": project_id, "job_id": job["id"], "kind": kind, **draft,
            "title": title, "message": message, "fields": list(fields), "submit_label": submit_label}


def _answers(request, answers):
    fields = {f["name"]: f for f in request["fields"]}
    if not fields or not isinstance(answers, dict) or set(answers) - fields.keys():
        raise ValueError("答复与当前问题不匹配。")
    result = {}
    for name, field in fields.items():
        value = answers.get(name)
        if value is None or value is False or value == "":
            if field.get("required", True):
                raise ValueError("请填写" + field["label"] + "。")
            continue
        if field["type"] == "confirm":
            valid = value is True
        elif field["type"] == "select":
            valid = value in [option["value"] for option in field["options"]]
        elif name == "endpoint_id" and isinstance(value, str):
            value = value.strip()
            valid = re.fullmatch(r"ep-[A-Za-z0-9][A-Za-z0-9-]{1,123}", value)
        elif field["type"] == "text" and isinstance(value, str):
            value = value.strip()
            valid = 0 < len(value) <= min(field.get("max_length", 1000), 4000)
        else:
            valid = False
        if not valid:
            raise ValueError(field["label"] + "的取值无效。")
        result[name] = value
    if not result:
        raise ValueError("请提供可用接入点，或确认已完成页面上的操作。")
    return result


def _matches_request(request, jid, rid, project_id, story_id):
    if (request.get("id") != rid or request.get("job_id") != jid
            or request.get("project_id") != project_id):
        return False
    if story_id is None:
        return True
    if request.get("kind") != "script_creative_input":
        return False
    owners = [request[name] for name in ("story_job_id", "story_id") if name in request]
    return bool(owners) and all(owner == story_id for owner in owners)


@traced("workbench.feedback.answer", kind="tool", capture_inputs=False, capture_output=False)
def answer_request(root, project_id, body, *, story_id=None):
    from tools.workbench_loop_watch import _folder, _jobs, _lock, _signature, watcher_paused
    if not isinstance(body, dict) or set(body) != {"job_id", "request_id", "answers"}:
        raise ValueError("答复缺少请求标识或包含未知字段。")
    jid, rid = body["job_id"], body["request_id"]
    if not re.fullmatch("[a-f0-9]{32}", str(jid)) or not re.fullmatch("[a-f0-9]{64}", str(rid)):
        raise ValueError("答复标识无效。")
    root = Path(root)
    job = _jobs(root).get(jid)
    if project_id is not None:
        if (story_id is not None or not re.fullmatch("[a-f0-9]{16}", str(project_id))
                or not job or job.get("project_id") != project_id):
            raise KeyError("project")
    elif (not re.fullmatch("[a-f0-9]{32}", str(story_id)) or not job
          or job.get("project_id") or _draft_id(job) != story_id):
        raise KeyError("story")
    if watcher_paused(root):
        raise ValueError("旁路记录中断，请先恢复 Trace。")
    folder = _folder(root)
    target = folder / "answers" / (rid + ".json")
    with _lock(target.with_suffix(".lock")) as acquired:
        if not acquired:
            raise ValueError("正在保存同一答复，请稍后重试。")
        if target.is_file():
            saved = read_json(target)
            if (saved.get("request_id") != rid or saved.get("job_id") != jid
                    or saved.get("project_id") != project_id or saved.get("story_job_id") != story_id
                    or not _matches_request(saved.get("request") or {}, jid, rid, project_id, story_id)):
                raise ValueError("答复不属于当前问题。")
            cleaned = _answers(saved["request"], body["answers"])
            if saved["answers"] != cleaned:
                raise ValueError("此问题已经收到另一份答复。")
        else:
            state = read_json(folder / "state.json")
            fingerprint = state["latest"].get(jid)
            record = state["events"].get(fingerprint, {})
            request = record.get("human_request") or {}
            if (fingerprint != _signature(job) or record.get("status") != "needs_input"
                    or not _matches_request(request, jid, rid, project_id, story_id)):
                raise ValueError("项目或问题已更新，请查看当前处理进度。")
            cleaned = _answers(request, body["answers"])
            saved = {"request_id": rid, "job_id": jid, "project_id": project_id, "answers": cleaned,
                     "request": request, "trace_id": current_context()["trace_id"]}
            if story_id is not None:
                saved["story_job_id"] = story_id
            event("workbench.feedback.reply.accepted", saved, kind="state")
            write_json(target, saved)
    return {"status": "accepted", "request_id": rid, "job_id": jid}


def video_question(job, reason, basis, all_jobs):
    """Decisions about a superseded commission belong in its history."""
    orders = [j for j in all_jobs.values() if j.get("project_id") == job.get("project_id") and j["kind"] == "studio_video"]
    if not orders or orders[-1]["id"] not in {job["id"], job.get("commission_job_id")}:
        return None
    if reason == "current_plan_requires_confirmation":
        return human_request(job, "confirm_video_plan", basis, "新版拍摄计划需要确认",
                             "制作规格已变化。请在当前拍摄计划核对剧本版本、镜头和额度，再决定是否委托。",
                             submit_label="查看当前拍摄计划")
    if reason == "acceptance_retake_limit":
        return human_request(job, "review_retake_limit", basis, "自动重拍后仍有镜头需要审看",
                             "自动重拍已达到本次委托的上限。请查看实际镜头和验收报告，决定接下来的制作安排。",
                             submit_label="审看镜头与报告")
    return None


def partial_video_execution(job, jobs, *, stopped=False):
    """The latest terminal continuation of this exact commission owns its decision."""
    original = job if job["kind"] == "studio_video" else jobs.get(job.get("commission_job_id"))
    if not original or original["kind"] != "studio_video":
        return None
    pid, cid = original.get("project_id"), original.get("message_id")
    if (not re.fullmatch("[a-f0-9]{32}", str(cid)) or not re.fullmatch("[a-f0-9]{16}", str(pid))
            or not re.fullmatch("[a-f0-9]{32}", str(original.get("run_id")))):
        return None
    orders = [j for j in jobs.values() if j["kind"]=="studio_video" and j.get("project_id")==pid]
    if not orders or orders[-1]["id"] != original["id"]:
        return None
    children = [j for j in jobs.values() if j["kind"]=="pi_video" and j.get("project_id")==pid
                and j.get("commission_id")==cid and j.get("commission_job_id")==original["id"]]
    latest = children[-1] if children else original
    outcome = latest.get("result") or {}
    if (job["id"] not in {latest["id"],original["id"]} or latest.get("status") not in {"succeeded","interrupted","failed"}
            or latest.get("cancel_requested") or latest.get("trace_incomplete") or original.get("trace_incomplete")
            or original.get("cancel_requested") or outcome.get("task_complete")
            or not stopped and not (outcome.get("action_required") and
                (outcome.get("task_complete") is False or (outcome.get("extra_retake") or {}).get("partial")))):
        return None
    return original, latest


def partial_video_question(root, original, latest, jobs):
    """Offer only an action exposed for this order by the current read-only studio."""
    from contextlib import nullcontext
    from pianchang.bailian_video import load_config
    from pianchang.preview_access import ReadOnlyPreviewSource
    from pianchang.shot_approval import shot_review_panel
    from pianchang.studio_assets import studio_project_for_video
    from pianchang.studio_jobs import studio_video_plan
    from pianchang.video_execution import current_commission
    from trace_system import get_tracer

    pid, cid = original["project_id"], original["message_id"]
    source = ReadOnlyPreviewSource(root)
    source.state, source.lock = {"jobs":list(jobs.values())}, nullcontext()
    source.video_config = load_config(Path(root)/".env", allow_missing=True)
    with get_tracer().read_batch():
        view = studio_project_for_video(source,pid,include_delivery=True)
        current = current_commission(source,pid,view=view,jobs=source.state["jobs"])
        if (view.get("pending_script_ref") or current.get("commission_id") != cid or current.get("job_id") != original["id"]
                or ((current.get("last_execution") or {}).get("id") or original["id"]) != latest["id"]
                or current.get("script_ref") != view.get("script_ref") or current.get("spec_ref") != view.get("spec")):
            return None
        from tools.workbench_loop_observer import acceptance_retake_stopped
        budget = current.get("exhausted_budget")
        outcome = latest.get("result") or {}
        loop = outcome.get("loop") or {}
        stopped = acceptance_retake_stopped(root,original)
        claimed = bool((outcome.get("extra_retake") or {}).get("partial") or loop.get("reason")=="retake_approval_required")
        if not (budget or stopped or claimed):
            return None
        spec = view["spec"]["revision_id"]
        evidence_link = None
        if budget or stopped:
            review = shot_review_panel(source,pid,cid)
            if review.get("delivered"):
                return None
            rows = [{k:s[k] for k in ("shot_id","media_sha256","report_sha256")} for s in review.get("shots", [])
                    if s.get("can_approve") and s.get("media_sha256") and s.get("report_sha256")]
            if rows:
                scope = {"commission_id":cid,"spec_revision":spec,"review_shots":rows}
                ids = "、".join(r["shot_id"] for r in rows)
                return {**human_request(original,"review_commission_shots",scope,"已有镜头需要审看",
                    f"{ids} 已有视频和验收报告。请播放对应版本，核对未通过项，再决定是否人工通过。查看素材不会发起生成。",
                    submit_label="审看镜头与报告"),**scope}
            if stopped and review.get("job_id")==original["id"]:
                pins = [{k:s[k] for k in ("shot_id","media_sha256","report_sha256") if s.get(k)}
                        for s in review.get("shots",[]) if s.get("shot_id") and not s.get("human_approved")]
                scope = {"commission_id":cid,"spec_revision":spec,"review_shots":pins}
                evidence_link = {**human_request(original,"inspect_commission_reports",scope,"查看镜头与阻塞原因",
                    "当前没有可人工通过的镜头。旁路会复查原委托的候选和验收报告。", submit_label="查看镜头与阻塞原因"),**scope}
                if not budget and not claimed:
                    return evidence_link
        previous = {**view["production_ref"],"content":view["published_delivery"]} if view.get("production_ref") else None
        plan = studio_video_plan(view, previous)
        extra = plan.get("extra_retake") or {}
        pointer = extra.get("partial") or {}
        if pointer.get("request_id") == cid and extra.get("proposal_id") and extra.get("shot_ids"):
            checkpoint = Path(view["store_path"]).parent/"runs"/view["run_id"]/("video-spec-"+cid)/"partial.json"
            saved = read_json(checkpoint) if checkpoint.is_file() else {}
            if (saved.get("request_id") != cid or saved.get("shooting_plan") != current.get("plan")
                    or saved.get("project") != view["project"] or digest_json(saved) != pointer.get("sha256")):
                return evidence_link
            ids = "、".join(extra["shot_ids"])
            title, message = "原委托有镜头需要重拍授权", f"{ids} 已有供应商失败回执。请核对失败原因、复用镜头和新增调用上限，决定是否同意额外重拍。"
        elif budget and (plan.get("continuation") or {}).get("request_id")==cid and plan.get("plan_id") != current["plan"].get("plan_id"):
            title = "原委托额度已用完，请核对后续计划"
            message = budget["message"] + "请核对新计划的复用镜头、剩余制作范围和调用上限，再决定是否委托。"
        else:
            return evidence_link
        if plan.get("blocked_reason") or plan.get("unresolved_shots") or not plan.get("plan_id"):
            return evidence_link
        scope = {"commission_id":cid,"spec_revision":spec,"plan_id":plan["plan_id"]}
        if extra.get("proposal_id"):
            scope["proposal_id"] = extra["proposal_id"]
        if budget:
            scope["exhausted_budget"] = budget
        return {**human_request(original,"confirm_commission_budget",scope,title,message,
            submit_label="查看拍摄计划与额度"),**scope}
