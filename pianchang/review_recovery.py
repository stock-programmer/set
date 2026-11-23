"""Resume verified content observations and receipt assembly for the current commission."""
import json
import re
import sqlite3
import time
from http.client import HTTPException
from pathlib import Path
from urllib import error, request

from pianchang.acceptance import file_sha256
from pianchang.story_spec import digest_json
from trace_system import TraceError, event, traced


OWNERS = {"studio_video", "pi_video", "studio_review"}


def _read(path):
    return json.loads(Path(path).read_bytes())


def _receipts(refs, directory, unresolved):
    seen = set()
    pending = [(ref,None) for ref in refs]
    while pending:
        ref, criteria = pending.pop()
        path = Path(ref["path"]).resolve()
        if not path.is_relative_to(directory):
            continue
        identity = (path,ref["sha256"], None if criteria is None else frozenset(criteria))
        if identity in seen:
            continue
        seen.add(identity)
        receipt = _read(path)
        if digest_json(receipt) != ref["sha256"]:
            raise ValueError("验收回执指纹已变更。")
        if criteria is None:
            criteria = {r["criterion_id"] for r in receipt.get("rows",[]) if r.get("status") == "UNKNOWN"} & unresolved
        if not criteria:
            continue
        if "dispatch_evidence" in receipt:
            yield path, receipt, criteria
        pending.extend((child,criteria) for child in receipt.get("source_receipts", []))
        if receipt.get("fallback_from"):
            pending.append((receipt["fallback_from"],criteria))


def _active_attempt(path, receipt, owner, snapshot, service):
    from pianchang.review_dispatch import _verified_unsent
    proof = dict(receipt["dispatch_evidence"])
    if proof.pop("status", None) != "not_sent" or proof.get("commission_id") != snapshot["request_id"]:
        return None
    call = Path(proof["call_path"]).resolve()
    if (not re.fullmatch(r"undispatched-[0-9a-f]{16,32}\.json", path.name) or call.parent != path.parent
            or call.name not in {"call.json", "audio-call.json", "visual-call.json"} or not call.is_file()
            or (path.parent/"receipt-p4.json").exists() or not re.fullmatch("[a-f0-9]{32}",str(proof.get("id")))):
        return None
    record = _read(call)
    inputs = record.get("request") or {}
    binding = record.get("attempt") or {}
    if (record.get("status") != "submitted_or_unknown" or record.get("wire") != proof
            or binding.get("call_path") != str(call) or binding.get("input_sha256") != digest_json(inputs)
            or record.get("input_sha256") != binding.get("input_sha256")
            or any(receipt.get(k) != inputs.get(k) for k in ("video","references","criteria","context"))):
        return None
    base = dict(inputs)
    if call.name == "visual-call.json":
        if base.pop("modality", None) != "independent_visual":
            audio = _read(call.parent/"audio-call.json")
            if audio.get("status") != "completed" or base.pop("audio_sha256", None) != digest_json(audio["response"]):
                return None
    if (digest_json(base) != receipt.get("request_sha256") or path.parent.name != digest_json(base)[:12]
            or proof["trace_id"] != (owner.get("worker_trace_id") or owner.get("trace_id"))
            or receipt["trace_id"] != proof["trace_id"] or not _verified_unsent(record,tracer=service.tracer)):
        return None
    vision = service.vision_config
    continuity = getattr(vision,"continuity_config",None)
    primary = continuity if base.get("audiovisual") and "audio_review" not in base else vision
    primary = primary or vision
    def matches(inputs,config):
        return bool(config and inputs.get("model") == config.model and inputs.get("endpoint") == config.base_url
                    and inputs.get("account","") == getattr(config,"route_account",""))
    if (not matches(base,primary) or "audio_review" in base and not matches(base["audio_review"],continuity)
            or not getattr(continuity if call.name == "audio-call.json" else primary,"api_key",None)):
        return None
    assets = [base["video"],*base["references"]]
    if (main_audio := base["context"].get("main_audio")):
        assets.append(main_audio)
    if any(file_sha256(asset["path"]) != asset["sha256"] for asset in assets):
        return None
    return str(call), proof["id"], proof["url"]


def current_reports(work, snapshot, *, allow_changed_neighbors=False):
    from pianchang.content_review import VERSION, STATE_AUDIT_VERSION
    path = work/"reports.json"
    if not path.is_file():
        return None
    selected = {sid:item["clip"] for sid,item in snapshot["available"].items() if item.get("clip")}
    if not selected or set(selected) != set(snapshot["available"]):
        return None
    media = {sid:(clip["media_sha256"],clip["media_path"]) for sid,clip in selected.items()}
    records = _read(path)
    result = {}
    for sid, clip in selected.items():
        item = records.get(sid) or {}
        path = Path(item.get("report_path","")).resolve()
        if not path.is_relative_to((work/"cv2").resolve()) or not path.is_file():
            return None
        report = _read(path)
        evidence = _read(path.parent/"content.json")
        selected_media = {s:(v["sha256"],v["path"]) for s,v in evidence["selected_media"].items()}
        if (digest_json(report) != item["report_sha256"] or report.get("shot_id") != sid
                or report.get("content_review_version") != VERSION or report.get("state_audit_version") != STATE_AUDIT_VERSION
                or report.get("project_sha256") != snapshot["project_sha256"] or report.get("media_sha256") != clip["media_sha256"]
                or report.get("content_evidence_sha256") != digest_json(evidence)
                or evidence.get("project_sha256") != snapshot["project_sha256"] or evidence.get("media_sha256") != clip["media_sha256"]
                or selected_media.get(sid) != media[sid] or set(selected_media) != set(media)
                or not allow_changed_neighbors and selected_media != media):
            return None
        unresolved = {r["criterion_id"] for r in report["results"] if r.get("required") and r.get("status") == "UNKNOWN"}
        result[sid] = (item["report_sha256"],evidence["receipts"],unresolved)
    return result


def _closed_owner(tracer, trace_id, name):
    if not trace_id:
        return {}
    rows = tracer.get_events(trace_id,names=[name],limit=3)
    if ([r["kind"] for r in rows] != ["span.start","span.end"] or rows[0].get("parent_span_id")
            or rows[0]["span_id"] != rows[1]["span_id"]):
        return {}
    end = tracer.resolve(rows[1]["data"])
    if end.get("status") not in {"OK","ERROR"} or (tracer.resolve(end.get("error")) or {}).get("type") == "TraceError":
        return {}
    return tracer.resolve(rows[0]["data"])


@traced("studio.review.retry_evidence",kind="validation",capture_inputs=False,capture_output=False)
def review_retry(service, view, snapshot, owner, *, excluding=None):
    """Return a scope for one idempotent review job after verifying the currently selected evidence."""
    from pianchang.studio_recovery import _work
    from pi_bridge.recovery import strategy_external_usage
    from pianchang.reference_inputs import external_review_usage
    from pianchang.studio_retakes import _latest_request
    if (not snapshot or snapshot["active"] or not snapshot["available"] or not owner
            or owner.get("status") not in {"succeeded","failed","interrupted"} or owner.get("cancel_requested")
            or owner.get("trace_incomplete") or (owner.get("result") or {}).get("task_complete")
            or owner.get("project_id") != view["project_id"]):
        return None
    cid = snapshot["request_id"]
    if owner["kind"] == "studio_video":
        if owner["id"] != snapshot["job_id"] or owner.get("message_id") != cid:
            return None
    elif owner["kind"] == "pi_video":
        if owner.get("commission_job_id") != snapshot["job_id"] or owner.get("commission_id") != cid:
            return None
    elif owner["kind"] == "studio_review":
        root = _closed_owner(service.tracer,owner.get("trace_id"),"agent.task.studio_review")
        pointer = (root.get("inputs",{}).get("body") or {}).get("continuation")
        if root.get("attributes",{}).get("job_id") != owner["id"] or pointer != {"request_id":cid,"sha256":snapshot["sha256"]}:
            return None
    else:
        return None
    if owner["kind"] in {"studio_video","pi_video"}:
        root = _closed_owner(service.tracer,owner.get("worker_trace_id"),"agent.task.video")
        if ((root.get("inputs",{}).get("request") or {}).get("message_id") != cid
                or root.get("attributes",{}).get("correlation_id") != view["run_id"]):
            return None
    work = _work(view,cid)
    reports = current_reports(work,snapshot)
    legacy_only = not reports
    if legacy_only and snapshot.get("retake_stop"):
        from pianchang.legacy_sound_review import sidecar_commission
        if sidecar_commission(getattr(service,"root",None),view["project_id"],cid,snapshot["job_id"]):
            reports = current_reports(work,snapshot,allow_changed_neighbors=True)
    if not reports:
        return None
    allowed, urls, bound, affected = {}, set(), {}, {}
    for sid,(report_sha,refs,unresolved) in reports.items():
        bound[sid] = report_sha
        if legacy_only:
            continue
        for receipt_path, receipt, criteria in _receipts(refs,(work/"cv2").resolve(),unresolved):
            attempt = _active_attempt(receipt_path,receipt,owner,snapshot,service)
            if attempt:
                call, attempt_id, url = attempt
                allowed[call] = attempt_id
                urls.add(url)
                affected.setdefault(sid,set()).update(criteria)
    if not allowed and not snapshot.get("retake_stop"):
        return None
    if any(file_sha256(item["clip"]["media_path"]) != item["clip"]["media_sha256"] for item in snapshot["available"].values()):
        return None
    order, _ = _latest_request(view)
    body = (order or {}).get("body") or {}
    limits = body.get("execution_limits") or {}
    limit = limits.get("max_model_calls")
    run = _read(work/"agent/run.json")
    steps = run.get("steps")
    if (not order or order.get("id") != cid or run.get("pending_tool") or run.get("trace_incomplete")
            or run.get("shooting_plan") != body.get("shooting_plan")
            or (body.get("shooting_plan") or {}).get("execution_limits",limits) != limits or type(steps) is not int or steps < 0
            or type(limit) is not int or (run.get("limits") or {}).get("max_steps") != limit):
        return None
    remaining = limit - steps - strategy_external_usage(work) - len(external_review_usage(work/"reference-inputs")["reservations"])
    if not allowed or remaining <= 0:
        if not snapshot.get("retake_stop"):
            return None
        from pianchang.legacy_sound_review import legacy_sound_scope, sidecar_commission
        if (body.get("script") != view.get("script_ref") or body.get("template") != view.get("spec")
                or not ((run.get("shooting_plan") or {}).get("acceptance_loop") or {}).get("autonomy_version")):
            return None
        legacy = legacy_sound_scope(service,view,snapshot,owner,work,reports,remaining,excluding=excluding)
        if legacy:
            return legacy
        if (legacy_only or view.get("pending_script_ref") or not view.get("script_ref") or not view.get("spec")
                or snapshot.get("script_ref") != view["script_ref"] or snapshot.get("spec_ref") != view["spec"]
                or any(snapshot.get(key) for key in ("unknown_shots","failed_shots","missing_shots"))):
            return None
        pid = view["project_id"]
        driver = sidecar_commission(getattr(service,"root",None),pid,cid,snapshot["job_id"])
        if not driver or (owner["kind"] == "pi_video" and owner["id"] not in driver["submitted"]):
            return None
        with service.lock:
            others = [j for j in service.state["jobs"] if j["id"] != excluding]
            original = next((j for j in others if j["id"] == snapshot["job_id"]),{})
            if (original.get("kind") != "studio_video" or original.get("project_id") != pid
                    or original.get("message_id") != cid or original.get("run_id") != view["run_id"]
                    or original.get("status") not in {"succeeded","failed","interrupted"}
                    or original.get("cancel_requested") or original.get("trace_incomplete")
                    or owner_for(others,pid) != owner or review_work_in_flight(others,pid,cid)):
                return None
        from pianchang.internal_review_scope import internal_cut_scope
        cuts = internal_cut_scope(service,view,snapshot,owner,work,reports,remaining,excluding=excluding)
        if cuts:
            return cuts if not cuts["urls"] or remaining > 0 else None
        from pianchang.sequence_review_recovery import sequence_assembly_scope
        assembly = sequence_assembly_scope(service,view,snapshot,owner,work,reports,remaining,excluding=excluding)
        return assembly if assembly and assembly["urls"] == [] else None
    token = digest_json([cid,owner["id"],snapshot["sha256"],bound,allowed])
    return {"token":token, "key":"review-not-sent-"+digest_json([cid,sorted(allowed.values())]),
            "allowed":allowed, "urls":sorted(urls), "criteria":{sid:sorted(ids) for sid,ids in affected.items()}}


def owner_for(jobs, pid, *, excluding=None):
    rows = [j for j in jobs if j.get("project_id") == pid and j["kind"] in OWNERS and j["id"] != excluding
            and (j["kind"] != "pi_video" or j.get("commission_id") or j.get("_pi_continue_commission"))]
    if not rows or any(j["status"] in {"queued","running"} for j in rows):
        return None
    return rows[-1]


def review_work_in_flight(jobs, pid, cid):
    from pianchang.shot_approval import active_production
    return active_production(jobs,pid,cid) or any(
        j.get("project_id") == pid and j.get("status") in {"queued","running"}
        and (j.get("kind") in {"agent","generate","check"}
             or j.get("kind") == "pi_video" and j.get("_pi_mode") != "inspect") for j in jobs)


@traced("studio.review.retry_connectivity",kind="http",capture_inputs=False)
def _reachable(service, urls):
    from pianchang.bailian_observer import BailianObserver
    config = service.vision_config
    configs = [c for c in (config,getattr(config,"continuity_config",None)) if c and c.api_key]
    for url in urls:
        route = next((c for c in configs if url == c.base_url.rstrip("/")+"/chat/completions"),None)
        if not route:
            return False
        req = request.Request(url,headers={"User-Agent":"Pianchang-Review-Check/1","Accept":"*/*"},method="HEAD")
        event("studio.review.probe.request",{"method":req.get_method(),"url":req.full_url,"headers":dict(req.header_items())},kind="http")
        try:
            with BailianObserver(route)._opener.open(req,timeout=8) as response:
                status = response.status
        except error.HTTPError as exc:
            status = exc.code
            exc.close()
        except (OSError,ValueError) as exc:
            event("studio.review.probe.error",{"type":type(exc).__name__},kind="http")
            return False
        event("studio.review.probe.response",{"status":status},kind="http")
        if not (200 <= status < 300 or status in {400,401,403,404,405}):
            return False
    return True


def submit_review_scope(service, pid, snapshot, scope, jobs):
    """Return (job ID, route unavailable). The durable submission key is shared by both watchdogs."""
    if not scope or any(j.get("_idempotency_key") == scope["key"] for j in jobs):
        return None, False
    if scope["urls"] and not _reachable(service,scope["urls"]):
        return None, True
    pointer = {"request_id":snapshot["request_id"],"sha256":snapshot["sha256"]}
    job = service.submit("studio_review",pid,{"continuation":pointer,"retry_observation":scope["token"]},scope["key"])
    return job["id"], False


def poll_review_retries(service, seen):
    from pianchang.legacy_sound_review import sidecar_commission
    from pianchang.studio_assets import studio_project_for_video
    from pianchang.studio_recovery import _work, read_incomplete_production
    with service.lock:
        jobs = [dict(j) for j in service.state["jobs"]]
    commissions = {j.get("project_id"):j for j in jobs if j["kind"] == "studio_video"}
    for pid in {j["project_id"] for j in jobs if j.get("project_id") and j["kind"] in OWNERS}:
        owner = owner_for(jobs,pid)
        if not owner:
            continue
        config = getattr(service,"vision_config",None)
        if not config:
            continue
        routes = tuple((getattr(c,"model",None),getattr(c,"base_url",None),getattr(c,"route_account",None),
                        bool(getattr(c,"api_key",None))) for c in (config,getattr(config,"continuity_config",None)))
        origin = commissions.get(pid) or {}
        cid, run_id = origin.get("message_id"), origin.get("run_id")
        stamp = None
        if getattr(service,"root",None) and all(re.fullmatch("[0-9a-f]{32}",str(v)) for v in (cid,run_id)):
            path = Path(service.root)/"outputs/collaborations/runs"/run_id/("video-spec-"+cid)/"reports.json"
            try:
                stat = path.stat()
                stamp = (stat.st_mtime_ns,stat.st_size)
            except OSError:
                pass
        try:
            watched = sidecar_commission(getattr(service,"root",None),pid,cid,origin.get("id")) if cid else None
        except (OSError,ValueError,TypeError,KeyError):
            watched = None
        watch_status = (watched["id"], owner["id"] in watched["submitted"], review_work_in_flight(jobs,pid,cid)) if watched else None
        signature = (owner["id"],owner["status"],owner.get("updated_at"),routes,stamp,watch_status)
        previous, due = seen.get(pid,(None,0))
        if previous == signature and due > time.monotonic():
            continue
        seen[pid] = (signature,float("inf"))
        try:
            view = studio_project_for_video(service,pid)
            if cid:
                if not re.fullmatch("[0-9a-f]{32}",str(cid)):
                    continue
                if (not next((_work(view,cid)/"cv2").rglob("undispatched-*.json"),None)
                        and not watched):
                    continue
            snapshot = read_incomplete_production(view)
            scope = review_retry(service,view,snapshot,owner)
            _, unavailable = submit_review_scope(service,pid,snapshot,scope,jobs)
            if unavailable:
                seen[pid] = (signature,time.monotonic()+60)
        except TraceError:
            seen.pop(pid,None)
            raise
        except (ValueError,OSError,KeyError,TypeError,AttributeError,sqlite3.Error,HTTPException) as exc:
            event("studio.review.retry_waiting",{"project_id":pid,"reason":type(exc).__name__},kind="state")
            seen[pid] = (signature,time.monotonic()+60)
