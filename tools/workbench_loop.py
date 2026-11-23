"""Drive one real workbench scenario and resume it across verified Codex repairs."""
import argparse
import http.client
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from pianchang.app_service import read_json, write_json
from tools.launch_workbench import ROOT, _probe, _start_server
from tools.workbench_loop_restart import restart_original, workbench_http_identity
from trace_system import event, get_tracer, register_secret, trace, traced, use_context

WEB = ROOT / "outputs/web"
RUNS = ROOT / "outputs/workbench-loop"
TERMINAL = {"succeeded", "blocked", "unknown"}
ID = re.compile(r"[0-9a-f]{32}")
PROJECT = re.compile(r"[0-9a-f]{16}")


@contextmanager
def exclusive_driver():
    RUNS.mkdir(parents=True, exist_ok=True)
    with (RUNS / "driver.lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def record(state, phase, **fields):
    state.update(fields, phase=phase)
    entry = {"phase":phase, "at":time.time(), "job_id":state.get("job_id"),
             "reason":state.get("reason")}
    for key in ("fault","repair","observation","receipt","final_page","page_fault"):
        if fields.get(key):
            entry[key]=fields[key]
    state["history"].append(entry)
    event("workbench_loop.transition", {"run":state["id"], **entry}, kind="state")
    write_json(RUNS / state["id"] / "run.json", state)
    print(json.dumps(entry, ensure_ascii=False), flush=True)


@traced("workbench_loop.page", kind="tool", capture_inputs=False, capture_output=False)
def page(state, action):
    if not workbench_http_identity(state["port"]):
        return {"ok":False,"uncertain":False,"error":"workbench_identity_unconfirmed"}
    request = {"url":state["url"], "action":action, "role":state["role"]}
    for key in ("story_id","project_id","commission_id"):
        if state.get(key):
            request[key] = state[key]
    if action == "start":
        request["idea"] = state["idea"]
    if action in {"start","continue"}:
        request.update(run_id=state["id"],attempt=state["attempt"])
    if action=="continue" and state["role"]=="script" and state.get("observation",{}).get("reason")=="published_quantity_mismatch":
        request["repair_quantities"]=True
    argv = [shutil.which("node") or "node", str(ROOT / "tools/workbench_loop_browser.cjs")]
    event("workbench_loop.browser.request", {"argv":argv, "request":request}, kind="subprocess")
    try:
        result = subprocess.run(argv, cwd=ROOT, input=json.dumps(request), capture_output=True,
                                encoding="utf-8", errors="replace", timeout=240)
        reply = json.loads(result.stdout)
        if not isinstance(reply,dict) or (result.returncode and reply.get("ok")):
            raise ValueError("invalid browser receipt")
    except (OSError,ValueError,subprocess.TimeoutExpired) as exc:
        event("workbench_loop.browser.error", {"type":type(exc).__name__}, kind="error")
        return {"ok":False,"uncertain":action in {"start","continue"},"error":"browser_receipt_unavailable"}
    event("workbench_loop.browser.response", {"action":action,"receipt":reply}, kind="tool")
    return reply


def jobs():
    return read_json(WEB / "state.json")["jobs"]


def gate(state):
    path = "/api/projects/" + state["project_id"] + "/studio"
    event("workbench_loop.gate.request", {"method":"GET","path":path},kind="http")
    client = http.client.HTTPConnection("127.0.0.1", state["port"], timeout=120)
    try:
        client.request("GET",path)
        response = client.getresponse()
        content = response.read()
        event("workbench_loop.gate.response", {"status":response.status,"trace_id":response.getheader("X-Trace-Id")},kind="http")
        if response.status != 200:
            raise ValueError("交付门禁读取失败")
        return json.loads(content)["preview_gate"]
    finally:
        client.close()


@traced("workbench_loop.restart",kind="subprocess",capture_inputs=False)
def restart(state):
    def busy():
        return any(job["status"] in {"queued", "running"} for job in jobs())

    return restart_original(state, WEB, RUNS, busy, _start_server)


def submit(state):
    action = state["next_action"]
    record(state,"submitting",attempt=uuid.uuid4().hex,reason=action)
    reply = page(state,action)
    if reply.get("queued") and reply.get("message_id"):
        record(state,"waiting_dispatch",reason="message_queued",receipt=reply)
        return
    if not reply.get("ok"):
        if reply.get("fault") and not reply.get("uncertain"):
            record(state,"repair",fault=reply["fault"],reason="page_fault",after_restart="submit")
        else:
            record(state,"reconcile" if reply.get("uncertain") else "blocked",
                   reason=reply.get("error","browser_error"),receipt=reply)
        return
    accept_receipt(state,action,reply)


def accept_receipt(state,action,reply):
    job_id = reply.get("job_id","")
    if not ID.fullmatch(job_id or ""):
        record(state,"unknown",receipt=reply,reason="missing_job_receipt")
        return
    if action == "continue" and job_id in state["submitted"]:
        record(state,"blocked",reason="continue_returned_previous_job",receipt=reply)
        return
    scope = {}
    for key,pattern in (("story_id",ID),("project_id",PROJECT),("session_id",ID),("commission_id",ID)):
        value = reply.get(key)
        if value and pattern.fullmatch(value):
            if state.get(key) not in (None,value):
                record(state,"unknown",reason="scope_changed",receipt=reply)
                return
            scope[key] = value
    if action == "start":
        scope.setdefault("story_id",job_id)
    state["submitted"].append(job_id)
    record(state,"watch",job_id=job_id,receipt=reply,reason=action+"_accepted",**scope)


def reconcile(state):
    """Recover a lost HTTP reply by its persisted native UI identity, without another POST."""
    action=state["next_action"]
    path=RUNS/state["id"]/("intent-"+state.get("attempt","")+".json")
    intent=read_json(path) if path.is_file() else {}
    if intent.get("action")==action and intent.get("role")==state["role"]:
        if action=="start":
            key=intent.get("idempotency_key")
            matches=[j for j in jobs() if key and j.get("_idempotency_key")==key and j["kind"]=="studio_script"
                     and (j.get("story_input") or {}).get("idea")==state["idea"].strip()]
            if len(matches)==1:
                accept_receipt(state,action,{"job_id":matches[0]["id"],"reconciled":True})
                return
        elif ID.fullmatch(intent.get("message_id") or ""):
            db_path=ROOT/"outputs/pi/registry.sqlite3"
            with closing(sqlite3.connect(db_path.as_uri()+"?mode=ro",uri=True)) as db:
                row=db.execute("""SELECT m.job_id,s.id,s.source_job,s.project_id,m.continue_commission,m.status
                    FROM messages m JOIN sessions s ON s.id=m.session_id WHERE m.id=? AND s.role=? AND m.role='user'""",
                    (intent["message_id"],state["role"])).fetchone()
            if row and (not state.get("story_id") or row[2]==state["story_id"]) and (
                    not state.get("project_id") or row[3]==state["project_id"]) and (
                    state["role"]!="video" or row[4] and row[4]==intent.get("commission_id")):
                if row[5] in {"queued","dispatching"} and not any(j["id"]==row[0] for j in jobs()):
                    if state["phase"]!="waiting_dispatch":
                        record(state,"waiting_dispatch",reason="message_queued")
                    time.sleep(5)
                    return
                accept_receipt(state,action,{"job_id":row[0],"session_id":row[1],"commission_id":row[4],
                                              "message_id":intent["message_id"],"reconciled":True})
                return
    record(state,"unknown",reason="submission_receipt_unconfirmed")


def watch(state):
    from tools.workbench_loop_observer import acceptance_retake_stopped, linked_recovery_job, observe_job
    all_jobs=jobs()
    job = next((j for j in all_jobs if j["id"] == state["job_id"]),None)
    if job is None:
        record(state,"unknown",reason="job_receipt_missing_from_store")
        return
    if acceptance_retake_stopped(ROOT,job):
        record(state,"blocked",reason="acceptance_retake_limit")
        return
    if job["status"] not in {"running","queued","succeeded","cancelled"}:
        related=linked_recovery_job(ROOT,job,all_jobs,role=state["role"],
                                    session_id=state.get("session_id") or "",
                                    message_id=state.get("receipt",{}).get("message_id") or "")
        if related and related["id"]!=job["id"]:
            record(state,"watch",job_id=related["id"],reason="original_commission_recovery")
            return
    if job.get("project_id") and PROJECT.fullmatch(job["project_id"]):
        if state.get("project_id") and job["project_id"] != state["project_id"]:
            record(state,"unknown",reason="job_project_mismatch")
            return
        state["project_id"] = job["project_id"]
    if (job.get("kind") in {"studio_video","pi_video"} or state["role"]=="asset") and job["status"] in {"interrupted","failed"}:
        from tools.workbench_loop_dependency import recheck_dependency
        evidence=recheck_dependency(ROOT, job)
        if evidence:
            if evidence.get("status")=="repairable" and evidence.get("fault"):
                record(state,"repair",fault=evidence["fault"],observation=evidence,
                       reason=evidence["reason"],after_restart="recheck_dependency")
            elif evidence.get("status")=="cleared" and evidence.get("safe_to_continue") is True:
                cid=evidence.get("commission_id")
                checkpoint=evidence.get("continuation_checkpoint")
                if (state["role"]!="video" or not ID.fullmatch(cid or "") or state.get("commission_id") not in (None,cid)
                        or evidence.get("receipt_unknown") is not False or not checkpoint):
                    record(state,"blocked",observation=evidence,reason="commission_reconciliation_required")
                elif checkpoint==state.get("last_continuation_checkpoint"):
                    record(state,"blocked",observation=evidence,reason="candidate_unchanged_after_continuation")
                else:
                    record(state,"submit",next_action="continue",commission_id=cid,observation=evidence,
                           last_continuation_checkpoint=checkpoint,reason=evidence["reason"])
            else:
                record(state,"blocked",observation=evidence,reason=evidence["reason"])
            return
    if state["role"]=="asset":
        record(state,"blocked",reason="asset_receipt_requires_reconciliation")
        return
    preview = None
    if state["role"]=="video" and state.get("project_id") and job["status"]=="succeeded":
        preview=gate(state)
    obs=observe_job(ROOT,job,session_id=state.get("session_id",""),role=state["role"],
                    preview=preview,idea=state.get("idea"))
    status=obs["status"]
    if status=="running":
        listen_until_terminal(state)
        return
    if status=="succeeded":
        if state["role"]=="script":
            check=(job.get("result") or {}).get("completion_check") or {}
            asset=((check.get("publication") or {}).get("asset_job") or {}).get("id")
            batch=next((j for j in all_jobs if j["id"]==asset and j["kind"]=="asset_production"
                        and j.get("project_id")==state.get("project_id")),None)
            if batch:
                obs["asset_job"]={"id":asset,"status":batch["status"],
                                  "counts":(batch.get("result") or {}).get("counts")}
        if state.get("page_fault"):
            record(state,"repair",fault=state["page_fault"],page_fault=None,observation=obs,
                   reason="page_fault",after_restart="verify_page")
        else:
            record(state,"verify_page",observation=obs,reason="job_validated")
    elif status=="repairable":
        record(state,"repair",fault=obs["fault"],observation=obs,reason=obs["reason"],after_restart="continue")
    elif status in {"interrupted","transient"} and obs.get("safe_to_continue") is True:
        checkpoint=obs.get("candidate_sha256")
        if checkpoint and checkpoint==state.get("last_continuation_checkpoint"):
            record(state,"blocked",observation=obs,reason="candidate_unchanged_after_continuation")
        else:
            retries = 0 if checkpoint else state["retry_count"]
            delay=15 * 2**min(retries,5) if status=="transient" or retries>=3 else 0
            record(state,"submit",observation=obs,next_action="continue",retry_count=retries+1,
                   not_before=time.time()+delay,last_continuation_checkpoint=checkpoint,reason=obs["reason"])
    else:
        record(state,"unknown" if status=="unknown" else "blocked",observation=obs,reason=obs["reason"])


@traced("workbench_loop.listen",kind="tool",capture_inputs=False,capture_output=False)
def listen_until_terminal(state):
    request={k:state[k] for k in ("url","role","story_id","project_id") if state.get(k)}
    request["action"]="listen"
    argv=[shutil.which("node") or "node",str(ROOT/"tools/workbench_loop_browser.cjs"),"--listen"]
    event("workbench_loop.listen.request",{"argv":argv,"request":request},kind="subprocess")
    process=subprocess.Popen(argv,cwd=ROOT,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL,encoding="utf-8",errors="replace")
    messages=queue.Queue()
    def collect():
        for line in process.stdout:
            messages.put(line)
    def drain():
        while not messages.empty():
            message=json.loads(messages.get_nowait())
            event("workbench_loop.listen.response",message,kind="tool")
            if message.get("fault") and not state.get("page_fault"):
                record(state,"watch",page_fault=message["fault"],reason="page_exception_recorded")
    reader=threading.Thread(target=collect,daemon=True)
    reader.start()
    next_health_check=time.monotonic()+60
    try:
        process.stdin.write(json.dumps(request)+"\n")
        process.stdin.flush()
        while True:
            drain()
            current=next((j for j in jobs() if j["id"]==state["job_id"]),{})
            if current.get("status") not in {"queued","running"}:
                break
            if time.monotonic()>=next_health_check:
                next_health_check=time.monotonic()+60
                if restore_stopped_server(state):
                    break
            if process.poll() is not None and not state.get("page_fault"):
                if not restore_stopped_server(state):
                    time.sleep(5)
                break
            time.sleep(5)
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=90)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        reader.join()
        process.stdout.close()
        event("workbench_loop.listen.closed",{"exit":process.returncode},kind="subprocess")
    drain()


def restore_stopped_server(state):
    if _probe(state["port"])!="free":
        return False
    manifest=read_json(WEB/"server-process.json")
    if manifest["url"]!=state["url"]:
        raise RuntimeError("失去原工作台的端口归属")
    pid=int(manifest["pid"])
    if os.name=="nt":
        cmd=["powershell.exe","-NoProfile","-NonInteractive","-Command",
             f"if(Get-Process -Id {pid} -ErrorAction SilentlyContinue){{exit 0}}; exit 3"]
        event("workbench_loop.pid.request",{"argv":cmd},kind="subprocess")
        check=subprocess.run(cmd,capture_output=True,timeout=45)
        event("workbench_loop.pid.response",{"exit":check.returncode},kind="subprocess")
        if check.returncode!=3:
            return False
    else:
        try:
            os.kill(pid,0)
            return False
        except ProcessLookupError:
            pass
    _start_server(state["port"],state["url"])
    record(state,"watch",reason="server_restored_reconcile_original_job")
    return True


def legacy_retry_phase(state):
    """Reopen legacy retry cutoffs only at an observed, side-effect-free checkpoint."""
    if state.get("phase") != "blocked":
        return None
    history = state.get("history") or []
    if len(history) < 2 or history[-1].get("phase") != "blocked":
        return None
    reason = state.get("reason")
    if history[-1].get("reason") != reason:
        return None
    if reason == "same_fault_after_three_repairs" and history[-2].get("phase")=="repair":
        if state.get("fault") and json.dumps(state["fault"],sort_keys=True)==state.get("last_fault"):
            return "repair"
    obs = state.get("observation") or {}
    if (reason=="repeated_interruption" and history[-1].get("observation")==obs and obs.get("safe_to_continue") is True
            and obs.get("job_id")==state.get("job_id") and obs.get("status") in {"transient","interrupted"}):
        return "watch"
    return None


def run(state):
    while state["phase"] not in TERMINAL:
        phase=state["phase"]
        if phase=="submit":
            remaining=state.get("not_before",0)-time.time()
            if remaining>0:
                time.sleep(min(remaining,30))
                continue
            submit(state)
        elif phase=="watch":
            watch(state)
        elif phase=="repair":
            from tools.workbench_loop_repair import repair_fault
            if state.get("max_repairs") is not None and state["repairs"]>=state["max_repairs"]:
                record(state,"blocked",reason="repair_limit")
                continue
            remaining=state.get("repair_not_before",0)-time.time()
            if remaining>0:
                if state.get("watch_managed"):
                    return "repair"
                time.sleep(min(remaining,30))
                continue
            if state.get("retry_observe_job") and state.get("job_id"):
                state["retry_observe_job"]=False
                watch(state)
                if state["phase"]!="repair":
                    continue
            fingerprint=json.dumps(state["fault"],sort_keys=True)
            same_fault=fingerprint==state.get("last_fault")
            repeated=state.get("repeated_faults",0)+1 if same_fault else 1
            rejected=list(state.get("rejected_candidates",[])) if same_fault else []
            last_repair=state.get("repair") or {}
            if same_fault and last_repair.get("status")=="applied" and len(last_repair.get("files",[]))==1:
                rejected.append({"reason":"business_fault_after_verified_repair","file":last_repair["files"][0],
                                 "after_sha256":last_repair.get("after_sha256"),"checks":{"passed":last_repair.get("tests",[])}})
            record(state,"repairing",repairs=state["repairs"]+1,last_fault=fingerprint,repeated_faults=repeated,
                   rejected_candidates=rejected,repair_not_before=0)
            outcome=repair_fault(ROOT,state["fault"],previous=rejected)
            if outcome["status"]!="applied":
                reason=outcome["reason"]
                failed=outcome.get("validation") or {"reason":reason}
                # Changed evidence can advance immediately. Repeated rejections wait
                # before rereading the actual job and fresh source/test snapshots.
                seen=failed in rejected
                if not seen:
                    rejected=[*rejected,failed]
                delay=0 if outcome.get("validation") and not seen else 15 * 2**min(repeated-1,5)
                record(state,"repair",repair=outcome,reason=reason,rejected_candidates=rejected,
                       repair_not_before=time.time()+delay if delay else 0,
                       retry_observe_job=state.get("after_restart") in {"continue","recheck_dependency"})
                continue
            manifest=read_json(WEB/"server-process.json")
            record(state,"restart",old_pid=manifest["pid"],old_started_at=manifest.get("started_at"),
                   repair=outcome,reason="verified_repair")
        elif phase=="restart":
            if not restart(state):
                time.sleep(10)
                continue
            if state.get("after_restart") == "verify_page":
                record(state,"verify_page",reason="workbench_ready")
            elif state.get("after_restart") == "recheck_dependency":
                record(state,"watch",reason="recheck_original_prerequisite")
            else:
                action=state["next_action"] if state.get("after_restart")=="submit" else "continue"
                if action=="continue" and state.get("job_id"):
                    from tools.workbench_loop_observer import linked_recovery_job
                    current=jobs()
                    old=next((j for j in current if j["id"]==state["job_id"]),None)
                    if old:
                        linked=linked_recovery_job(ROOT,old,current,role=state["role"],
                            session_id=state.get("session_id") or "",
                            message_id=state.get("receipt",{}).get("message_id") or "")
                        if linked or old["status"] in {"queued","running","succeeded"}:
                            record(state,"watch",job_id=(linked or old)["id"],reason="server_recovered_job")
                            continue
                record(state,"submit",next_action=action,reason="workbench_ready")
        elif phase=="verify_page":
            reply=page(state,"observe")
            if reply.get("fault"):
                record(state,"repair",fault=reply["fault"],reason="page_fault",after_restart="verify_page")
                continue
            if reply.get("ok") and state["role"]=="script" and state.get("through_video"):
                project=reply.get("project_id") or state.get("project_id")
                if not PROJECT.fullmatch(project or ""):
                    record(state,"blocked",reason="published_project_missing")
                    continue
                record(state,"submit",role="video",project_id=project,session_id=None,commission_id=None,
                       next_action="continue",script_page=reply,reason="script_verified")
                continue
            record(state,"succeeded" if reply.get("ok") else "blocked",final_page=reply,
                   reason="page_verified" if reply.get("ok") else "page_verification_failed")
        elif phase in {"submitting","reconcile","waiting_dispatch"}:
            reconcile(state)
        elif phase=="repairing":
            record(state,"unknown",reason=phase+"_receipt_lost")
        else:
            raise ValueError("无法识别的旁路检查点")
    return state["phase"]


@traced("cli.workbench_loop",kind="cli",capture_inputs=False,capture_output=False)
def main():
    from pianchang.model_routes import load_routes
    for credential in load_routes(ROOT/".env").secrets():
        register_secret(credential)
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest="command",required=True)
    start=commands.add_parser("start",help="在网页提交新创意或继续已有会话")
    source=start.add_mutually_exclusive_group(required=True)
    source.add_argument("--idea")
    source.add_argument("--story-id")
    source.add_argument("--project-id")
    source.add_argument("--job-id",help="接管网页已提交的任务，不再点击提交")
    start.add_argument("--run-id",help="常驻监听器分配给同一故障事件的稳定运行 ID")
    start.add_argument("--role",choices=("script","video"),default="script")
    start.add_argument("--through-video",action="store_true",help="剧本通过后，在同一工程继续已确认的视频委托")
    start.add_argument("--max-repairs",type=int,help="可选的代码修复总次数上限")
    resume=commands.add_parser("resume",help="恢复这个旁路检查点")
    resume.add_argument("run_id")
    args=parser.parse_args()
    with exclusive_driver():
        if args.command=="start" and args.run_id:
            if not ID.fullmatch(args.run_id) or not args.job_id or not ID.fullmatch(args.job_id):
                parser.error("--run-id 只用于接管有回执的任务")
            receipt=RUNS/args.run_id/"run.json"
            if receipt.exists():
                state=read_json(receipt)
                if state.get("origin_job_id")!=args.job_id:
                    parser.error("旁路运行 ID 已绑定其他任务")
                return 0 if run(state)=="succeeded" else 2
        if args.command=="resume":
            from tools.workbench_loop_watch import passive_observation
            if not ID.fullmatch(args.run_id): parser.error("run_id 格式无效")
            state=read_json(RUNS/args.run_id/"run.json")
            # Older watcher-owned drivers predate the managed flag. Release the
            # global driver lock during a delayed retry only for a confirmed owner.
            if "watch_managed" not in state and state.get("origin_job_id"):
                index=RUNS/"watch"/"state.json"
                if index.is_file():
                    watch_state=read_json(index)
                    row=watch_state.get("events",{}).get(watch_state.get("latest",{}).get(state["origin_job_id"]),{})
                    state["watch_managed"]=row.get("run_id")==args.run_id and row.get("job_id")==state["origin_job_id"]
            if state["phase"]=="succeeded":
                record(state,"watch",reason="revalidate_saved_delivery")
            elif state["phase"]=="unknown" and state.get("reason")=="submission_receipt_unconfirmed":
                record(state,"reconcile",reason="recheck_original_receipt")
            elif resume_phase:=legacy_retry_phase(state):
                record(state,resume_phase,reason="recheck_previous_retry_cutoff")
            elif passive_observation(state):
                record(state,"watch",reason="recheck_job_evidence")
            elif state["phase"]=="blocked" and state.get("reason")=="authorization_or_resumable_commission_required":
                record(state,"submit",next_action="continue",reason="check_original_commission")
            elif state["phase"]=="blocked" and state.get("reason")=="page_verification_failed":
                record(state,"verify_page",reason="recheck_page")
            elif state["phase"]=="blocked" and state.get("reason")=="workbench_identity_unconfirmed":
                record(state,"submit",reason="recheck_workbench")
        else:
            if (args.idea is not None and not 1<=len(args.idea.strip())<=6000 or
                    args.story_id and not ID.fullmatch(args.story_id) or
                    args.project_id and not PROJECT.fullmatch(args.project_id) or
                    args.job_id and not ID.fullmatch(args.job_id) or
                    args.role=="video" and not (args.project_id or args.job_id) or
                    args.max_repairs is not None and args.max_repairs<0):
                parser.error("创意或范围无效；视频须指定工程")
            manifest=read_json(WEB/"server-process.json")
            match=re.fullmatch(r"http://127\.0\.0\.1:(\d+)",manifest["url"])
            if not match or not workbench_http_identity(int(match[1])):
                raise RuntimeError("请先启动本项目工作台")
            state={"id":args.run_id or uuid.uuid4().hex,"origin_job_id":args.job_id,"role":args.role,"idea":args.idea,"story_id":args.story_id,
                   "project_id":args.project_id,"port":int(match[1]),"url":manifest["url"],
                   "repairs":0,"retry_count":0,"max_repairs":args.max_repairs,"history":[],"submitted":[],
                   "through_video":args.through_video,"watch_managed":bool(args.run_id),
                   "next_action":"start" if args.idea else "continue"}
            if args.job_id:
                job=next((j for j in jobs() if j["id"]==args.job_id),None)
                from tools.workbench_loop_watch import ASSETS
                if not job or job["kind"] not in {"pi_script","studio_script","pi_video","studio_video"} | ASSETS:
                    parser.error("请选择已有的剧本或制作 Agent 任务")
                role="asset" if job["kind"] in ASSETS else job["kind"].split("_")[1]
                story=job["id"] if job["kind"]=="studio_script" else job.get("story_job_id")
                if not story and not job.get("project_id"):
                    parser.error("原任务缺少故事或工程范围")
                record(state,"watch",role=role,job_id=job["id"],project_id=job.get("project_id"),
                       story_id=story,session_id=job.get("session_id"),
                       commission_id=job.get("message_id") if job["kind"]=="studio_video" else job.get("commission_id"),
                       submitted=[job["id"]],reason="attached_existing_job")
            else:
                record(state,"submit",reason="created")
        print("run_id="+state["id"],flush=True)
        return 0 if run(state)=="succeeded" else 2


if __name__=="__main__":
    parent=os.environ.pop("PIANCHANG_LOOP_PARENT_CONTEXT", "")
    with use_context(json.loads(parent) if parent else None,tracer=get_tracer(ROOT/"outputs/traces")),trace("workbench_loop.run"):
        exit_code=main()
    raise SystemExit(exit_code)
