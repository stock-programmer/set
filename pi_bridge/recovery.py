"""Evidence-bound reconciliation of stopped Pi executions, never a model runner."""
import hashlib
import json
from pathlib import Path

from trace_system import event,traced
from .runtime import persist


@traced("pi.recovery.reconcile_checkpoint",kind="validation",capture_inputs=False)
def reconcile_checkpoint(service,job,work):
    path=Path(work)/"agent/run.json"
    if not path.is_file():return {"changed":False}
    record=json.loads(path.read_text("utf-8"));context=record.get("trace_context") or {}
    if record.get("backend")!="pi" or record.get("status")!="running":return {"changed":False}
    with service.lock:
        owners=[j for j in service.state["jobs"] if j.get("project_id")==job["project_id"] and
                (j.get("commission_id")==job.get("message_id") or j["id"]==job["id"])]
    if any(j.get("status") in {"running","queued"} for j in owners):return {"changed":False,"reason":"owner_active"}
    with service.tracer.read_batch():
        endings=service.tracer.get_events(context["trace_id"],span_id=context["span_id"],limit=1000)
        failure=next((r for r in reversed(endings) if r["kind"]=="span.end" and service.tracer.resolve(r["data"]).get("status")=="ERROR"),None)
        if failure is None and job.get("trace_id"):
            # A reboot/deployment may leave the child span open. The exclusive
            # AppService startup records positive owner-interruption evidence;
            # absence of a child end event alone is never considered proof.
            notices=service.tracer.get_events(job["trace_id"],names=["task.interrupted_detected"],limit=1000)
            failure=next((r for r in reversed(notices) if service.tracer.resolve(r["data"]).get("job_id")==job["id"]),None)
        if failure is None:return {"changed":False,"reason":"no_durable_terminal_evidence"}
        error=(service.tracer.resolve(failure["data"]).get("error") or {}) if failure["kind"]=="span.end" else {
            "message":"服务重新取得独占进程锁，已确认旧执行中断；原云端任务与未确认结果保留。"}
        primary=next((line.split("TraceError:",1)[1].strip() for line in error.get("stack","").splitlines() if "trace_system.core.TraceError:" in line),error.get("message","Pi 执行失败"))
        evidence={"event_id":failure["event_id"],"trace_id":failure["trace_id"],"seq":failure["trace_seq"],
                  "timestamp":failure["timestamp"],"sha256":failure["hash"]}
    raw=path.read_bytes();persist(path.with_name("before-reconcile-"+hashlib.sha256(raw).hexdigest()[:16]+".json"),json.loads(raw))
    record.update(status="interrupted",failure={"reason":"runtime_interruption","primary_error":primary,
        "trace_context":context,"terminal_evidence":evidence,"secondary_error":error.get("message")},
        result={"status":"interrupted","task_complete":False,"reason":"runtime_interruption","summary":primary})
    persist(path,record)
    event("pi.recovery.checkpoint_reconciled",{"path":str(path),"expected":"worker status agrees with closed ERROR root and terminal owner",
          "actual":"interrupted","status":"PASS","evidence":evidence,"budget_reset":False},kind="validation")
    return {"changed":True,"evidence":evidence,"primary_error":primary}


@traced("pi.recovery.design_uncertainty",kind="validation",capture_inputs=False)
def reconcile_design(work,record,tracer):
    """Retire an unconfirmed text design; never repeat its request or video POST."""
    pending=record.get("pending_tool") or {}
    if pending.get("tool")!="design_repair_strategy":return None
    origin=record.get("recovery_origin") or record.get("trace_context") or {}
    with tracer.read_batch():
        rows=tracer.get_events(origin["trace_id"],names=["pi.tool.request","video.autonomy.design_strategy","pi.loop.before_model","bailian.text.complete_json","http.request","http.response"],limit=10000)
        requests=[r for r in rows if r["name"]=="pi.tool.request" and tracer.resolve(r["data"]).get("call_id")==pending.get("call_id")]
        if len(requests)!=1:raise ValueError("不能唯一绑定中断的策略调用；保留未确认操作。")
        operation=requests[0];descendants={operation["span_id"]};observed=[];reserved_steps=[]
        for row in rows:
            if row["parent_span_id"] in descendants:descendants.add(row["span_id"])
            if row["name"]=="pi.loop.before_model" and row["kind"]=="span.end" and row["span_id"] in descendants and tracer.resolve(row["data"]).get("status")=="OK":
                reserved_steps.append(row["event_id"])
            if row["name"]=="http.request" and row["span_id"] in descendants:
                data=tracer.resolve(row["data"])
                observed.append({"event_id":row["event_id"],"trace_id":row["trace_id"],"seq":row["trace_seq"],
                    "wire_body_sha256":data.get("wire_body_sha256"),"provider_outcome":"unconfirmed"})
    path=Path(work)/"strategy-recovery.json";saved=json.loads(path.read_text("utf-8")) if path.exists() else {"operations":{}}
    key=pending["call_id"]
    receipt={"call_id":key,"shot_id":pending["arguments"]["shot_id"],"source_trace":origin["trace_id"],
             "request_evidence":observed,"already_reserved_pi_steps":reserved_steps,
             "external_calls_reserved":max(0,len(observed)-len(reserved_steps)),"status":"retired_unconfirmed_text_design",
             "new_video_posts":0,"next_action":"可在原剩余额度内设计新的替代方案；新请求必须携带本次未确认设计上下文，不能重放旧请求。"}
    saved["operations"].setdefault(key,receipt);persist(path,saved)
    event("pi.recovery.design_retired",receipt,kind="validation")
    return receipt


@traced("pi.recovery.strategy_usage",kind="validation",capture_inputs=False)
def strategy_external_usage(work):
    path=Path(work)/"strategy-recovery.json"
    if not path.exists():return 0
    return sum(r["external_calls_reserved"] for r in json.loads(path.read_text("utf-8"))["operations"].values())


@traced("pi.recovery.watch",kind="task",capture_inputs=False,capture_output=False)
def watch_recovery(service,stop):
    """Retry storage health, not paid calls, after fail-closed interruptions."""
    from trace_system import TraceError
    from pianchang.video_autonomy import enqueue_recovery
    while not stop.wait(15):
        with service.lock:
            jobs=list(service.state["jobs"])
            failed_orders={j.get("message_id") for j in jobs if j.get("_autonomy_enabled") and
                j.get("status") in {"failed","interrupted"} and not j.get("cancel_requested")}
            needed=any(j.get("trace_incomplete") and (j.get("_autonomy_enabled") or j.get("commission_id") in failed_orders) for j in jobs)
        if not needed:continue
        try:enqueue_recovery(service,recover_trace_failures=True)
        except TraceError:continue  # no effects were permitted without durable evidence
        except (OSError,ValueError,KeyError) as exc:
            event("pi.recovery.watch_blocked",{"type":type(exc).__name__,"reason":str(exc),"new_video_posts":0},kind="error")
