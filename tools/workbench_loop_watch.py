"""Persistent local observer for failures originating in ordinary workbench pages."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import uuid

from pianchang.app_service import read_json, write_json
from tools.workbench_loop_observer import acceptance_retake_stopped, observe_job
from trace_system import TraceError, current_context, event, get_tracer, register_secret, trace, traced, use_context

ROOT = Path(__file__).resolve().parents[1]
ID = re.compile(r"[a-f0-9]{32}")
AGENTS = {"pi_video", "pi_script", "studio_video", "studio_script"}
ASSETS = {"asset_production", "asset_regenerate", "asset_inspect", "asset_approve", "studio_reference"}
FAILED = {"failed", "interrupted"}
TERMINAL = {"succeeded", "blocked", "unknown"}


def passive_observation(state):
    """Return the stopped read of this exact job, excluding submissions, repair attempts and operator decisions."""
    prior = state.get("observation") or {}
    reason = state.get("reason")
    return (state.get("phase") in {"unknown","blocked"} and ID.fullmatch(str(state.get("job_id")))
            and prior.get("job_id") == state["job_id"] and prior.get("reason") == reason
            and prior.get("status") in {"unknown","interrupted","transient","needs_input"}
            and reason in {"provider_receipt_unknown","pending_operation","observation_evidence_unavailable",
                           "missing_task_evidence","provider_connection_outcome_unknown","planner_request_not_sent",
                           "incomplete_execution_without_diagnosis","publication_unconfirmed","provider_rate_limited"})


def _actionable(observation):
    return (observation.get("status") == "repairable" and bool(observation.get("fault"))
            or observation.get("status") in {"interrupted","transient"} and observation.get("safe_to_continue") is True)


def _legacy_retry(state, job):
    if (state.get("reason") not in {"same_fault_after_three_repairs","repeated_interruption"}
            or job.get("cancel_requested") or job.get("trace_incomplete") or state.get("job_id") != job.get("id")
            or state.get("project_id") != job.get("project_id") or job["id"] not in state.get("submitted",[])):
        return False
    observation = state.get("observation") or {}
    if (observation.get("job_id") != job["id"] or not observation.get("trace_id") or
            observation["trace_id"] not in {job.get("worker_trace_id"),job.get("trace_id")}):
        return False
    from tools.workbench_loop import legacy_retry_phase
    return bool(legacy_retry_phase(state))


def _folder(root):
    return Path(root) / "outputs/workbench-loop/watch"


def _signature(job):
    # Changing the UI progress text or refreshing the identical job cannot trigger another repair.
    result = job.get("result") or {}
    data = [job["id"], job["status"], job.get("trace_id"), job.get("worker_trace_id"),
            job.get("error"), result.get("reason"), result.get("summary"), result.get("asset_diagnostics")]
    if result.get("reason") == "script_creative_input":
        data.append((result.get("completion_check") or {}).get("creative_input"))
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _partial_result(job):
    outcome = job.get("result") or {}
    return (job.get("kind") in {"studio_video","pi_video"} and job.get("status")=="succeeded"
            and outcome.get("action_required") is True and (outcome.get("task_complete") is False
            or bool((outcome.get("extra_retake") or {}).get("partial"))))


def _needs_attention(job):
    return job.get("status") in FAILED or _partial_result(job)


def record_failure_transition(state, job, *, transition=None):
    """Called under the AppService lock, before persisting the job and this outbox together."""
    if job.get("kind") not in AGENTS | ASSETS or not _needs_attention(job):
        return
    key = _signature(job)
    source = {}
    if transition and all(ID.fullmatch(str(transition.get(k))) for k in ("event_id","trace_id")):
        source = {"event_id":transition["event_id"],"task_trace_id":transition["trace_id"]}
    state.setdefault("feedback_failures", {}).setdefault(key, {
        "job_id":job["id"], "kind":job["kind"], "status":job["status"],
        "trace_id":job.get("worker_trace_id") or job.get("trace_id"), "fingerprint":key, **source,
        **({"partial_result":True} if _partial_result(job) else {}),
    })


@contextmanager
def _lock(path, *, create=True):
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        file = path.open("a+b" if create else "r+b")
    except FileNotFoundError:
        yield True
        return
    with file:
        try:
            if os.name == "nt":
                import msvcrt
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True


def _jobs(root):
    path = root / "outputs/web/state.json"
    rows = read_json(path)["jobs"] if path.is_file() else []
    return {row["id"]:row for row in rows}


def _load(root):
    path = _folder(root) / "state.json"
    if path.is_file():
        return read_json(path)
    # Only the FIRST installation sets a baseline; a restart catches any failures during downtime.
    web_state=root/"outputs/web/state.json"
    index=read_json(web_state) if web_state.is_file() else {}
    baseline = {j["id"]:_signature(j) for j in index.get("jobs",[])
                if j["kind"] in AGENTS | ASSETS and _needs_attention(j)}
    # Stamped failures were explicitly delivered by the app, even if the watcher started late.
    stamps = index.get("feedback_failures", {})
    state = {"version":2, "baseline":{jid:key for jid,key in baseline.items() if key not in stamps},
             "events":{}, "latest":{}}
    write_json(path,state)
    return state


def watcher_alive(root):
    """Cheap process ownership check for the service watchdog, without any Trace writes."""
    with _lock(_folder(root)/"watch.lock",create=False) as free:
        return not free


def watcher_paused(root):
    return (_folder(root)/"trace-paused.json").exists()


def mark_trace_pause(root, failure=None):
    # Independent fail-stop marker: the broken Trace backend must not be called again.
    folder=_folder(root)
    folder.mkdir(parents=True,exist_ok=True)
    try:
        with (folder/"trace-paused.json").open("x",encoding="utf8") as file:
            marker = {"reason":"trace_incomplete","at":time.time()}
            context = current_context()
            for name in ("unwritten_span_end", "uncommitted_read_batch"):
                ref = getattr(failure, name, None)
                if ref and context and context["trace_id"] == ref.get("trace_id"):
                    marker.update({name:ref, "watcher_pid":os.getpid()})
                    break
            json.dump(marker,file)
            file.flush()
            os.fsync(file.fileno())
    except FileExistsError:
        pass


def _include(root, job_id):
    if not ID.fullmatch(job_id):
        raise ValueError("无效的任务 ID")
    job = _jobs(root).get(job_id)
    if not job or job.get("kind") not in AGENTS | ASSETS or not _needs_attention(job):
        raise ValueError("请选择工作台已失败或中断的 Agent 或资产任务")
    write_json(_folder(root) / "include" / (job_id+".json"), {"job_id":job_id})


def watch_status(root, job_id):
    """Small read-only public view. Never exposes prompts, filesystem paths or raw exceptions."""
    folder = _folder(root)
    if watcher_paused(root):
        return {"running":False,"status":"blocked","reason":"trace_incomplete","job_id":job_id}
    alive = watcher_alive(root)
    statefile = folder / "state.json"
    if not ID.fullmatch(str(job_id)) or not statefile.is_file():
        return {"running":alive, "status":"untracked"}
    state = read_json(statefile)
    key = state["latest"].get(job_id)
    record = state["events"].get(key, {})
    result = {k:record[k] for k in ("job_id","kind","status","reason","run_id","triage","dependency","updated_at","progress_message","driver_phase",
                                   "voice_recovery_job_id","voice_recovery_error","voice_inspection_job_id","voice_inspection_error")
              if k in record}
    if record.get("status") == "needs_input" and record.get("human_request"):
        result["human_request"] = record["human_request"]
    if record.get("status") == "awaiting_evidence" and record.get("reason")=="inspect_commission_reports" and record.get("review_link"):
        result["review_link"] = record["review_link"]
    flow = record.get("image_flow") or {}
    if flow.get("child_id"):
        result["image_recovery_job_id"] = flow["child_id"]
    result["running"] = alive
    result.setdefault("status","historical" if job_id in state["baseline"] else "untracked")
    return result


class Watcher:
    def __init__(self, root):
        self.root = Path(root)
        self.folder = _folder(root)
        self.runs = self.folder.parent
        self.state = _load(self.root)
        self.child = None
        self.triage_future = None
        self.triage_key = None
        self.pool = ThreadPoolExecutor(max_workers=1,thread_name_prefix="codex-triage")
        self.next_check = {}
        self.retry_inspection = {}
        self.state_stamp = None
        self.jobs = {}

    def close(self):
        self.pool.shutdown(wait=True)

    def save(self):
        write_json(self.folder / "state.json", self.state)

    def update(self, record, **fields):
        if "status" in fields and fields["status"] != "needs_input" and record.get("human_request"):
            fields.setdefault("human_request", None)
        if "status" in fields and fields["status"] != record.get("status") and record.get("progress_message"):
            fields.setdefault("progress_message", None)
        if all(record.get(k)==v for k,v in fields.items()):
            return
        event("workbench.watch.transition",{"job_id":record["job_id"],**fields},kind="state")
        record.update(fields, updated_at=time.time())
        self.save()

    def register(self, job, *, include=False):
        jid=job["id"]
        if not ID.fullmatch(jid) or not _needs_attention(job) or job["kind"] not in AGENTS | ASSETS:
            return
        key=_signature(job)
        if self.state["baseline"].get(jid)==key and not include:
            return
        self._register(key,jid,job["kind"],job["status"],job.get("worker_trace_id") or job.get("trace_id"))

    def _register(self,key,jid,kind,status,trace_id,*,source=None,latest=True):
        if key in self.state["events"]:
            return
        record={"job_id":jid, "kind":kind, "trace_id":trace_id, "original_status":status,
                "status":"queued", "reason":"page_job_failed", "updated_at":time.time()}
        if source:
            record.update({k:source[k] for k in ("event_id","task_trace_id") if ID.fullmatch(str(source.get(k)))})
        if latest:
            self.state["latest"][jid]=key
        self.state["events"][key]=record
        event("workbench.watch.failure",{"fingerprint":key,**record},kind="state")
        self.save()

    def register_stamps(self,index):
        stamps = index.get("feedback_failures",{})
        previous = self.state.get("historical_stamps",{})
        # Dict insertion order is the app's persisted outbox delivery order.
        visited = set()
        for key,stamp in stamps.items():
            visited.add(key)
            if key in self.state["events"]:
                continue
            if (stamp.get("fingerprint")!=key or not re.fullmatch(r"[a-f0-9]{64}",key)
                    or not ID.fullmatch(str(stamp.get("job_id"))) or not (stamp.get("status") in FAILED
                        or stamp.get("status")=="succeeded" and stamp.get("partial_result") is True and stamp.get("kind") in {"pi_video","studio_video"})
                    or stamp.get("kind") not in AGENTS | ASSETS):
                continue
            jid = stamp["job_id"]
            latest = self.state["latest"].get(jid)
            advance = key not in previous or not latest or latest in visited
            self._register(key,jid,stamp["kind"],stamp["status"],stamp.get("trace_id"),source=stamp,latest=advance)
        if "historical_stamps" in self.state:
            self.state["baseline"] = {jid:key for jid,key in self.state["baseline"].items() if key not in previous}
            del self.state["historical_stamps"]
            self.state["version"] = 2
            self.save()

    def owned(self, job, key):
        jid=job["id"]
        own_id=uuid.uuid5(uuid.NAMESPACE_URL,"pianchang/workbench_watch/"+key).hex
        own_path=self.runs/own_id/"run.json"
        if own_path.is_file():
            return read_json(own_path)
        matches=[]
        failure_time=datetime.fromisoformat(job["updated_at"]).timestamp() if job.get("updated_at") else 0
        for path in self.runs.glob("*/run.json"):
            if not ID.fullmatch(path.parent.name):
                continue
            state=read_json(path)
            if jid!=state.get("job_id") and jid not in state.get("submitted",[]):
                continue
            if state["phase"] not in TERMINAL:
                return state
            if path.stat().st_mtime >= failure_time:
                matches.append((path.stat().st_mtime,state))
        return max(matches,key=lambda item:item[0])[1] if matches else None

    def _sync_run(self, record, state):
        phase=state["phase"]
        progress = {"driver_phase":phase}
        messages = {"repair":"已定位本地故障，正在准备修复。",
                    "repairing":"Codex 正在提出修复，候选改动须通过隔离验证。",
                    "restart":"修复已验证，正在等待工作台空闲并重启。",
                    "submit":"正在核对原任务与剩余额度，准备接续业务。",
                    "submitting":"已记录原任务的提交意图，正在获取回执。",
                    "reconcile":"正在核对原提交回执。","watch":"正在核对原业务执行结果。",
                    "verify_page":"正在工作台复验原业务结果。"}
        if phase == "repair" and state.get("repair_not_before",0) > time.time():
            progress["progress_message"]="已记录本轮修复的验证结果，等待下一次修复。"
        elif phase in messages:
            progress["progress_message"]=messages[phase]
        elif passive_observation(state):
            progress["progress_message"]="正在持续核对原任务的执行证据，回执就绪后自动接续。"
        if (phase=="succeeded" and state.get("role")=="video"
                and (state.get("observation") or {}).get("reason")=="delivery_gate_passed"):
            progress["progress_message"]="已完成原制作委托，当前交付验收和工作台页面均已复验。"
        self.update(record,run_id=state["id"],status=phase if phase in TERMINAL else "handling",
                    reason=state.get("reason") or phase, **progress)

    def _schedule_triage(self, key, record, job, evidence):
        # Claim before crossing the Codex boundary. Never repeat an unconfirmed invocation.
        digest=None
        if evidence.get("transport_failure") or "provider_calls" in evidence:
            connectivity=evidence.get("connectivity") or {}
            digest=hashlib.sha256(json.dumps([evidence.get("status"),evidence.get("reason"),evidence.get("missing_count"),
                evidence.get("receipt_unknown"),evidence.get("transport_failure"),evidence.get("provider_calls"),evidence.get("voice_review"),
                {k:v for k,v in connectivity.items() if k!="trace_id"}],sort_keys=True).encode()).hexdigest()
        elif evidence.get("reason") and evidence["reason"] != "original_job_changed":
            binding = {name:evidence[name] for name in ("status","reason","trace_id","fault","safe_to_continue",
                       "receipt_unknown","related_job_id","source_revision","spec_revision","continuation_checkpoint")
                       if name in evidence}
            digest = hashlib.sha256(json.dumps(binding,sort_keys=True).encode()).hexdigest()
        path=self.folder/"triage"/(key+("-"+digest if digest else "")+".json")
        previous=record.get("triage",{})
        if previous:
            prior=record.get("triage_evidence")
            if previous.get("status")=="running":
                if key!=self.triage_key:
                    receipt=self.folder/"triage"/(key+("-"+prior if prior else "")+".json")
                    if receipt.is_file():
                        outcome=read_json(receipt)
                    else:
                        outcome={"status":"unknown","reason":"triage_receipt_unconfirmed"}
                        write_json(receipt,outcome)
                    self.update(record,triage=outcome)
                return key==self.triage_key and prior==digest
            if digest is None or prior==digest:
                return True
        if self.triage_future is not None:
            return
        if path.is_file():
            self.update(record,triage=read_json(path),triage_evidence=digest)
            return True
        self.update(record,triage={"status":"running"},triage_evidence=digest)
        context=current_context()
        tracer=get_tracer()
        def invoke():
            from tools.workbench_loop_triage import triage_failure
            links=[{**context,"relation":"observed_by"}] if context else []
            if job.get("_trace_context"):
                links.append({**job["_trace_context"],"relation":"failed_task"})
            with use_context(None,tracer=tracer), tracer.trace("workbench.watch.diagnose",
                    inputs={"job_id":job["id"],"source_trace_id":job.get("trace_id"),"fingerprint":key},links=links):
                outcome=triage_failure(self.root,job,evidence)
                write_json(path,outcome)
                return outcome
        self.triage_key=key
        self.triage_future=self.pool.submit(invoke)
        return True

    def _completed_triage(self):
        if self.triage_future is None or not self.triage_future.done():
            return
        try:
            outcome=self.triage_future.result()
        except TraceError:
            raise
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            outcome={"status":"unavailable","reason":type(exc).__name__}
        self.update(self.state["events"][self.triage_key],triage=outcome)
        # A diagnostic receipt wakes the original evidence check. Its text cannot authorize a submission.
        self.next_check.pop(self.triage_key,None)
        self.next_check.pop(("agent_evidence",self.triage_key),None)
        self.triage_key=None
        self.triage_future=None

    def dependency(self, key, record, job, all_jobs):
        from tools.workbench_loop_dependency import recheck_dependency
        if self.next_check.get(key,0)>time.monotonic():
            return record.get("dependency")
        evidence=recheck_dependency(self.root,job)
        self.next_check[key]=time.monotonic()+60
        if evidence:
            failure=evidence.get("transport_failure") or {}
            if failure.get("request_kind")=="audio" and failure.get("error_type") in {"ConnectError","ConnectTimeout","ProxyError"}:
                from tools.workbench_loop_connectivity import check_voice_route
                evidence["connectivity"]=check_voice_route(self.root,failure)
            related=all_jobs.get(evidence.get("related_job_id"))
            if related and related.get("project_id")==job.get("project_id") and related["kind"] in ASSETS:
                self.register(related,include=True)
            self.update(record,dependency=evidence)
        return evidence

    def dispatch(self, key, record, *, identity=None, resume_id=None, previous_observation=None, legacy_job=None):
        run_id=resume_id or uuid.uuid5(uuid.NAMESPACE_URL,"pianchang/workbench_watch/"+(identity or key)).hex
        if self.child is not None:
            return
        with _lock(self.runs/"driver.lock",create=False) as free:
            if not free:
                return
        # If the owner changed while observing, the CLI checks the exclusive driver lock again.
        path=self.runs/run_id/"run.json"
        if path.is_file():
            state=read_json(path)
            if state.get("phase")=="repair" and state.get("repair_not_before",0)>time.time():
                self._sync_run(record,state)
                return
            if state.get("phase") in TERMINAL:
                passive = (previous_observation and passive_observation(state)
                           and state["observation"] == previous_observation)
                legacy = legacy_job and _legacy_retry(state,legacy_job)
                if not resume_id or state.get("job_id") != record["job_id"] or not (passive or legacy):
                    self._sync_run(record,state)
                    return
        args=["resume",resume_id] if resume_id else ["start","--job-id",record["job_id"],"--run-id",run_id]
        argv=[sys.executable,"-u","-m","tools.workbench_loop",*args]
        self.update(record,status="handling",reason="automatic_driver",run_id=run_id,
                    progress_message="已取得新的执行证据，正在用原运行记录接续处理。" if previous_observation else None)
        event("workbench.watch.driver.request",{"argv":argv,"cwd":str(self.root)},kind="subprocess")
        with (self.folder/"driver.log").open("ab") as log:
            self.child=subprocess.Popen(argv,cwd=self.root,stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                                        env={**os.environ,"PIANCHANG_LOOP_PARENT_CONTEXT":json.dumps(current_context())},
                                        creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        self.child_key=key
        event("workbench.watch.driver.started",{"job_id":record["job_id"],"pid":self.child.pid},kind="subprocess")

    def advance_script_input(self, record, job):
        result = job.get("result") or {}
        if (job["kind"] not in {"studio_script","pi_script"} or job["status"] != "interrupted"
                or result.get("backend") != "pi" or result.get("status") != "needs_input"
                or result.get("reason") != "script_creative_input"):
            return False
        from tools.workbench_loop_script import script_question, submit_script_reply
        if record.get("script_receipt") or record.get("reason") == "script_input_superseded":
            return True
        if job.get("cancel_requested") or job.get("trace_incomplete"):
            self.update(record, status="blocked", reason="original_order_stopped")
            return True
        request = record.get("human_request") or record.get("script_request")
        if request:
            answer_file = self.folder / "answers" / (request["id"] + ".json")
            reply = read_json(answer_file) if answer_file.is_file() else {}
            if (reply.get("job_id") == job["id"] and reply.get("request_id") == request["id"]
                    and reply.get("request") == request and reply.get("answers", {}).get("answer")):
                if record.get("script_retry_after", 0) > time.time():
                    return True
                self.update(record, status="handling", reason="script_input_submitting", script_request=request,
                            progress_message="已收到补充资料，正在交回原剧本会话。", script_retry_after=time.time()+30)
                receipt = submit_script_reply(self.root, job, request, reply["answers"]["answer"])
                if receipt.get("status") == "superseded":
                    self.update(record, status="resolved", reason="script_input_superseded",
                                progress_message="原剧本会话已更新，此问题已关闭。")
                else:
                    if not ID.fullmatch(str(receipt.get("job_id"))) or not ID.fullmatch(str(receipt.get("message_id"))):
                        raise ValueError("缺少原剧本会话的续写回执。")
                    self.update(record, status="succeeded", reason="script_input_delivered", script_receipt=receipt,
                                progress_message="补充资料已送达原剧本会话，续写任务已登记。")
                return True
        question_key = ("script_creative_input", job["id"])
        if request and self.next_check.get(question_key, 0) > time.monotonic():
            return True
        current = script_question(self.root, job)
        if current:
            self.next_check[question_key] = time.monotonic()+30
            self.update(record, status="needs_input", reason="script_creative_input", human_request=current)
        elif not request:
            return False
        else:
            self.update(record, status="resolved", reason="script_input_superseded")
        return True

    def process(self,key,record,job,all_jobs):
        from tools.workbench_loop_input import partial_video_execution, partial_video_question, video_question
        from tools.workbench_loop_images import advance_images
        if record["status"]=="succeeded":
            return
        if self.advance_script_input(record, job):
            return
        retake_stopped = acceptance_retake_stopped(self.root,job)
        if job["kind"] in {"studio_video","pi_video"}:
            bound = partial_video_execution(job, all_jobs, stopped=retake_stopped)
            if bound:
                original, latest = bound
                review = next((r for r in reversed(all_jobs.values()) if r.get("kind")=="studio_review"
                               and r.get("project_id")==original["project_id"] and r.get("review_request_id")==original["message_id"]), None)
                if review and review["status"] in {"queued","running"}:
                    message = "本次委托的自动复验已排队。" if review["status"]=="queued" else "正在自动复验本次委托的镜头与验收报告。"
                    self.update(record,status="handling",reason="commission_review_active",progress_message=message,
                                partial_question=None,review_link=None,human_request=None)
                    return
                check = ("partial_video",key,latest["id"],latest.get("updated_at"),(review or {}).get("id"),(review or {}).get("updated_at"))
                if self.next_check.get(check,0) <= time.monotonic():
                    try:
                        question = partial_video_question(self.root,original,latest,all_jobs)
                    except TraceError:
                        raise
                    except (OSError, KeyError, ValueError, sqlite3.Error):
                        if not retake_stopped:
                            raise
                        question = None
                    self.update(record,partial_question=question)
                    self.next_check[check] = time.monotonic()+60
                else:
                    question = record.get("partial_question")
                if question:
                    if original["id"] != job["id"]:
                        self.register(original,include=True)
                    read_only = question["kind"]=="inspect_commission_reports"
                    self.update(record,status="awaiting_evidence" if read_only else "needs_input",reason=question["kind"],dependency=None,
                                progress_message=question["message"],
                                review_link=question if read_only and original["id"]==job["id"] else None,
                                human_request=question if not read_only and original["id"]==job["id"] else None)
                    return
        if job["kind"] in {"studio_video","pi_video"} and ID.fullmatch(str(record.get("run_id"))):
            path=self.runs/record["run_id"]/"run.json"
            if path.is_file():
                driver=read_json(path)
                if (driver.get("id")==record["run_id"] and driver.get("origin_job_id")==job["id"]
                        and driver.get("project_id")==job.get("project_id")
                        and (driver["phase"] not in TERMINAL or driver["phase"]=="succeeded")):
                    self._sync_run(record,driver)
                    if driver["phase"] not in TERMINAL:
                        self.dispatch(key,record,resume_id=driver["id"])
                    return
        if record.get("image_flow") and advance_images(self,record,job,all_jobs,record.get("dependency")):
            return
        if retake_stopped:
            self.update(record,status="awaiting_evidence",reason="acceptance_retake_limit",human_request=None,review_link=None,
                        progress_message="自动重拍已停止。旁路会继续核对原委托的镜头与报告。")
            return
        dependency=self.dependency(key,record,job,all_jobs)
        if advance_images(self, record, job, all_jobs, dependency):
            return
        if dependency:
            if dependency.get("reason") == "current_plan_requires_confirmation" and not dependency.get("receipt_unknown"):
                request = video_question(job, dependency["reason"], dependency.get("spec_revision"), all_jobs)
                if request:
                    request["spec_revision"] = dependency.get("spec_revision")
                    self.update(record, status="needs_input", reason=dependency["reason"], human_request=request)
                    return
            repairable=dependency["status"]=="repairable" and bool(dependency.get("fault"))
            commission=job.get("commission_id") or job.get("message_id")
            resumable=(dependency["status"]=="cleared" and dependency.get("safe_to_continue") is True
                       and ID.fullmatch(str(commission)) and not dependency.get("receipt_unknown"))
            if repairable or resumable:
                if resumable and (dependency.get("transport_failure") or {}).get("request_kind")=="model":
                    self._schedule_triage(key,record,job,dependency)
                existing=self.owned(job,key)
                if existing and _legacy_retry(existing,job):
                    self.dispatch(key,record,resume_id=existing["id"],legacy_job=job)
                    return
                if repairable and existing and existing.get("reason")=="repair_limit":
                    self._sync_run(record,existing)
                    return
                # A passive observation can end as unknown before its wire evidence is reconciled.
                # A lost submit/repair receipt remains with that driver and cannot start another action.
                prior=(existing or {}).get("observation") or {}
                reconciled_observation=(resumable and existing and existing["phase"]=="unknown"
                    and existing.get("job_id")==job["id"] and prior.get("job_id")==job["id"]
                    and existing.get("reason")==prior.get("reason")=="provider_receipt_unknown"
                    and prior.get("trace_id")==job.get("worker_trace_id")
                    and dependency.get("transport_failure",{}).get("dispatch")=="not_sent")
                if existing and existing["phase"]!="blocked" and not reconciled_observation:
                    self._sync_run(record,existing)
                    if existing["phase"] not in TERMINAL:
                        self.dispatch(key,record,resume_id=existing["id"])
                    elif existing["phase"]=="unknown":
                        self._schedule_triage(key,record,job,dependency)
                    return
                # A new immutable revision or a reconciled original receipt opens a new decision.
                # Re-reading the same closed decision can never create a second dispatch.
                binding={k:dependency.get(k) for k in ("status","fault","source_revision","spec_revision",
                                                       "related_job_id","commission_id","continuation_checkpoint")}
                identity=key+hashlib.sha256(json.dumps(binding,sort_keys=True).encode()).hexdigest()
                if record.get("driver_evidence")!=identity:
                    self.update(record,status="queued",driver_evidence=identity,reason="dependency_revalidated")
                self.dispatch(key,record,identity=identity)
                return
            self.update(record,status=dependency["status"],reason=dependency["reason"])
            if (dependency["status"] == "needs_input" and dependency["reason"] == "reference_image_model_access"
                    and not dependency["receipt_unknown"]):
                # The provider supplied the exact rejection. Recheck the local gate on later ticks.
                if record.get("triage", {}).get("status") == "running":
                    # Reconcile the existing diagnostic receipt if a previous watcher was interrupted.
                    self._schedule_triage(key, record, job, dependency)
                return
            diagnostic_queued=self._schedule_triage(key,record,job,dependency)
            probe=dependency.get("connectivity") or {}
            if (diagnostic_queued and job["kind"] in {"asset_production","asset_regenerate"} and not job.get("_voice_recovery_from")
                    and dependency["reason"]=="voice_request_not_sent" and not dependency.get("receipt_unknown",True)
                    and probe.get("status")=="http_response" and probe.get("http_status") in {200,401,403,405}
                    and not record.get("voice_recovery_job_id") and record.get("voice_recovery_after",0)<=time.time()):
                from tools.workbench_loop_voice import request_voice_recovery
                self.update(record,voice_recovery_after=time.time()+300)
                try:
                    jid=request_voice_recovery(self.root,job)
                    if not ID.fullmatch(str(jid)):
                        raise ValueError("invalid voice recovery job receipt")
                except TraceError:
                    raise
                except Exception as exc:
                    self.update(record,voice_recovery_error=type(exc).__name__)
                else:
                    self.update(record,voice_recovery_job_id=jid,voice_recovery_error=None)
            review=dependency.get("voice_review") or {}
            if (diagnostic_queued and dependency["reason"]=="voice_review_input_unavailable"
                    and review.get("corrected_input_format")=="voice-audition/input-audio-last/1"
                    and not record.get("voice_inspection_job_id") and record.get("voice_inspection_after",0)<=time.time()):
                from tools.workbench_loop_voice import request_voice_inspection
                self.update(record,voice_inspection_after=time.time()+300)
                try:
                    jid=request_voice_inspection(self.root,job,review["candidate"])
                    if not ID.fullmatch(str(jid)):
                        raise ValueError("invalid voice inspection job receipt")
                except TraceError:
                    raise
                except Exception as exc:
                    self.update(record,voice_inspection_error=type(exc).__name__)
                else:
                    self.update(record,voice_inspection_job_id=jid,voice_inspection_error=None)
            return
        if record["kind"] in ASSETS:
            items=(job.get("result") or {}).get("asset_diagnostics",{}).get("items",[])
            unknown=any(i.get("verdict")=="UNKNOWN" or i.get("stage")=="unknown" for i in items)
            reason="asset_receipt_unknown" if unknown else "asset_reconciliation_required"
            self.update(record,status="awaiting_evidence",reason=reason)
            self._schedule_triage(key,record,job,{"reason":reason,"status":"unknown" if unknown else "needs_input"})
            return
        owner=self.owned(job,key)
        if owner:
            self._sync_run(record,owner)
            if owner["phase"] not in TERMINAL:
                # If our own child crashed, recover its checkpoint under the same deterministic identity.
                self.dispatch(key,record,resume_id=owner["id"])
                return
            if owner["phase"]=="succeeded":
                return
            if _legacy_retry(owner,job):
                self.dispatch(key,record,resume_id=owner["id"],legacy_job=job)
                return
            if owner.get("job_id") != job["id"] or not passive_observation(owner):
                original=owner.get("observation") or {"status":owner["phase"],"reason":owner.get("reason")}
                self._schedule_triage(key,record,job,original)
                return
        if record.get("reason")=="driver_failed_before_checkpoint":
            self._schedule_triage(key,record,job,{"reason":record["reason"],"status":"unknown"})
            return
        triage = record.get("triage") or {}
        rechecking = bool(triage) and record["status"] not in {"queued", "handling"}
        probe_key = ("agent_evidence", key)
        if rechecking or owner:
            if triage.get("status") == "running" and self.triage_key != key:
                self._schedule_triage(key,record,job,{})
            if (record["status"] not in {"blocked", "unknown"} or record.get("run_id") and not owner
                    or job.get("cancel_requested") or job.get("trace_incomplete")
                    or (job.get("result") or {}).get("status") in {"cancelled", "budget_exhausted"}
                    or record.get("reason") in {"job_cancelled", "trace_incomplete", "repair_limit",
                                                "same_fault_after_three_repairs", "repeated_interruption"}
                    or self.next_check.get(probe_key, 0) > time.monotonic()):
                return
        role="video" if job["kind"].endswith("video") else "script"
        obs=observe_job(self.root,job,session_id=job.get("session_id") or "",role=role)
        self.next_check[probe_key] = time.monotonic() + 60
        if owner and (obs.get("job_id") != job["id"] or owner["observation"].get("trace_id")
                      and obs.get("trace_id") != owner["observation"]["trace_id"]):
            return
        if _actionable(obs):
            if owner:
                self.dispatch(key,record,resume_id=owner["id"],previous_observation=owner["observation"])
            else:
                self.dispatch(key,record)
        else:
            self.update(record,status="unknown" if obs["status"]=="unknown" else "blocked",reason=obs["reason"])
            self._schedule_triage(key,record,job,obs)

    def collect_children(self):
        self._completed_triage()
        if self.child and self.child.poll() is not None:
            code=self.child.returncode
            event("workbench.watch.driver.response",{"fingerprint":self.child_key,"exit":code},kind="subprocess")
            record=self.state["events"][self.child_key]
            if code and not (self.runs/record["run_id"]/"run.json").is_file():
                with _lock(self.runs/"driver.lock",create=False) as free:
                    if free:
                        self.update(record,status="blocked",reason="driver_failed_before_checkpoint")
            self.child=None

    def tick(self):
        (self.folder/"heartbeat").touch()
        self.collect_children()
        web_state=self.root/"outputs/web/state.json"
        stat=web_state.stat() if web_state.is_file() else None
        stamp=(stat.st_mtime_ns,stat.st_size) if stat else None
        changed=stamp!=self.state_stamp
        if changed:
            index=read_json(web_state) if stat else {}
            self.jobs={j["id"]:j for j in index.get("jobs",[])}
            self.register_stamps(index)
            self.state_stamp=stamp
        jobs=self.jobs
        include=self.folder/"include"
        requested={p.stem:p for p in include.glob("*.json") if ID.fullmatch(p.stem)}
        if changed or requested:
            for jid,j in jobs.items():
                self.register(j,include=jid in requested)
        for jid,path in requested.items():
            if jid in self.state["latest"]:
                path.unlink(missing_ok=True)
        for key,record in list(self.state["events"].items()):
            jid=record["job_id"]
            job=jobs.get(jid)
            same_failure=job and _needs_attention(job) and _signature(job)==key
            if not same_failure:
                self.update(record,status="watching" if job and job["status"] in {"queued","running"} else "resolved",
                            reason="original_job_changed")
                original={"id":jid,"kind":record["kind"],"status":record.get("original_status","failed"),
                          "trace_id":record.get("trace_id"),"task_trace_id":record.get("task_trace_id"),
                          "event_id":record.get("event_id")}
                self._schedule_triage(key,record,original,{"status":"resolved","reason":"original_job_changed",
                                                           "trace_id":record.get("trace_id")})
                continue
            if self.retry_inspection.get(key,0)>time.monotonic():
                continue
            try:
                self.process(key,record,jobs[jid],jobs)
            except TraceError:
                raise
            except Exception as exc:
                # An unreadable record blocks this job only. Leave independent deliveries running.
                self.update(record,status="unknown",reason="watch_inspection_error",error_type=type(exc).__name__)
                self.retry_inspection[key]=time.monotonic()+30

    def serve(self, generation):
        (self.folder/"heartbeat").write_text(str(os.getpid()),encoding="ascii")
        write_json(self.folder/"instance.json",{"pid":os.getpid(),"generation":generation})
        while True:
            try:
                if watcher_paused(self.root):
                    return
                if _generation(self.folder)!=generation:
                    (self.folder/"heartbeat").touch()
                    self.collect_children()
                    if self.triage_future is None and self.child is None:
                        _spawn(self.root,handoff=True)
                        return
                    time.sleep(2)
                    continue
                self.tick()
            except TraceError:
                raise
            except (OSError,ValueError,subprocess.SubprocessError) as exc:
                event("workbench.watch.scan_error",{"type":type(exc).__name__},kind="error")
            time.sleep(3)


def _generation(folder):
    marker=folder/"generation.json"
    return read_json(marker)["generation"] if marker.exists() else "initial"


def _spawn(root, *, handoff=False):
    folder=_folder(root)
    argv=[sys.executable,"-u","-m","tools.workbench_loop_watch"]
    if handoff:
        argv.append("--handoff")
    event("workbench.watch.start.request",{"argv":argv,"cwd":str(root)},kind="subprocess")
    with (folder/"watch.log").open("ab") as log:
        process=subprocess.Popen(argv,cwd=root,stdin=subprocess.DEVNULL,stdout=log,stderr=log,
            env={**os.environ,"PIANCHANG_LOOP_PARENT_CONTEXT":json.dumps(current_context())},
            start_new_session=os.name!="nt",creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    event("workbench.watch.start.response",{"pid":process.pid},kind="subprocess")
    return {"status":"started","pid":process.pid}


@traced("workbench.watch.start",kind="subprocess",capture_inputs=False)
def start_watcher(root, *, include_job=None, generation=None):
    root=Path(root).resolve()
    folder=_folder(root)
    if watcher_paused(root):
        return {"status":"blocked","reason":"trace_incomplete"}
    if include_job:
        _include(root,include_job)
    if generation is not None and str(generation)!=_generation(folder):
        write_json(folder/"generation.json",{"generation":str(generation)})
    with _lock(folder/"watch.lock") as free:
        if not free:
            return {"status":"running"}
        _load(root)
    return _spawn(root)


@traced("cli.workbench_loop_watch",kind="cli",capture_inputs=False)
def main():
    from pianchang.model_routes import load_routes
    for credential in load_routes(ROOT/".env").secrets():
        register_secret(credential)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--include-job",help="把这条已有失败记录纳入本机监听")
    parser.add_argument("--handoff",action="store_true",help=argparse.SUPPRESS)
    args=parser.parse_args()
    if watcher_paused(ROOT):
        return
    if args.include_job:
        _include(ROOT,args.include_job)
    deadline=time.monotonic()+60
    while True:
        with _lock(_folder(ROOT)/"watch.lock") as free:
            if free:
                watcher=Watcher(ROOT)
                try:
                    watcher.serve(_generation(watcher.folder))
                except TraceError as exc:
                    mark_trace_pause(ROOT,exc)
                    raise
                finally:
                    watcher.close()
                return
        if not args.handoff or time.monotonic()>=deadline:
            return
        time.sleep(.25)


if __name__=="__main__":
    parent=json.loads(os.environ.pop("PIANCHANG_LOOP_PARENT_CONTEXT","null"))
    if not watcher_paused(ROOT):
        try:
            with use_context(parent,tracer=get_tracer(ROOT/"outputs/traces")),trace("workbench.watch.run"):
                main()
        except TraceError:
            mark_trace_pause(ROOT)
            raise
