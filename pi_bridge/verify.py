"""Read-only live evidence audit. It never submits a model or media request."""
import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from pianchang.collab_store import CollabStore
from pianchang.story_spec import digest_json,validate_compilation
from trace_system import event,get_tracer,trace,traced,use_context
from .requirements import check_quantities
from .context import video_observation
from .runtime import persist

ROOT=Path(__file__).resolve().parent.parent


def _response_in_user_turn(message,response_id):
    content=message.get("content","")
    text=content if isinstance(content,str) else "\n".join(b.get("text","") for b in content if b.get("type")=="text")
    if "[message_id="+response_id+"]" in text:return True
    # Early live sessions used a separate delivery ID. The actual Pi user turn
    # still contains the explicit agent_response envelope and original bus ID.
    try:outer=json.loads(text)
    except ValueError:return False
    goal=outer.get("goal") if isinstance(outer,dict) else None
    if not isinstance(goal,str):return False
    payload=goal.split("\n",1)[1] if goal.startswith("[message_id=") and "\n" in goal else goal
    try:envelope=json.loads(payload)
    except ValueError:return False
    return isinstance(envelope,dict) and envelope.get("type")=="agent_response" and envelope.get("message_id")==response_id


@traced("pi.integration.verify_story",kind="validation",capture_inputs=False)
def verify_story(story):
    if len(story)!=32 or any(c not in "0123456789abcdef" for c in story):raise ValueError("Invalid story id")
    state=json.loads((ROOT/"outputs/web/state.json").read_text("utf-8"))
    original=next(j for j in state["jobs"] if j["id"]==story)
    folder=ROOT/"outputs/web/stories"/story
    studio=json.loads((folder/"studio.json").read_text("utf-8"))
    project=json.loads((folder/"project.json").read_text("utf-8"))
    script=project["script_source"]["script"]
    validation=validate_compilation(project,script)
    store=CollabStore(ROOT/"outputs/collaborations/assets.sqlite3")
    published=store.get_asset(studio["script_ref"]["asset_id"],studio["script_ref"]["revision_id"])
    checks=[]
    def check(path,expected,actual):
        row={"path":path,"expected":expected,"actual":actual,"status":"PASS" if expected==actual else "FAIL"}
        checks.append(row);event("pi.integration.check",row,kind="validation")
    check("script.published_content_hash",published["sha256"],digest_json(script))
    checks.extend(check_quantities(original["story_input"]["idea"],script,project))
    check("spec.declared_validation",True,validation["ready_for_generation"])
    db=sqlite3.connect(ROOT/"outputs/pi/registry.sqlite3");db.row_factory=sqlite3.Row
    try:sessions=[dict(r) for r in db.execute("SELECT * FROM sessions WHERE story_id=?",("story-"+story,))]
    finally:db.close()
    check("sessions.roles",["script","video"],sorted(s["role"] for s in sessions))
    session_evidence=[];observed_turns={}
    for session in sessions:
        path=Path(session["session_file"]) if session["session_file"] else None
        exists=bool(path and path.is_file());check("sessions."+session["role"]+".persisted",True,exists)
        if exists:
            raw=path.read_bytes();entries=[json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
            check("sessions."+session["role"]+".identity",session["pi_session_id"],entries[0].get("id"))
            user_turns=[e["message"] for e in entries if e.get("type")=="message" and e.get("message",{}).get("role")=="user"]
            observed_turns[session["role"]]=user_turns
            session_evidence.append({"role":session["role"],"session_id":session["id"],"pi_session_id":session["pi_session_id"],
                "file":str(path),"sha256":hashlib.sha256(raw).hexdigest(),"entries":len(entries),
                "user_turns":len(user_turns),"native_compactions":sum(e["type"]=="compaction" for e in entries)})
    check("sessions.distinct_pi_ids",len(sessions),len({s["pi_session_id"] for s in sessions}))
    check("context.actual_multiturn_per_role",True,bool(session_evidence) and all(s["user_turns"]>=2 for s in session_evidence))
    check("context.native_compaction_persisted",True,any(s["native_compactions"] for s in session_evidence))
    jobs=[j for j in state["jobs"] if j.get("story_job_id")==story and j.get("backend")=="pi"]
    successful=[j for j in jobs if (j.get("result") or {}).get("task_complete") and (j.get("result") or {}).get("project_id")]
    check("continuation.published_by_pi",True,bool(successful))
    requests=[m for m in store.messages(message_type="agent.conversation_requested") if m["story_id"]=="story-"+story]
    collaboration=[]
    for request in requests:
        replies=store.messages(causation_id=request["message_id"],message_type="agent.conversation_response")
        collaboration.append({"request_id":request["message_id"],"sender":request["sender"],"recipient":request["recipient"],
            "pins":request["payload"].get("pins"),"response_ids":[r["message_id"] for r in replies],"deliveries":request["deliveries"]})
    check("collaboration.actual_request_and_reply",True,any(c["response_ids"] for c in collaboration))
    check("collaboration.reply_reentered_source_session",True,any(_response_in_user_turn(turn,reply)
        for c in collaboration for reply in c["response_ids"] for turn in observed_turns.get(c["sender"],[])))
    unknown=[]
    for path in (ROOT/"outputs/collaborations/runs"/story/"compile-r1").glob("*-call.json"):
        call=json.loads(path.read_text("utf-8"))
        if call.get("status")!="COMPLETED":unknown.append({"path":str(path),"status":call.get("status"),"sha256":digest_json(call)})
    snapshots=[]
    for job in jobs:
        run=ROOT/"outputs/pi/runs"/job["id"]/"run.json"
        if not run.is_file():continue
        record=json.loads(run.read_text("utf-8"))
        for message in record.get("messages",[]):
            entry=json.loads(message["content"])
            if entry.get("tool")=="read_video_state" and entry.get("ok"):
                snapshots.append({"job_id":job["id"],"trace_id":job["trace_id"],"state":entry["result"],"turn_result":record.get("result")})
    latest=snapshots[-1] if snapshots else None
    preview=(latest or {}).get("state",{}).get("preview_gate",{})
    check("preview.unaccepted_media_stays_locked",False,preview.get("ready"))
    latest_result=(latest or {}).get("turn_result") or {}
    check("latest_video_consultation.finished_without_media_claim",True,latest_result.get("status")=="responded" and
        latest_result.get("turn_complete") is True and latest_result.get("task_complete") is False and latest_result.get("video_generations")==0)
    integrity=[]
    tracer=get_tracer(ROOT/"outputs/traces")
    for job in jobs:
        result=tracer.verify(job["trace_id"])
        integrity.append({"job_id":job["id"],"trace_id":job["trace_id"],"verification":result})
        check("trace."+job["id"]+".integrity",True,result["ok"])
    report={"schema_version":"pi-integration-proof/1","label":"actual project executions; this audit performs no model requests",
        "story_id":story,"project_id":studio["project_id"],"integration_verified":all(c["status"]=="PASS" for c in checks),
        "video_production_complete":False,"preview_ready":preview.get("ready"),"checks":checks,
        "script_ref":studio["script_ref"],"shot_count":len(project["shots"]),"sessions":session_evidence,
        "legacy_unconfirmed_operations_retained":unknown,"collaboration":collaboration,
        "executions":[{**{k:j.get(k) for k in ("id","kind","status","trace_id","backend")},
            "result":{k:(j.get("result") or {}).get(k) for k in ("status","task_complete","turn_complete","scope","summary","context_maintenance")}} for j in jobs],
        "trace_integrity":integrity,"latest_video_observation":{**latest,"state":video_observation(latest["state"])} if latest else None}
    return report


@traced("cli.pi.integration.verify",kind="cli")
def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("--story",required=True)
    parser.add_argument("--output",default="outputs/web/pi-integration-proof-20261112.json")
    args=parser.parse_args(argv)
    output=(ROOT/args.output).resolve()
    if not output.is_relative_to(ROOT/"outputs"):raise ValueError("Evidence output must stay inside outputs")
    with use_context(None,tracer=get_tracer(ROOT/"outputs/traces")),trace("pi.integration.live_audit",kind="validation",inputs={"story":args.story,"no_model_calls":True}):
        report=verify_story(args.story);persist(output,report)
    print(json.dumps({"integration_verified":report["integration_verified"],"output":str(output),"checks":len(report["checks"])},ensure_ascii=False))
    return 0 if report["integration_verified"] else 1


if __name__=="__main__":raise SystemExit(main())
