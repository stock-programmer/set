"""Re-evaluate a failed reference gate against the current published project without dispatching work."""
import json
import re
import sqlite3
from contextlib import closing, nullcontext
from pathlib import Path

from pianchang.app_service import read_json
from pianchang.asset_dispatch import asset_work_state, check_video_asset_readiness
from pianchang.asset_diagnostics import IMAGE_ACCESS_ERRORS
from pianchang.bailian_video import VideoError
from pianchang.preview_access import ReadOnlyPreviewSource
from pianchang.reference_assets import SUBJECT_KINDS
from pianchang.studio_assets import _shot_layout, studio_asset_catalog
from pianchang.story_spec import digest_json
from trace_system import TraceError, get_tracer, traced


_PREFIX = "以下镜头缺少已选用的参考资产："
_BATCH = {"asset_production", "asset_regenerate"}
_ID = re.compile("[a-f0-9]{32}")
_PHASES = {"before_send","connect_tcp","start_tls","send_request_headers","send_request_body","receive_response_headers",
           "receive_response_body"}


def _missing(message):
    if not isinstance(message, str) or not message.startswith(_PREFIX):
        return None
    rows = [item.split(" · ") for item in message[len(_PREFIX):].split("、")]
    if not rows or any(len(row) != 3 or not all(row) for row in rows):
        return None
    return rows


def _diagnostics(job):
    return ((job.get("result") or {}).get("asset_diagnostics") or {}).get("items") or []


def _unit_calls(root, job, subject, purpose):
    """Match each response to its actual POST in the same unit and SDK span."""
    trace = job.get("trace_id")
    if not _ID.fullmatch(trace or ""):
        return []
    sql = """
        WITH RECURSIVE spans(id) AS (
            SELECT span_id FROM events WHERE trace_id=:trace
              AND kind='span.start' AND name='studio.assets.produce_item'
              AND json_extract(data,'$.inputs.job_id')=:job
              AND json_extract(data,'$.inputs.project_id')=:project
              AND json_extract(data,'$.inputs.subject_id')=:subject
              AND json_extract(data,'$.inputs.purpose')=:purpose
            UNION
            SELECT e.span_id FROM events e JOIN spans s ON s.id=e.parent_span_id
              WHERE e.trace_id=:trace AND e.kind='span.start'
        )
        SELECT e.trace_seq,e.span_id,e.name,
          json_extract(e.data,'$.method'),json_extract(e.data,'$.http_status'),
          json_extract(e.data,'$.error_type'),
          json_type(e.data,'$.body.audio'),json_type(e.data,'$.body.input.text'),
          json_extract(e.data,'$.dispatch'),json_extract(e.data,'$.phase'),
          json_extract(e.data,'$.mode'),json_extract(e.data,'$.url'),
          json_extract(e.data,'$.winerror'),json_extract(e.data,'$.ssl_reason'),
          json_extract(e.data,'$.operation'),json_extract(e.data,'$.route.model_id'),
          json_extract(e.data,'$.body.model'),json_extract(e.data,'$.body.error.code'),
          json_extract(e.data,'$.action'),json_extract(e.data,'$.attempt'),json_extract(e.data,'$.limit')
        FROM events e JOIN spans s ON s.id=e.span_id
        WHERE e.trace_id=:trace AND e.name IN ('model.route','http.request','http.response','http.error','http.proxy.selected','asset.reference.next')
        ORDER BY e.trace_seq
    """
    path = root / "outputs/traces/trace.sqlite3"
    if not path.is_file():
        return []
    calls = []
    open_requests = {}
    image_models = {}
    requested_image_models = {}
    remaining_attempts = 1
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
        for seq, span, name, method, status, error, audio, tts_text, dispatch, phase, proxy, url, winerror, ssl_reason, operation, route_model, model, code, action, attempt, limit in db.execute(sql, {
                "trace":trace, "job":job["id"], "project":job["project_id"],
                "subject":subject, "purpose":purpose}):
            if name == "asset.reference.next":
                remaining_attempts = (limit - attempt + 1 if action == "generate" and type(attempt) == type(limit) == int
                                      and 1 <= attempt <= limit <= 2 else 1)
            elif name == "model.route":
                image_models.pop(span, None)
                if (operation == "image_generation" and isinstance(route_model, str)
                        and re.fullmatch(r"[A-Za-z0-9][\w.:-]{0,127}", route_model, re.ASCII)):
                    image_models[span] = route_model
            elif name == "http.request":
                open_requests.pop(span, None)
                requested_image_models.pop(span, None)
                if method == "POST":
                    call = {"trace_id":trace,"span_id":span,"request_seq":seq,"response_seq":None,
                            "request_kind":"audio" if purpose=="voice" and (audio or tts_text) else "model"}
                    if isinstance(url,str):
                        call["url_digest"] = digest_json(url)
                        if (purpose != "voice" and model and model == image_models.get(span)
                                and url.partition("?")[0].rstrip("/").endswith("/images/generations")):
                            requested_image_models[span] = (model, remaining_attempts)
                            call.update(request_kind="image", model_id=model)
                    open_requests[span] = call
                    calls.append(call)
            elif span in open_requests:
                if name == "http.response":
                    call = open_requests.pop(span)
                    call.update(response_seq=seq, http_status=status)
                    image = requested_image_models.pop(span, None)
                    if image and code in IMAGE_ACCESS_ERRORS and status == IMAGE_ACCESS_ERRORS[code]:
                        call.update(request_kind="image", model_id=image[0], remaining_attempts=image[1], provider_code=code)
                elif name == "http.error":
                    call=open_requests[span]
                    call.update(error_seq=seq,
                        error_type=error if re.fullmatch(r"[a-zA-Z0-9_]{1,64}", error or "") else "TransportError",
                        phase=phase if phase in _PHASES else "unknown",
                        dispatch="not_sent" if dispatch=="not_sent" and phase in {"before_send","connect_tcp","start_tls"} else "unknown")
                    if type(winerror) is int:
                        call["winerror"]=winerror
                    if isinstance(ssl_reason,str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,79}",ssl_reason):
                        call["ssl_reason"]=ssl_reason
                    if dispatch is None and error=="ConnectError" and call["request_kind"]=="audio" and _ID.fullmatch(span):
                        from tools.workbench_loop_legacy_wire import classify_legacy_voice_call
                        proof=classify_legacy_voice_call(root,call)
                        if (proof and proof.get("delivery")=="not_sent" and proof.get("request_seq")==call["request_seq"]
                                and proof.get("error_seq")==seq and proof.get("proof")=="legacy_httpx_connect_before_origin_write"):
                            call.update(dispatch="not_sent",phase="connect_or_tls",proof=proof["proof"],end_seq=proof["end_seq"])
                            if type(proof.get("errno")) is int:
                                call["errno"]=proof["errno"]
                elif name == "http.proxy.selected" and proxy in {"proxy","direct"}:
                    open_requests[span]["proxy_mode"] = proxy
    return calls


def _stale_source(catalog, publication):
    """Prove that the active gate is reading a bound copy of an earlier source cut."""
    current = publication.get("source_ref") or {}
    active = catalog.get("spec") or {}
    if not current.get("asset_id") or not active.get("revision_id"):
        return None
    project = read_json(Path(catalog["project_path"]))
    if digest_json(project) != current.get("sha256") or _shot_layout(project) == _shot_layout(catalog["project"]):
        return None
    with closing(sqlite3.connect(Path(catalog["store_path"]).resolve().as_uri()+"?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        cuts = db.execute("SELECT revision_id,sha256,content FROM assets WHERE story_id=? AND asset_id=? "
                          "AND kind='input_project' ORDER BY seq DESC",(catalog["story_id"],current["asset_id"]))
        latest = cuts.fetchone()
        if not latest or latest[:2] != (current["revision_id"],current["sha256"]):
            return None
        pin = db.execute("SELECT revision_id,sha256 FROM assets WHERE story_id=? AND asset_id=? "
                         "AND kind='production_spec' ORDER BY seq DESC LIMIT 1",
                         (catalog["story_id"],active["asset_id"])).fetchone()
        if pin != (active["revision_id"],active["sha256"]) or digest_json(catalog["project"]) != active["sha256"]:
            return None
        layout = _shot_layout(catalog["project"])
        matches = []
        for revision, sha, content in cuts:
            prior = json.loads(content)
            if digest_json(prior)==sha and _shot_layout(prior)==layout:
                matches.append(revision)
        if len(matches)==1:
            return {"expected_source_revision":current["revision_id"],"actual_source_revision":matches[0],
                    "expected_shot_count":len(project["shots"]),"actual_shot_count":len(catalog["project"]["shots"])}
    return None


def _continuation(source, catalog, tracked, jobs, evidence):
    """Return only an existing continuation command recognized by the workbench."""
    from pianchang.bailian_video import load_config
    from pianchang.studio_live_progress import _order, confirmed_video_reservations
    from pianchang.video_execution import current_commission
    from tools.workbench_loop_observer import _trace

    if evidence.get("requires_plan_reconfirmation"):
        return {"reason":"current_plan_requires_confirmation"}
    if evidence["receipt_unknown"]:
        return {"reason":"provider_receipt_unknown"}
    original = tracked if tracked["kind"]=="studio_video" else jobs.get(tracked.get("commission_job_id"),{})
    cid = original.get("message_id")
    if (not _ID.fullmatch(cid or "") or original.get("kind")!="studio_video" or
            tracked["kind"]=="pi_video" and tracked.get("commission_id")!=cid):
        return {"reason":"frozen_commission_unavailable"}
    planner = evidence.get("transport_failure",{})
    if (planner.get("request_kind")=="model" and
            (planner.get("commission_id")!=cid or planner.get("run_id")!=original.get("run_id"))):
        return {"reason":"commission_reconciliation_required","receipt_unknown":True}
    if (any(j.get("cancel_requested") or j.get("trace_incomplete") for j in (original,tracked)) or
            any(j["status"] in {"running","queued"} and j["kind"] in {"studio_video","pi_video",*_BATCH} for j in jobs.values())):
        return {"reason":"frozen_commission_unavailable"}
    pid=original["project_id"]
    source.video_config=load_config(source.root/".env",allow_missing=True)
    current=current_commission(source,pid,view=catalog,jobs=list(jobs.values()))
    last=current.get("last_execution") or {}
    plan=current.get("plan") or {}
    body=original.get("_autonomy_body") or {}
    if (current.get("commission_id")!=cid or current.get("job_id")!=original["id"] or
            not current.get("resumable") or current.get("status") not in {"failed","interrupted"} or
            last.get("id",tracked["id"])!=tracked["id"] or
            current.get("delivery_status") not in {"pending","received","accepted","blocked","unknown"} or
            not (plan.get("acceptance_loop") or {}).get("autonomy_version")):
        return {"reason":"frozen_commission_unavailable"}
    if (catalog.get("pending_script_ref") or plan.get("project_id")!=pid or
            plan.get("production_ref")!=catalog.get("production_ref") or
            body.get("spec_ref",catalog["spec"])!=catalog["spec"] or
            body.get("script_ref",catalog["script_ref"])!=catalog["script_ref"] or
            body.get("project_sha256",catalog["spec"]["sha256"])!=catalog["spec"]["sha256"] or
            not re.fullmatch(r"[a-f0-9]{64}",plan.get("plan_id") or "") or
            body.get("plan_id")!=plan.get("base_plan_id",plan["plan_id"]) or
            original.get("plan_id")!=body["plan_id"] or body.get("proposal_id")!=plan.get("proposal_id") or
            bool(body.get("repair_existing_only"))!=bool(plan.get("repair_existing_only")) or
            any(not current.get(k) or current[k]!=catalog.get(v) or original.get(k)!=current[k]
                for k,v in (("script_ref","script_ref"),("spec_ref","spec")))):
        return {"reason":"current_plan_requires_confirmation","requires_plan_reconfirmation":True}
    if not _ID.fullmatch(original.get("run_id") or "") or original["run_id"]!=catalog.get("run_id"):
        return {"reason":"frozen_commission_unavailable"}
    work=Path(catalog["store_path"]).parent/"runs"/original["run_id"]/("video-spec-"+cid)
    order,_,delivery,accepted_trace=_order(source.root,original["run_id"],cid,pid,original)
    if (order.get("shooting_plan")!=plan or current["delivery_status"]!=delivery or
            order.get("max_video_generations")!=(plan.get("execution_limits") or {}).get("max_video_generations")):
        return {"reason":"frozen_commission_unavailable"}
    if delivery=="pending":
        # claim() writes the first receipt atomically before the video worker can submit anything.
        with closing(sqlite3.connect(Path(catalog["store_path"]).resolve().as_uri()+"?mode=ro",uri=True)) as db:
            claimed=db.execute("SELECT 1 FROM receipts WHERE message_id=? AND agent='video' LIMIT 1",(cid,)).fetchone()
        limits=current.get("limits") or {}
        frozen=plan["execution_limits"]
        if (claimed or work.exists() or current["usage"] is not None or last or limits.get("source")!="frozen_commission" or
                any(type(frozen.get(k)) is not int or frozen[k]!=limits.get(k) or frozen[k]<0
                    for k in ("max_video_generations","max_model_calls")) or not frozen["max_model_calls"]):
            return {"reason":"commission_reconciliation_required"}
        return {"safe_to_continue":True,"reason":"frozen_commission_ready","commission_id":cid,
                "continuation_checkpoint":digest_json([cid,plan["plan_id"],"unclaimed"]),
                "remaining_video_generations":frozen["max_video_generations"],"remaining_model_calls":frozen["max_model_calls"]}
    checkpoint=read_json(work/"agent/run.json")
    paid_decision = planner.get("request_kind") == "model" and bool(checkpoint.get("video_generations"))
    if current["delivery_status"]=="blocked" and not paid_decision:
        from pianchang.video_autonomy import _can_resume_unprepared_pi_order, can_prepare_reused_continuation, recoverable_order
        stopped=checkpoint.get("status")=="needs_input" and (checkpoint.get("result") or {}).get("reason")=="pi_stopped"
        planned = False
        if evidence.get("transport_failure",{}).get("request_kind")=="model":
            from pianchang.video_autonomy import can_resume_planned_pi_order
            planned = can_resume_planned_pi_order(work,plan,checkpoint)
        if not (planned or recoverable_order(work) or stopped and (can_prepare_reused_continuation(work,plan,checkpoint) or
                                                                   _can_resume_unprepared_pi_order(work,plan,checkpoint))):
            return {"reason":"frozen_commission_unavailable"}
    limits=current.get("limits") or {}
    usage=current.get("usage") or {}
    frozen=plan.get("execution_limits") or {}
    maximum=frozen.get("max_video_generations")
    model_limit=frozen.get("max_model_calls")
    used=usage.get("reserved_video_generations")
    calls=usage.get("model_calls")
    if (type(maximum) is not int or type(model_limit) is not int or type(used) is not int or type(calls) is not int or
            limits.get("source")!="frozen_commission" or limits.get("max_video_generations")!=maximum or
            limits.get("max_model_calls")!=model_limit or checkpoint.get("shooting_plan")!=plan or
            not 0<=used<=maximum or checkpoint.get("video_generations")!=used or
            usage.get("remaining_video_generations")!=maximum-used or not 0<=calls<model_limit):
        return {"reason":"commission_budget_unavailable"}
    if (checkpoint.get("status") not in {"needs_input","interrupted","error","budget_exhausted"} or
            checkpoint.get("pending_tool") or usage.get("unconfirmed_strategy_calls") or
            (checkpoint.get("result") or {}).get("task_complete") or
            (work/"autonomy.json").is_file() and read_json(work/"autonomy.json").get("provider_blockers")):
        return {"reason":"commission_reconciliation_required"}
    context=checkpoint.get("trace_context") or {}
    if (not _ID.fullmatch(context.get("trace_id") or "") or not re.fullmatch("[a-f0-9]{16,32}",context.get("span_id") or "") or
            context["trace_id"]!=(tracked.get("worker_trace_id") or accepted_trace)):
        return {"reason":"commission_reconciliation_required"}
    trace=_trace(source.root,context["trace_id"],context["span_id"])
    if (trace["outstanding"] or trace["trace_failed"]
            or planner.get("request_kind")=="model" and planner!=trace.get("planner_not_sent")):
        return {"reason":"commission_reconciliation_required","receipt_unknown":True}
    if paid_decision:
        from pianchang.video_autonomy import can_resume_paid_pi_order
        confirmed = can_resume_paid_pi_order(work, plan, checkpoint, planner, request_id=cid, run_id=original["run_id"],
                    script=order["script"], output=source.video_config.output_dir, limits=order.get("execution_limits"))
    else:
        confirmed = confirmed_video_reservations(source.video_config.output_dir, work, plan, original["script_ref"],
                                                 checkpoint, created_at=original.get("created_at"))
    if not confirmed:
        return {"reason":"commission_reconciliation_required","receipt_unknown":True}
    if maximum==used and set(plan["new_shots"]+plan["retake_shots"])-set(checkpoint["reserved_shots"]):
        return {"reason":"commission_budget_unavailable"}
    return {"safe_to_continue":True,"reason":"frozen_commission_ready","commission_id":cid,
            "continuation_checkpoint":digest_json([cid,checkpoint,used,calls]),
            "remaining_video_generations":maximum-used,"remaining_model_calls":model_limit-calls}


@traced("workbench_loop.recheck_dependency", kind="validation", capture_inputs=False)
def recheck_dependency(root, job):
    with get_tracer().read_batch():
        result = _recheck_dependency(root, job)
    failure = (result or {}).get("transport_failure") or {}
    if failure.get("request_kind")=="model" and result.get("safe_to_continue"):
        from tools.workbench_loop_connectivity import check_planner_route
        probe = check_planner_route(Path(root),failure)
        result["connectivity"] = probe
        if probe.get("status")!="reachable":
            result.update(status="awaiting_evidence",reason="planner_route_unavailable",safe_to_continue=False)
    return result


def _recheck_dependency(root, job):
    kind = job.get("kind")
    original = _missing(job.get("error"))
    planner = None
    worker = job.get("worker_trace_id")
    if (kind in {"studio_video","pi_video"} and job.get("status") in {"failed","interrupted"} and not original
            and _ID.fullmatch(str(worker)) and not job.get("cancel_requested") and not job.get("trace_incomplete")):
        from tools.workbench_loop_observer import _planner_not_sent, _read_db
        try:
            with _read_db(Path(root)/"outputs/traces/trace.sqlite3") as db:
                planner = _planner_not_sent(db,worker,pi_session_id=job.get("pi_session_id"))
        except (OSError,ValueError,sqlite3.Error):
            return None
    if job.get("status") not in {"failed","interrupted"} or (
            not (kind in {"studio_video","pi_video"} and (original or planner)) and not (kind in _BATCH and _diagnostics(job))):
        return None
    root = Path(root)
    pid = job.get("project_id")
    out = {"status":"blocked","reason":"dependency_evidence_unavailable","original_missing_count":
           len(original) if original else len(_diagnostics(job)), "missing_count":None,
           "current_shot_count":None,"source_revision":None,"spec_revision":None,
           "related_job_id":None,"receipt_unknown":True,"provider_calls":[],"missing_references":[],
           "safe_to_continue":False}
    if not re.fullmatch("[a-f0-9]{16}", pid or ""):
        return out
    try:
        source = ReadOnlyPreviewSource(root)
        source.state = read_json(source.state_dir / "state.json")
        source.lock = nullcontext()
        jobs = {j["id"]:j for j in source.state["jobs"] if j.get("project_id")==pid}
        tracked = jobs.get(job["id"])
        if (not tracked or (tracked["kind"],tracked.get("trace_id"),tracked.get("status")) !=
                (kind,job.get("trace_id"),job.get("status")) or
                original and _missing(tracked.get("error"))!=original):
            return out
        catalog = studio_asset_catalog(source, pid, include_project=True)
        project = catalog["project"]
        publication_path = Path(catalog["project_path"]).parent / "studio.json"
        publication = read_json(publication_path) if publication_path.is_file() else {}
        out["source_revision"] = (publication.get("source_ref") or {}).get("revision_id")
        out.update(current_shot_count=len(project["shots"]),spec_revision=catalog["spec"]["revision_id"])
        submitted = (tracked.get("_autonomy_body") or {}).get("spec_ref")
        if isinstance(submitted,dict):
            out.update(original_spec_revision=submitted.get("revision_id"),
                       requires_plan_reconfirmation=submitted!=catalog["spec"])
        work = asset_work_state(source, pid, catalog) if not planner else {}
        units = {(unit["subject_id"],unit["purpose"]):unit for unit in work.get("items",[])}
        if kind in _BATCH:
            targets = {(u["subject_id"],u["purpose"]) for u in _diagnostics(job)}
            if not targets.issubset(units):
                return {**out, "reason":"reference_scope_changed"}
            missing = [key for key in targets if not units[key].get("selected_pin")]
            missing_count = len(missing)
        else:
            readiness_project = project
            if (tracked.get("_autonomy_body") or {}).get("repair_existing_only"):
                from pianchang.studio_recovery import read_incomplete_production
                available = (read_incomplete_production(catalog) or {}).get("available") or {}
                shots = [shot for shot in project["shots"] if shot["id"] in available]
                if not shots:
                    return {**out,"reason":"original_repair_scope_unavailable"}
                readiness_project = {**project,"shots":shots}
            try:
                check_video_asset_readiness(source, pid, readiness_project, catalog=catalog)
            except ValueError as exc:
                rows = _missing(str(exc))
                if rows is None:
                    raise
            else:
                rows = []
            out["missing_references"] = [
                {"shot_id":sid, "subject_id":subject, "purpose":purpose} for sid,subject,purpose in rows
                if re.fullmatch(r"[A-Za-z0-9_-]{1,64}",sid) and
                re.fullmatch(r"[A-Za-z0-9_:-]{1,96}",subject) and purpose in SUBJECT_KINDS][:3]
            missing = {(subject,purpose) for _,subject,purpose in rows}
            missing_count = len(rows)
        out["missing_count"] = missing_count
        if planner:
            if tracked.get("worker_trace_id")!=worker:
                return out
            out.update(receipt_unknown=False,transport_failure=planner)
            if missing_count:
                return {**out,"status":"awaiting_evidence","reason":"reference_selection_pending"}
            out.update(_continuation(source,catalog,tracked,jobs,out))
            if out.get("safe_to_continue"):
                out["status"]="cleared"
            elif out.get("receipt_unknown"):
                out["status"]="awaiting_evidence"
            return out
        original_units = {(row[1],row[2]) for row in original} if original else targets
        related = set()
        unresolved = set()
        uncertain_units = set()
        image_rejections = {}
        incomplete = False
        for subject, purpose in sorted(set(missing) | original_units):
            unit = units.get((subject, purpose), {})
            owner_ids = {job["id"]} if kind in _BATCH else {unit.get("job_id")}
            if kind not in _BATCH:
                # A later candidate can change the current unit's job_id. Keep original
                # failed batches for this exact unit in the receipt check.
                owner_ids.update(j["id"] for j in jobs.values() if j["kind"] in _BATCH
                    and (not tracked.get("created_at") or (j.get("created_at") or "")<=tracked["created_at"])
                    and any((row.get("subject_id"),row.get("purpose"))==(subject,purpose) for row in _diagnostics(j)))
            owners = [jobs[i] for i in sorted(filter(None,owner_ids)) if i in jobs and jobs[i]["kind"] in _BATCH]
            if not owners:
                incomplete = True
            for owner in owners:
                related.add(owner["id"])
                calls = _unit_calls(root, owner, subject, purpose)
                if (not calls or purpose=="voice" and not any(c["request_kind"]=="audio" for c in calls) or
                        any(c.get("dispatch")!="not_sent" and (c["response_seq"] is None or
                            type(c.get("http_status")) is not int or not 200<=c["http_status"]<500 or
                            c["http_status"]==408) for c in calls)):
                    unresolved.add(owner["id"])
                    uncertain_units.add((subject, purpose))
                elif ((subject, purpose) in missing and unit.get("status") == "needs_attention" and not unit.get("selected_pin")
                      and owner["id"] == unit.get("job_id") and owner["status"] in {"failed", "interrupted"}
                      and calls[-1].get("provider_code") in IMAGE_ACCESS_ERRORS):
                    image_rejections[(subject, purpose)] = calls[-1]
                out["provider_calls"].extend(calls)
        links=unresolved or related
        out["related_job_id"] = next(iter(links)) if len(links)==1 else None
        out["receipt_unknown"] = incomplete or bool(unresolved)
        audio = [c for c in out["provider_calls"] if c["request_kind"]=="audio"]
        failed_audio = [c for c in audio if c.get("error_type") and c["response_seq"] is None]
        if failed_audio:
            # Report an unresolved attempt before a proven pre-send fault.
            out["transport_failure"] = next((c for c in reversed(failed_audio) if c["dispatch"]=="unknown"),failed_audio[-1])
        if missing_count:
            out.update(status="awaiting_evidence",reason="provider_receipt_unknown" if out["receipt_unknown"]
                       else "reference_selection_pending")
            blocked_images = [{"subject_id": subject, "purpose": purpose, "model_id": call["model_id"],
                               "provider_code": call["provider_code"], "http_status": call["http_status"],
                               "remaining_attempts": call["remaining_attempts"],
                               "job_id": units[(subject, purpose)]["job_id"]}
                              for (subject, purpose), call in sorted(image_rejections.items())
                              if (subject, purpose) not in uncertain_units]
            if blocked_images:
                models = sorted({item["model_id"] for item in blocked_images})
                codes = {item["provider_code"] for item in blocked_images}
                statuses = {item["http_status"] for item in blocked_images}
                out["human_intervention"] = {
                    "kind": "reference_image_model_access",
                    "http_status": next(iter(statuses)) if len(statuses) == 1 else None,
                    "provider_code": next(iter(codes)) if len(codes) == 1 else None,
                    "model_id": models[0] if len(models) == 1 else None, "model_ids": models, "items": blocked_images,
                    "steps": ["旁路将核对方舟账号可见的生图模型和接入点，并尝试修复参考图配置。",
                              "需要账号权限或专属接入点时，工作台会弹窗说明需要提供的信息。",
                              "线路恢复后，工作台沿用原资产委托，自动续做缺少的参考图并自检。"],
                }
                if not out["receipt_unknown"] and len(blocked_images) == len(set(missing)):
                    out.update(status="needs_input", reason="reference_image_model_access")
            if (kind in _BATCH and len(missing)==len(targets)==len(audio)==len(failed_audio)==1 and
                    missing[0][1]=="voice" and not out["receipt_unknown"] and failed_audio[0]["dispatch"]=="not_sent"
                    and failed_audio[0]["phase"] in {"connect_tcp","start_tls","connect_or_tls"}
                    and not tracked.get("_voice_recovery_from")):
                out["reason"]="voice_request_not_sent"
            if kind in _BATCH and len(_diagnostics(job))==1:
                from tools.workbench_loop_voice_review import review_evidence
                item=_diagnostics(job)[0]
                review=review_evidence(root,job,item,units.get((item["subject_id"],item["purpose"])),catalog)
                if review:
                    out.update(reason="voice_review_input_unavailable",voice_review=review)
        else:
            # A later selection can clear the local gate while an older provider charge remains unknown.
            out.update(status="cleared",reason="current_reference_gate_passed")
            if original:
                out.update(_continuation(source,catalog,tracked,jobs,out))
        if kind=="studio_video" and not tracked.get("message_id") and not catalog.get("production_ref"):
            mismatch = _stale_source(catalog, publication)
            if mismatch:
                out.update(status="repairable",reason="active_spec_uses_superseded_source",version_mismatch=mismatch,
                           fault={"code":"active_spec_uses_superseded_source","error_type":"StaleProductionSpec",
                                  "frames":[{"file":"pianchang/studio_assets.py","function":"_read_view"}],
                                  "tool":"check_video_asset_readiness"})
        return out
    except TraceError:
        raise
    except (OSError, ValueError, KeyError, TypeError, VideoError, sqlite3.Error):
        if out["status"]=="cleared" and original:
            out.update(reason="commission_reconciliation_required",safe_to_continue=False)
        return out
