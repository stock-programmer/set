"""Audit the live local workflow without starting a model or video generation."""
import argparse
import hashlib
import http.client
import json
from pathlib import Path

from trace_system import event,get_tracer,span,trace,traced,use_context
from pi_bridge.runtime import persist

ROOT=Path(__file__).resolve().parents[1]


@traced("workflow.verify.http",kind="tool",capture_inputs=False)
def local_request(port,method,path,payload=None):
    body=json.dumps(payload,ensure_ascii=False).encode() if payload is not None else None
    headers={"Origin":f"http://127.0.0.1:{port}","Content-Type":"application/json"}
    with span("workflow.verify.exchange",kind="http",inputs={"method":method,"url":f"http://127.0.0.1:{port}"+path,"headers":headers,"body":payload}):
        connection=http.client.HTTPConnection("127.0.0.1",port,timeout=180)
        try:
            connection.request(method,path,body,headers)
            response=connection.getresponse();raw=response.read(8*1024*1024+1)
            event("workflow.verify.received",{"status":response.status,"headers":dict(response.getheaders()),
                "body":raw.decode("utf-8",errors="replace"),"sha256":hashlib.sha256(raw).hexdigest()},kind="http")
            if len(raw)>8*1024*1024:raise ValueError("Local response exceeded audit limit")
            return response.status,raw,response.getheader("X-Trace-Id")
        finally:connection.close()


@traced("workflow.verify.live",kind="validation",capture_inputs=False)
def verify(port,projects):
    checks=[];observations=[]
    def check(path,expected,actual):
        if isinstance(expected,set):expected=sorted(expected)
        if isinstance(actual,set):actual=sorted(actual)
        row={"path":path,"expected":expected,"actual":actual,"status":"PASS" if actual==expected else "FAIL"}
        checks.append(row);event("workflow.verify.check",row,kind="validation")
    status,html,_=local_request(port,"GET","/");check("page.available",200,status)
    text=html.decode();check("navigation.only_four_stages",4,text.count('data-step="'))
    for key in ("story","assets","video","review"):check("navigation."+key,True,'data-step="'+key+'"' in text)
    for key in ("studio","shots","checks","export","agent"):check("navigation.legacy_removed."+key,False,'data-step="'+key+'"' in text)
    status,body,_=local_request(port,"GET","/studio.js")
    check("video.duplicate_settings_removed",False,"高级执行设置" in body.decode())
    check("video.current_limits_present",True,"function studioExecutionLimits" in body.decode())
    for pid in projects:
        status,body,tid=local_request(port,"GET",f"/api/projects/{pid}");check(pid+".project",200,status)
        project=json.loads(body)
        status,body,studio_tid=local_request(port,"GET",f"/api/projects/{pid}/studio");check(pid+".studio",200,status)
        studio=json.loads(body);gate=studio["preview_gate"]
        status,snapshot_body,snapshot_tid=local_request(port,"GET",f"/api/workspace?project={pid}")
        check(pid+".workspace.available",200,status);snapshot=json.loads(snapshot_body)
        check(pid+".workspace.project",pid,snapshot["project"]["id"])
        check(pid+".workspace.gate",gate["ready"],snapshot["studio"]["preview_gate"]["ready"])
        for role in ("script","video"):check(pid+".workspace.conversation_scope."+role,studio["story_id"],snapshot["conversations"][role]["session"]["story_id"])
        check(pid+".shared_preview_decision",project["preview_gate"]["ready"],gate["ready"])
        check(pid+".same_delivery",project["preview_gate"]["production_ref"],gate["production_ref"])
        evidence={"project_id":pid,"project_trace":tid,"studio_trace":studio_tid,"workspace_trace":snapshot_tid,"gate":gate,"asset_counts":studio.get("asset_work",{}).get("counts")}
        execution=studio["video_execution"];evidence["video_execution"]=execution
        check(pid+".commission_scope",studio["story_id"],execution["story_id"])
        if execution.get("commission_id"):
            from pianchang.collab_store import CollabStore
            order=CollabStore(ROOT/"outputs/collaborations/assets.sqlite3").get_message(execution["commission_id"])
            check(pid+".frozen_generation_limit",order["payload"]["max_video_generations"],execution["limits"]["max_video_generations"])
        before={j["id"] for j in json.loads((ROOT/"outputs/web/state.json").read_text("utf-8"))["jobs"]}
        status,body,retired_tid=local_request(port,"POST",f"/api/projects/{pid}/agent",{"goal":"验证已退役入口","mode":"inspect"})
        rejected=json.loads(body);check(pid+".duplicate_agent_api_retired",410,status)
        check(pid+".duplicate_agent_rejection_code","AGENT_ENTRY_MERGED",rejected.get("code"))
        check(pid+".duplicate_agent_points_to_current_conversation",{"scope":{"project":pid},"role":"video"},rejected.get("conversation"))
        after={j["id"] for j in json.loads((ROOT/"outputs/web/state.json").read_text("utf-8"))["jobs"]}
        check(pid+".duplicate_agent_creates_no_task",before,after);evidence["retired_entry_trace"]=retired_tid
        if not gate["ready"]:
            before={j["id"] for j in json.loads((ROOT/"outputs/web/state.json").read_text("utf-8"))["jobs"]}
            status,_,export_tid=local_request(port,"POST",f"/api/projects/{pid}/export",{})
            check(pid+".legacy_export_rejected",409,status);check(pid+".no_export_in_project",None,project.get("export"))
            after={j["id"] for j in json.loads((ROOT/"outputs/web/state.json").read_text("utf-8"))["jobs"]}
            check(pid+".rejection_creates_no_task",before,after)
            evidence["export_rejection_trace"]=export_tid
            def media_link(value):
                if isinstance(value,dict):
                    if str(value.get("path","")).endswith(".mp4") and str(value.get("url","")).startswith("/assets/"):return value["url"]
                    for child in value.values():
                        found=media_link(child)
                        if found:return found
                elif isinstance(value,list):
                    for child in value:
                        found=media_link(child)
                        if found:return found
            url=media_link(studio)
            if url:
                status,_,media_tid=local_request(port,"GET",url);check(pid+".raw_media_rejected",409,status)
                evidence["media_rejection_trace"]=media_tid
        for role in ("script","video"):
            status,body,session_tid=local_request(port,"GET",f"/api/pi/sessions?project={pid}&role={role}")
            session=json.loads(body);check(pid+".session."+role,200,status)
            check(pid+".session_scope."+role,studio["story_id"],session["session"]["story_id"])
            evidence[role+"_session"]={"id":session["session"]["id"],"pi_session_id":session["session"]["pi_session_id"],"messages":len(session["messages"]),"trace_id":session_tid}
        observations.append(evidence)
    # Sets above are only compared, never serialized as a substitute log.
    for row in checks:
        for key in ("expected","actual"):
            if isinstance(row[key],set):row[key]=sorted(row[key])
    return {"schema_version":"workflow-live-verification/1","label":"actual local HTTP observations; no model or video generation",
        "verified":all(c["status"]=="PASS" for c in checks),"checks":checks,"observations":observations}


@traced("cli.workflow.verify",kind="cli")
def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("--port",type=int,default=8765)
    parser.add_argument("--project",action="append",required=True);args=parser.parse_args()
    if not 1024<=args.port<=65535 or any(len(p)!=16 or any(c not in "0123456789abcdef" for c in p) for p in args.project):raise ValueError("Invalid local port/project")
    with use_context(None,tracer=get_tracer(ROOT/"outputs/traces")),trace("workflow.live_verification",kind="validation",inputs={"projects":args.project,"no_generation":True}) as task:
        report=verify(args.port,args.project);report["trace_id"]=task.trace_id
        output=ROOT/"outputs/web"/("unified-workflow-verification-"+task.trace_id+".json");persist(output,report)
    print(json.dumps({"verified":report["verified"],"checks":len(report["checks"]),"trace_id":report["trace_id"],"report":str(output)},ensure_ascii=False))
    return 0 if report["verified"] else 1


if __name__=="__main__":raise SystemExit(main())
