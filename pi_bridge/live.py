"""Read-only conversation events projected from the shared Trace and checkpoints.

No second event log, task dispatch, or provider polling is performed here.
"""
import base64
import copy
import json
import re
from pathlib import Path

from trace_system import TraceError,event,traced

NAMES=("pi.session.event","pi.tool.request","pi.tool.response","pi.tool.error","pi.model.purpose",
       "pi.model.receipt","pi.sdk.ready","pi.run.stopped","pi.video.continuation_failed",
       "bailian.video.submit","bailian.video.get_task","video.autonomy.work_partition","media.process","export.process",
       "content.model.observe_video","content.prepare_cut","video.references.verify_receipt","reference.usage.verify_exchange",
       "pi.recovery.resuming","pi.recovery.design_retired","pi.model.failure","http.transport_error","video.autonomy.observe_pending")
ID=re.compile(r"^[a-f0-9]{32}$")


def _summary(value):
    if not isinstance(value,dict):return {}
    allowed={"shot_id","task_id","status","ready","summary","reason","error","accepted","execution_status",
             "operation","task_complete","turn_complete","media_accepted","verdict"}
    result={k:v[:2000] if isinstance(v,str) else v for k,v in value.items() if k in allowed and isinstance(v,(str,bool,int,float,type(None)))}
    for name in ("result","loop","output"):
        if isinstance(value.get(name),dict):result[name]=_summary(value[name])
    return result


@traced("pi.live.project_event",kind="validation",capture_inputs=False,capture_output=False)
def project_event(entry,tracer):
    """Only public assistant text and selected execution fields can leave Trace."""
    name=entry["name"]
    if name in {"bailian.video.submit","bailian.video.get_task"} and entry["kind"]!="span.end":return None
    data=tracer.resolve(entry["data"])
    if not isinstance(data,dict):return None
    public=None
    if name=="pi.session.event":
        typ=data.get("type");message=data.get("message") or {}
        if typ=="message_end" and message.get("role")=="assistant":
            content=message.get("content",[])
            text=content if isinstance(content,str) else "".join(c.get("text","") for c in content if isinstance(c,dict) and c.get("type")=="text")
            if text:public={"type":"assistant","text":text,"message_id":str(message.get("timestamp",entry["event_id"]))}
        elif typ=="assistant_text":public={"type":"assistant_delta","text":data.get("text",""),"message_id":data.get("message_id",entry["event_id"])}
        elif typ in {"compaction_start","compaction_end"}:public={"type":typ,"error":data.get("error")}
        elif typ=="tool_execution_start" and data.get("toolName")=="finish_task":public={"type":"finishing"}
    elif name=="pi.tool.request":
        args=data.get("arguments") or {}
        public={"type":"tool_start","tool":data.get("tool"),"call_id":data.get("call_id"),
                "arguments":{k:v for k,v in args.items() if k in {"shot_id","repair_id","target_role","strategy"} and isinstance(v,str)}}
    elif name in {"pi.tool.response","pi.tool.error"}:
        pending=data.get("pending") or {}
        public={"type":"tool_error" if name.endswith("error") else "tool_end","tool":data.get("tool"),
                "call_id":pending.get("call_id"),"result":_summary(data.get("result")),"error":data.get("error")}
    elif name=="pi.model.purpose":public={"type":"model_start","purpose":data.get("purpose")}
    elif name=="pi.model.receipt":public={"type":"model_end","model":data.get("model"),"usage":{k:v for k,v in (data.get("usage") or {}).items() if k in {"prompt_tokens","completion_tokens","total_tokens"} and type(v) is int},"provider_id":data.get("provider_id")}
    elif name=="pi.sdk.ready":public={"type":"session_ready","message_count":data.get("messages")}
    elif name in {"pi.run.stopped","pi.video.continuation_failed"}:public={"type":"error","error":data.get("reason") or data.get("error")}
    elif name=="pi.model.failure":public={"type":"error","error":data.get("message")}
    elif name=="http.transport_error":public={"type":"error","error":"传输查询未完成："+str(data.get("message","")),"cause_type":data.get("cause_type"),"errno":data.get("errno")}
    elif name=="video.autonomy.observe_pending":public={"type":"stage","stage":name,"running":entry["kind"]=="span.start","status":data.get("status")}
    elif name=="pi.recovery.resuming":public={"type":"recovery","summary":data.get("summary")}
    elif name=="pi.recovery.design_retired":public={"type":"recovery","summary":"已登记未确认的策略模型请求并保留已占用额度；旧请求不重放，继续核对其他可修问题。"}
    elif name in {"bailian.video.submit","bailian.video.get_task"} and entry["kind"]=="span.end":
        output=tracer.resolve(data.get("output")) or {};provider=output.get("output") or {}
        if provider.get("task_id"):public={"type":"video_status","task_id":provider["task_id"],"status":provider.get("task_status"),"error":provider.get("message")}
    elif name=="video.autonomy.work_partition":
        public={"type":"work_queue","actionable":len(data.get("actionable",[])),"blocked":len(data.get("blocked",[])),"exhausted":len(data.get("exhausted",[]))}
    elif name in {"media.process","export.process"}:
        if entry["kind"]=="span.start":
            args=(data.get("inputs") or {}).get("argv",[]);program=str(args[0]).replace("\\","/").rsplit("/",1)[-1] if args else "媒体工具"
            joined=" ".join(map(str,args));shot=re.search(r"\bS\d{3}(?:-S\d{3})?\b",joined)
            public={"type":"media_start","program":"ffprobe" if "ffprobe" in program else "ffmpeg",
                "action":"检查验收媒体" if "ffprobe" in program else "拼接衔接验收片段" if "concat" in joined else "处理验收片段",
                "shot_id":shot.group(0) if shot else None}
        elif entry["kind"]=="span.end":public={"type":"media_end","status":data.get("status"),"error":(data.get("error") or {}).get("message"),"duration_ms":data.get("duration_ms")}
    elif name in {"content.model.observe_video","content.prepare_cut","video.references.verify_receipt","reference.usage.verify_exchange"}:
        error=data.get("error");reason=error.get("message") if isinstance(error,dict) else error
        public={"type":"stage","stage":name,"running":entry["kind"]=="span.start","status":data.get("status"),"error":str(reason)[:2000] if reason else None}
    if public is None:return None
    return {"id":entry["event_id"],"trace_id":entry["trace_id"],"span_id":entry["span_id"],"seq":entry["trace_seq"],"timestamp":entry["timestamp"],**public}


def encode_cursor(value):
    return base64.urlsafe_b64encode(json.dumps(value,separators=(",",":")).encode()).decode().rstrip("=")


def decode_cursor(value):
    if not value:return {}
    if len(value)>8192:raise ValueError("实时事件游标过长")
    try:result=json.loads(base64.urlsafe_b64decode(value+"="*((-len(value))%4)))
    except (ValueError,TypeError):raise ValueError("实时事件游标无效") from None
    if not isinstance(result,dict) or len(result)>16 or any(not ID.fullmatch(str(k)) or type(v) is not int or v<0 for k,v in result.items()):raise ValueError("实时事件游标无效")
    return result


@traced("pi.live.sources",kind="validation",capture_inputs=False,capture_output=False)
def session_sources(service,session):
    with service.lock:
        jobs=copy.deepcopy([j for j in service.state["jobs"] if j.get("session_id")==session["id"]])
    active=next((j for j in jobs if j["id"]==session.get("active_job")),None)
    job=active or (max(jobs,key=lambda j:(j.get("updated_at") or j.get("created_at") or "",j.get("created_at") or "")) if jobs else None)
    if not job:return {"execution":None,"trace_ids":[]}
    execution={k:job.get(k) for k in ("id","status","message","error","trace_id","updated_at","cancel_requested","trace_incomplete")}
    traces=[job["trace_id"]] if ID.fullmatch(str(job.get("trace_id"))) else []
    # The native conversation hands the same session to a detached production
    # trace. A stale conversation agent_settled event is not production completion.
    cid=job.get("commission_id") or (job.get("message_id") if job.get("kind")=="studio_video" else None)
    if session["role"]=="video" and cid:
        with service.lock:
            source=next((dict(j) for j in service.state["jobs"] if j.get("id")==job.get("commission_job_id") or
                j.get("kind")=="studio_video" and j.get("project_id")==session.get("project_id") and j.get("message_id")==cid),None)
        if source and source.get("project_id")==session.get("project_id") and ID.fullmatch(str(source.get("run_id"))) and ID.fullmatch(str(cid)) and source.get("story_id")==session["story_id"]:
            path=service.root/"outputs/collaborations/runs"/source["run_id"]/("video-spec-"+cid)/"agent/run.json"
            if path.is_file():
                record=json.loads(path.read_text("utf-8"));context=record.get("trace_context") or {};tid=context.get("trace_id")
                pending=record.get("pending_tool") or {}
                if ID.fullmatch(str(tid)) and tid not in traces:traces.append(tid)
                recorded_status=record.get("status")
                effective="interrupted" if recorded_status=="running" and job.get("status") not in {"queued","running"} else recorded_status
                execution["worker"]={"status":effective,"recorded_status":recorded_status,"state_conflict":effective!=recorded_status,"trace_id":tid,"steps":record.get("steps"),
                    "tool":pending.get("tool"),"shot_id":(pending.get("arguments") or {}).get("shot_id"),"summary":(record.get("result") or {}).get("summary") if effective!="running" else None}
    return {"execution":execution,"trace_ids":traces}


@traced("pi.live.read",kind="tool",capture_inputs=False,capture_output=False)
def read_session_events(service,session,cursor):
    with service.tracer.read_batch():
        repo=service._pi_sessions.repo;session=repo.get(session["id"])
        source=session_sources(service,session);allowed=source["trace_ids"]
        # Client cursors never grant permission to read another story's Trace.
        positions={tid:cursor.get(tid,0) for tid in allowed};events=[];more=False
        for tid in allowed:
            page=service.tracer.get_events(tid,after=positions[tid],names=NAMES,limit=101)
            more=more or len(page)>100
            for raw in page[:100]:
                try:public=project_event(raw,service.tracer)
                except TraceError:raise
                except (ValueError,TypeError,KeyError,AttributeError) as exc:
                    public={"id":raw["event_id"],"trace_id":tid,"span_id":raw["span_id"],"seq":raw["trace_seq"],"timestamp":raw["timestamp"],
                            "type":"error","error":"此执行记录暂时无法显示："+type(exc).__name__+"；原证据保留在Trace。"}
                if public:events.append(public)
                positions[tid]=raw["trace_seq"]
        events.sort(key=lambda e:(e["timestamp"],e["trace_id"],e["seq"]))
        messages=repo.messages(session["id"])
        return {"session_id":session["id"],"active_job":session["active_job"],"execution":source["execution"],"messages":messages,
                "events":events,"cursor":encode_cursor(positions),"has_more":more,"trace_ids":allowed}
