"""Read-only classification of a tracked workbench job for the browser supervisor."""

import json
from pathlib import Path
import re
import sqlite3
from contextlib import closing


ID = re.compile(r"[0-9a-f]{32}")
HASH = re.compile(r"[0-9a-f]{64}")
CODE_ROOTS = {"pianchang", "pi_bridge", "tools", "trace_system"}
LOCAL_ERRORS = {"FileNotFoundError", "NotADirectoryError", "KeyError", "IndexError",
                "AttributeError", "TypeError", "NameError", "UnboundLocalError", "ImportError",
                "ModuleNotFoundError", "JSONDecodeError", "UnicodeDecodeError", "AssertionError"}
NETWORK_ERRORS = {"ConnectError", "ReadTimeout", "ConnectTimeout", "RemoteProtocolError",
                  "SSLError", "SSLEOFError", "TimeoutError", "ConnectionError", "URLError"}
TOOLS = {"write_draft", "patch_production_spec", "publish_production_spec", "check_production_spec",
         "publish_script", "check_script", "replace_beat", "generate_shot", "read_video_state",
         "read_production_spec", "finish_task", "prepare_reference_inputs"}
# The SDK stores large payloads by SHA-256. Select only the required fields from the stored JSON.
TRACE_BODY = """COALESCE((SELECT content FROM artifacts WHERE sha256=json_extract(events.data,'$."$artifact"')),
                        events.data)"""


def _read_db(path):
    return closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2))


def _frames(root, stack):
    frames = []
    for name, line in re.findall(r'^  File "([^"\r\n]+)", line (\d{1,7}), in ', str(stack), re.M):
        path = Path(name)
        try:
            relative = (root / path).resolve().relative_to(root.resolve())
        except (ValueError, OSError):
            continue
        if (len(relative.parts) >= 2 and relative.parts[0] in CODE_ROOTS
                and relative.suffix == ".py" and (root / relative).is_file()
                and re.fullmatch(r"[a-zA-Z0-9_./-]+", relative.as_posix())):
            frame = {"file":relative.as_posix(), "line":int(line)}
            if frame not in frames:
                frames.append(frame)
    return frames[-8:]


def _planner_not_sent(db, trace_id, run_span="", sequence_range=None, *, pi_session_id=None):
    """Pair the stopped Pi decision with its own SDK request and pre-send error."""
    scope = ""
    args = (trace_id,run_span,run_span)
    if pi_session_id and not run_span:
        scope = (f" AND span_id IN (SELECT span_id FROM events WHERE trace_id=? AND name='pi.sdk.ready' "
                 f"AND json_extract({TRACE_BODY},'$.sessionId')=?)")
        args += (trace_id,pi_session_id)
    end = db.execute("SELECT trace_seq,span_id,json_extract(data,'$.type') FROM events "
                     "WHERE trace_id=? AND name='pi.run.stopped' AND (?='' OR span_id=?)"
                     + scope + " ORDER BY trace_seq DESC LIMIT 2",args).fetchall()
    if len(end) != 1:
        return None
    stop, run_span, error_type = end[0]
    if error_type not in {"ConnectError","ConnectTimeout","ProxyError"}:
        return None
    failure = db.execute("SELECT trace_seq,json_extract(data,'$.purpose'),json_extract(data,'$.type') FROM events "
                         "WHERE trace_id=? AND span_id=? AND name='pi.model.failure' AND trace_seq<? "
                         "ORDER BY trace_seq DESC LIMIT 1",(trace_id,run_span,stop)).fetchone()
    if not failure or failure[1:] != ("decision",error_type):
        return None
    exchange = db.execute("SELECT span_id,json_extract(data,'$.error.type') FROM events WHERE trace_id=? "
                          "AND parent_span_id=? AND name='pi.model.exchange' AND kind='span.end' AND trace_seq<? "
                          "ORDER BY trace_seq DESC LIMIT 1",(trace_id,run_span,failure[0])).fetchone()
    if not exchange or exchange[1] != error_type:
        return None
    sdk = db.execute("SELECT span_id,json_extract(data,'$.error.type') FROM events WHERE trace_id=? AND parent_span_id=? "
                     "AND name='models.sdk.call' AND kind='span.end'",(trace_id,exchange[0])).fetchall()
    if len(sdk)!=1 or sdk[0][1]!=error_type:
        return None
    span = sdk[0][0]
    events = db.execute("SELECT trace_seq,name FROM events WHERE trace_id=? AND span_id=? AND name IN "
                        "('model.route','http.request','http.proxy.selected','http.error','http.response') ORDER BY trace_seq",
                        (trace_id,span)).fetchall()
    if [row[1] for row in events] != ["model.route","http.request","http.proxy.selected","http.error"]:
        return None
    route_seq, req, proxy_seq, err = [row[0] for row in events]
    if err >= failure[0] or sequence_range and not (sequence_range[0] <= req < err <= sequence_range[1]):
        return None
    def fields(seq, *paths):
        projection = ",".join(f"json_extract({TRACE_BODY}, ?)" for path in paths)
        return db.execute(f"SELECT {projection} FROM events WHERE trace_id=? AND trace_seq=?",
                          (*paths,trace_id,seq)).fetchone()
    error = fields(err, "$.error_type","$.dispatch","$.phase")
    if error[:2] != (error_type,"not_sent") or error[2] not in {"connect_tcp","start_tls"}:
        return None
    operation, model = fields(route_seq, "$.operation","$.route.model_id")
    method, url, body_model, wire = fields(req,"$.method","$.url","$.body.model","$.wire_body_sha256")
    mode, host, scheme, proxy_host, port = fields(proxy_seq,"$.mode","$.target_host","$.proxy_scheme","$.proxy_host","$.proxy_port")
    from urllib.parse import urlsplit
    if (operation != "completion" or method != "POST" or not isinstance(model,str) or not model or model!=body_model
            or not isinstance(url,str) or not url.endswith(("/chat/completions","/responses"))
            or not HASH.fullmatch(str(wire)) or not host or urlsplit(url).hostname!=host or mode not in {"direct","proxy"}
            or mode=="proxy" and not (scheme and proxy_host and type(port) is int)):
        return None
    from pianchang.story_spec import digest_json
    binding = {}
    order = db.execute("SELECT trace_seq FROM events WHERE trace_id=? AND kind='span.start' "
                       "AND name='agent.task.video' AND parent_span_id IS NULL",(trace_id,)).fetchall()
    if order:
        if len(order)!=1:
            return None
        cid, run_id = fields(order[0][0],"$.inputs.request.message_id","$.attributes.correlation_id")
        if not ID.fullmatch(str(cid)) or not ID.fullmatch(str(run_id)):
            return None
        binding = {"commission_id":cid,"run_id":run_id}
    ready = db.execute("SELECT trace_seq FROM events WHERE trace_id=? AND span_id=? AND name='pi.sdk.ready' "
                       "AND trace_seq<?",(trace_id,run_span,req)).fetchall()
    if len(ready) == 1:
        session_id, session_file = fields(ready[0][0],"$.sessionId","$.sessionFile")
        if isinstance(session_id,str) and session_id and isinstance(session_file,str) and session_file:
            binding.update(pi_session_id=session_id, session_file_digest=digest_json(session_file),
                           stopped_steps=fields(stop,"$.steps")[0])
    return {**binding,"trace_id":trace_id,"run_span_id":run_span,"span_id":span,"request_seq":req,"error_seq":err,
            "response_seq":None,"request_kind":"model","url_digest":digest_json(url),"model_id":model,
            "wire_body_sha256":wire,"dispatch":"not_sent","error_type":error_type,"phase":error[2],
            "proxy_mode":mode,"target_host":host,"proxy_scheme":scheme,"proxy_host":proxy_host,"proxy_port":port}


def _trace(root, trace_id, span_id, *, sequence_range=None):
    """Select only protocol metadata and error frames, scoped to this worker span."""
    evidence = {"outstanding":False, "network":False, "http_status":0, "trace_failed":False,
                "error_type":None, "frames":[], "tool":None}
    counts = {}
    # A job's request Trace can also contain sibling workers. Enter at the Pi run's span.
    scope = """WITH RECURSIVE owned(id) AS (
        SELECT ? UNION SELECT e.span_id FROM events e JOIN owned o ON e.parent_span_id=o.id
        WHERE e.trace_id=? AND e.kind='span.start'
    ) """ if span_id else ""
    selected = " AND span_id IN (SELECT id FROM owned)" if span_id else ""
    args = (span_id,trace_id,trace_id) if span_id else (trace_id,)
    if sequence_range:
        selected += " AND trace_seq BETWEEN ? AND ?"
        args += sequence_range
    body = TRACE_BODY if sequence_range else "data"
    query = scope + f"""SELECT span_id,kind,name,
        json_extract({body},'$.http_status'),json_extract({body},'$.type'),json_extract({body},'$.tool'),
        json_extract({body},'$.error.type'),json_extract({body},'$.error.stack'),json_extract({body},'$.stack')
        FROM events WHERE trace_id=?{selected}
        AND (name IN ('http.request','http.response','http.error','task.exception','pi.tool.error','pi.model.failure')
             OR (kind='span.end' AND json_extract({body},'$.status')='ERROR')) ORDER BY trace_seq"""
    with _read_db(root / "outputs/traces/trace.sqlite3") as db:
        bound = db.execute("SELECT 1 FROM events WHERE trace_id=? AND (?='' OR span_id=?) LIMIT 1",
                           (trace_id,span_id,span_id)).fetchone()
        if not bound:
            raise ValueError("No evidence for this execution")
        for span,kind,name,status,etype,tool,nested_type,nested_stack,stack in db.execute(query,args):
            if name=="http.request":
                counts[span] = counts.get(span,0)+1
            elif name=="http.response":
                counts[span] = max(0,counts.get(span,0)-1)
                evidence["http_status"] = status if type(status) is int else 0
            elif name=="pi.tool.error" and tool in TOOLS:
                evidence["tool"] = tool
            if kind=="span.end":
                etype,stack = nested_type,nested_stack
            if etype=="TraceError":
                evidence["trace_failed"]=True
            if etype in NETWORK_ERRORS:
                evidence["network"]=True
                evidence["network_error_type"]=etype
            if etype in LOCAL_ERRORS:
                frames=_frames(root,stack)
                if frames:
                    evidence.update(error_type=etype,frames=frames)
        if any(counts.values()) and not evidence["trace_failed"]:
            proof = _planner_not_sent(db,trace_id,span_id,sequence_range)
            if proof and counts.get(proof["span_id"]) == 1:
                counts[proof["span_id"]] = 0
                evidence["planner_not_sent"] = proof
    evidence["outstanding"] = any(counts.values())
    return evidence


def original_failure_evidence(root, job):
    """Look only at the recorded execution up to its failed transition, even after recovery."""
    jid, status = job.get("id"), job.get("status")
    trace_id = job.get("task_trace_id") or job.get("trace_id")
    event_id = job.get("event_id")
    unknown = {"code":"original_evidence_unavailable"}
    if not ID.fullmatch(str(jid)) or not ID.fullmatch(str(trace_id)) or status not in {"failed","interrupted"}:
        return unknown
    if event_id and not ID.fullmatch(str(event_id)):
        return unknown
    root = Path(root)
    try:
        with _read_db(root/"outputs/traces/trace.sqlite3") as db:
            # Older stamps only had a trace ID. Accept a unique matching boundary, never guess between failures.
            terminal = f"""SELECT trace_seq,span_id FROM events
                          WHERE trace_id=? AND name='task.transition_requested'
                          AND json_extract({TRACE_BODY},'$.job_id')=? AND json_extract({TRACE_BODY},'$.changes.status')=?"""
            args = (trace_id,jid,status)
            if event_id:
                terminal += " AND event_id=?"
                args += (event_id,)
            rows = db.execute(terminal+" LIMIT 2",args).fetchall()
            if len(rows)!=1:
                return unknown
            end,span = rows[0]
            previous = db.execute(f"""SELECT MAX(trace_seq) FROM events WHERE trace_id=? AND trace_seq<?
                AND name='task.transition_requested' AND json_extract({TRACE_BODY},'$.job_id')=?
                AND json_extract({TRACE_BODY},'$.changes.status') IN ('failed','interrupted','succeeded','cancelled')""",
                (trace_id,end,jid)).fetchone()[0] or 0
        evidence = _trace(root,trace_id,span,sequence_range=(previous+1,end))
    except (OSError,ValueError,sqlite3.Error):
        return unknown
    if evidence["trace_failed"]:
        return {"code":"trace_incomplete"}
    info = {}
    if evidence["outstanding"]:
        info.update(code="provider_receipt_unknown", receipt_unknown=True)
    elif evidence.get("planner_not_sent"):
        info.update(code="planner_request_not_sent",receipt_unknown=False)
    elif evidence["network"]:
        info["code"]="provider_connection_outcome_unknown"
    elif 400<=evidence["http_status"]<=599:
        info.update(code="provider_http_error", http_status=evidence["http_status"])
    if evidence["network"]:
        info["error_type"]=evidence["network_error_type"]
    if evidence["error_type"]:
        frame=evidence["frames"][-1]
        info.setdefault("code","local_tool_exception")
        info.update(error_type=evidence["error_type"],code_file=frame["file"],code_line=frame["line"])
    return info or unknown


def _local_checkpoint(root, job, doc, run):
    from pianchang.story_spec import digest_json
    check=doc.get("completion_check") or {}
    source=job.get("story_job_id") or job["id"]
    if (not ID.fullmatch(str(source)) or doc.get("status")!="needs_input"
            or check.get("scope")!="script_and_spec_publication_not_media_acceptance"
            or check.get("publication") or doc.get("action_required")):
        return None
    workspace=root/"outputs/pi/workspaces"/("story-"+source)
    path=workspace/"script/publication.json"
    receipt=json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
    for message in reversed(run.get("messages", [])):
        if message.get("role")!="user" or not isinstance(message.get("content"),str):
            continue
        try:
            entry=json.loads(message["content"])
        except ValueError:
            continue
        if not isinstance(entry,dict) or entry.get("type")!="tool_result" or entry.get("ok") is not True:
            continue
        saved=entry.get("result") or {}
        if (entry.get("tool")=="publish_script" and saved==receipt
                and HASH.fullmatch(str(receipt.get("sha256")))):
            return "continue_from_published_script"
        if entry.get("tool")=="write_draft" and not receipt:
            candidate=workspace/"script/candidate.json"
            if (candidate.is_file() and saved.get("needs_check") is True
                    and digest_json(json.loads(candidate.read_text(encoding="utf-8-sig")))==saved.get("candidate_sha256")):
                return "continue_from_saved_draft"
        if (entry.get("tool")=="read_production_spec" and receipt and saved.get("candidate")
                and not saved.get("existing_publication") and not saved.get("interruption_evidence")):
            actual=workspace/"spec-candidate.json"
            report=workspace/"validation-report.json"
            if (saved.get("script_ref")=={k:receipt.get(k) for k in ("asset_id","revision_id","sha256")}
                    and actual.is_file() and report.is_file()
                    and json.loads(actual.read_text(encoding="utf-8-sig"))==saved["candidate"]
                    and json.loads(report.read_text(encoding="utf-8-sig"))==saved.get("validation")):
                return "continue_from_saved_spec"
    return None


def linked_recovery_job(root, tracked_job, all_jobs, *, role, session_id="", message_id=""):
    """Find the owner of the exact submitted message, or a uniquely active frozen commission."""
    indexed={j["id"]:j for j in all_jobs if isinstance(j.get("id"),str) and ID.fullmatch(j["id"])}
    current_id=tracked_job.get("id")
    if role not in {"script","video"}:
        return None
    if session_id and message_id and ID.fullmatch(str(session_id)) and ID.fullmatch(str(message_id)):
        try:
            with _read_db(Path(root)/"outputs/pi/registry.sqlite3") as db:
                row=db.execute(
                    "SELECT m.job_id FROM messages m JOIN sessions s ON s.id=m.session_id "
                    "WHERE m.id=? AND s.id=? AND s.role=? AND m.role='user'",
                    (message_id,session_id,role)).fetchone()
        except (sqlite3.Error,OSError):
            return None
        child=indexed.get(row[0]) if row else None
        if (child and child["id"]!=current_id and child.get("kind")=="pi_"+role
                and (not tracked_job.get("project_id") or child.get("project_id")==tracked_job["project_id"])
                and child.get("session_id")==session_id):
            return child
    if (role!="video" or tracked_job.get("status") in {"queued","running"} or
            tracked_job.get("kind") not in {"studio_video","pi_video"}):
        return None
    if tracked_job["kind"]=="studio_video":
        source=tracked_job
        cid=source.get("message_id")
    else:
        source=indexed.get(tracked_job.get("commission_job_id"))
        cid=tracked_job.get("commission_id")
    if (not source or not ID.fullmatch(str(cid)) or not tracked_job.get("project_id")
            or source.get("message_id")!=cid or source.get("kind")!="studio_video"
            or source.get("project_id")!=tracked_job["project_id"]):
        return None
    active=[]
    for child in indexed.values():
        if (child["id"]==current_id or child.get("status") not in {"queued","running"}
                or child.get("project_id")!=source["project_id"]
                or (session_id and child.get("session_id")!=session_id)):
            continue
        if (child["id"]==source["id"] or
                (child.get("kind")=="pi_video" and child.get("commission_id")==cid
                 and child.get("commission_job_id")==source["id"])):
            active.append(child)
    return active[0] if len(active)==1 else None


def _published(root, job, result):
    check = result.get("completion_check") or {}
    pub = check.get("publication") or {}
    source = job.get("story_job_id") or (job["id"] if job["kind"]=="studio_script" else None)
    if source and ID.fullmatch(str(source)):
        receipt = root / "outputs/web/stories" / source / "studio.json"
    else:
        # External projects also publish an overview under the story's collaboration run.
        run_id=pub.get("run_id")
        if not ID.fullmatch(str(run_id)) or pub.get("story_id")!="story-"+run_id:
            return False
        if run_id!=job.get("run_id"):
            with _read_db(root/"outputs/pi/registry.sqlite3") as db:
                owner=db.execute("SELECT story_id FROM sessions WHERE id=? AND role='script'",
                                 (job.get("session_id"),)).fetchone()
            if not owner or owner[0]!=pub["story_id"]:
                return False
        receipt=root/"outputs/collaborations/runs"/run_id/"overview.json"
    if not receipt.is_file():
        return False
    saved = json.loads(receipt.read_text(encoding="utf-8-sig"))
    for key in ("script_ref","source_ref"):
        pin = pub.get(key)
        if (not isinstance(pin,dict) or not pin.get("revision_id")
                or not HASH.fullmatch(str(pin.get("sha256"))) or pin != saved.get(key)):
            return False
    return (result.get("task_complete") is True and check.get("complete") is True
            and saved.get("project_id")==pub.get("project_id")==job.get("project_id")
            and bool(job.get("project_id")))


def _delivered(root, job, doc, preview):
    pin=doc.get("production_ref")
    if (not isinstance(preview,dict) or preview.get("ready") is not True
            or preview.get("project_id")!=job.get("project_id") or not job.get("project_id")
            or not isinstance(pin,dict) or not HASH.fullmatch(str(pin.get("sha256")))
            or not pin.get("revision_id") or preview.get("production_ref")!=pin or doc.get("action_required")):
        return False
    commission=job.get("commission_id") or job.get("message_id")
    delivered=doc.get("result_message_id") or doc.get("message_id")
    if not ID.fullmatch(str(commission)) or not ID.fullmatch(str(delivered)):
        return False
    with _read_db(root/"outputs/collaborations/assets.sqlite3") as db:
        row=db.execute("""SELECT payload FROM messages WHERE message_id=? AND causation_id=?
                        AND message_type='production.result' AND sender='video' AND recipient='script'""",
                       (delivered,commission)).fetchone()
    return bool(row and json.loads(row[0]).get("production")==pin)


def _publication_quantities(root, job, doc, idea):
    from pianchang.app_service import project_id_for_path
    from pianchang.story_spec import digest_json
    from pi_bridge.requirements import check_quantities
    publication=doc["completion_check"]["publication"]
    source=job.get("story_job_id") or (job["id"] if job["kind"]=="studio_script" else None)
    if source and ID.fullmatch(str(source)):
        path=root/"outputs/web/stories"/source/"project.json"
    else:
        # External imports write project.json alongside their originating project.
        candidates=(root/"fixtures/project.json",root/"outputs/stories/last-key-step2-final/project.json")
        path=next((p for p in candidates if project_id_for_path(p)==job["project_id"]),None)
    if not path or not path.is_file():
        raise ValueError("Missing published project")
    project=json.loads(path.read_text(encoding="utf-8-sig"))
    bound=project["script_source"]
    script=bound["script"]
    digest=digest_json(project)
    if digest!=publication["source_ref"]["sha256"] or digest_json(script)!=bound["sha256"] or bound["sha256"]!=publication["script_ref"]["sha256"]:
        raise ValueError("Publication changed")
    index=root/"outputs/web/state.json"
    jobs=json.loads(index.read_text(encoding="utf-8-sig"))["jobs"] if index.is_file() else []
    source_job=next((j for j in jobs if j.get("id")==source),job)
    prompt=(source_job.get("story_input") or {}).get("idea") or idea or script.get("idea","")
    issues=[{k:r[k] for k in ("path","expected","actual")}
            for r in check_quantities(prompt,script,project) if r["status"]=="FAIL"]
    if not issues:
        return None
    # Shot count and duration can be corrected in the spec for the same script.
    safe=all(i["path"] in {"requested.shots","requested.shot_duration_s"} for i in issues) and index.is_file()
    if any(j.get("kind")=="studio_video" and j.get("project_id")==job["project_id"] for j in jobs):
        safe=False
    if safe:
        with _read_db(root/"outputs/collaborations/assets.sqlite3") as db:
            order=db.execute("SELECT 1 FROM messages WHERE story_id=? AND message_type='production.requested' LIMIT 1",
                             (publication.get("story_id") or "story-"+source,)).fetchone()
        safe=not order
    return {"issues":issues,"candidate_sha256":digest,"safe_to_continue":safe}


def acceptance_retake_stopped(root, job):
    """A stopped quality review is a human decision point for this exact order."""
    if job.get("kind") not in {"studio_video", "pi_video"} or job.get("status") not in {"failed", "interrupted", "succeeded"}:
        return False
    run_id = job.get("run_id")
    commission = job.get("commission_id") or job.get("message_id")
    if not all(ID.fullmatch(str(key)) for key in (run_id, commission)):
        return False
    checkpoint = Path(root)/"outputs/collaborations/runs"/run_id/("video-spec-" + commission)/"agent/run.json"
    try:
        run = json.loads(checkpoint.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    from pianchang.video_autonomy import stopped_retake_summary
    return bool(stopped_retake_summary(checkpoint.parent.parent, run))


def observe_job(root, job, *, session_id="", role="script", preview=None, idea=None):
    """Classify exactly the supplied job. Nothing from a creative payload is returned."""
    root = Path(root)
    jid = job.get("id")
    trace = job.get("worker_trace_id") or job.get("trace_id") or ""
    if not ID.fullmatch(str(trace)):
        trace = ""

    def result(status, reason, *, fault=None, safe_to_continue=False):
        return {"job_id":jid if isinstance(jid,str) and ID.fullmatch(jid) else "",
                "trace_id":trace, "status":status,"reason":reason,"code":reason,
                "safe_error":reason,"fault":fault,"safe_to_continue":safe_to_continue}

    if (not isinstance(jid,str) or not ID.fullmatch(jid) or role not in {"script","video"}
            or job.get("kind") not in {"pi_"+role,"studio_"+role}):
        return result("unknown","job_identity_mismatch")
    try:
        if session_id and job.get("session_id") != session_id:
            if job.get("session_id"):
                return result("unknown","session_mismatch")
            with _read_db(root/"outputs/pi/registry.sqlite3") as db:
                linked=db.execute("SELECT 1 FROM messages WHERE session_id=? AND job_id=? LIMIT 1",
                                  (session_id,jid)).fetchone()
            if not linked:
                return result("unknown","session_mismatch")
        if job.get("status") in {"queued","running"}:
            return result("running","job_active")
        if job.get("status") not in {"succeeded","interrupted","failed","cancelled"}:
            return result("unknown","job_status_unknown")
        doc = job.get("result") or {}
        path = root/"outputs/pi/runs"/jid/"run.json"
        run = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
        ctx=run.get("trace_context") or {}
        span=""
        if ID.fullmatch(str(ctx.get("trace_id"))):
            trace=ctx["trace_id"]
            span=ctx.get("span_id") or ""
        if job.get("trace_incomplete") or run.get("trace_incomplete"):
            return result("unknown","trace_incomplete")
        if job.get("cancel_requested") or job.get("status")=="cancelled" or doc.get("status")=="cancelled":
            return result("needs_input","job_cancelled")
        if role == "video" and acceptance_retake_stopped(root, job):
            return result("needs_input", "acceptance_retake_limit")
        if job["status"]=="succeeded":
            if role=="script" and _published(root,job,doc):
                mismatch=_publication_quantities(root,job,doc,idea)
                if mismatch:
                    status="interrupted" if mismatch["safe_to_continue"] else "needs_input"
                    return {**result(status,"published_quantity_mismatch"),**mismatch}
                return result("succeeded","script_and_spec_published")
            if role=="video" and _delivered(root,job,doc,preview):
                return result("succeeded","delivery_gate_passed")
            return result("needs_input","delivery_gate_pending" if role=="video" else "publication_unconfirmed")
        if not trace:
            return result("unknown","missing_task_evidence")
        evidence=_trace(root,trace,span)
        pending=run.get("pending_tool") or {}
        if evidence["trace_failed"]:
            return result("unknown","trace_incomplete")
        if evidence["outstanding"]:
            return result("unknown","provider_receipt_unknown")
        if evidence.get("planner_not_sent"):
            return result("transient","planner_request_not_sent")
        if pending and pending.get("effect") not in {"read"}:
            # This exact local save failure predates any written candidate. Never apply this
            # exception to another tool, in particular generators and publication operations.
            if not (pending.get("tool")=="write_draft" and evidence["tool"]=="write_draft"
                    and evidence["error_type"]=="FileNotFoundError"
                    and any(f["file"]=="pianchang/story_agent.py" for f in evidence["frames"])):
                return result("unknown","pending_operation")
        if evidence["http_status"] in {401,403,402}:
            return result("needs_input","provider_credentials_or_quota")
        if evidence["http_status"]==429:
            return result("transient","provider_rate_limited",safe_to_continue=role=="script" and not pending)
        if evidence["network"] or evidence["http_status"]>=500:
            return result("unknown","provider_connection_outcome_unknown")
        if evidence["error_type"]:
            fault={k:evidence[k] for k in ("error_type","tool","frames")}
            fault["code"]="local_tool_exception"
            return result("repairable",fault["code"],fault=fault)
        limit=(run.get("limits") or {}).get("max_steps")
        used=(run.get("steps") or 0)+(run.get("external_model_calls") or 0)
        if role=="script" and not pending and not (isinstance(limit,int) and used>=limit):
            checkpoint=_local_checkpoint(root,job,doc,run)
            if checkpoint:
                return result("interrupted",checkpoint,safe_to_continue=True)
        if doc.get("action_required") or doc.get("status")=="needs_input" or role=="video":
            return result("needs_input","workbench_action_required")
        return result("interrupted","incomplete_execution_without_diagnosis")
    except (OSError,sqlite3.Error,ValueError,TypeError,AttributeError,KeyError):
        return result("unknown","observation_evidence_unavailable")

