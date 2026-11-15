"""Persistent production scheduling, strategy changes and reconciliation of existing work.

This is part of the project's executor. It never resolves an uncertain video by POSTing again.
"""
from __future__ import annotations

import copy
import difflib
import json
import time
from pathlib import Path

from pianchang.agent_loop import Tool, _validate
from pianchang.story_spec import digest_json
from trace_system import TraceError, event, get_tracer, traced

VERSION = "video-autonomy/1"
FAMILIES = ["timed_action_beats", "blocking_and_paths", "camera_and_visibility", "reference_composition"]


def _strategy_context(result):
    return {**{k:result[k] for k in ("repair_id","shot_id","candidate_sha256","report_sha256","criterion_ids","next_action") if k in result},
            "strategy":{k:result["strategy_design"][k] for k in ("strategy_family","change_from_previous")},
            "full_plan_sha256":digest_json(result)}


@traced("video.autonomy.recoverable_coordination",kind="validation",capture_inputs=False)
def recoverable_coordination(directory):
    """Only protocol-rejected, unexecuted decisions with remaining steps qualify."""
    found=[]
    for path in Path(directory).glob("coordination/*/response/run.json"):
        run=json.loads(path.read_text("utf-8"))
        if run.get("status")!="needs_input" or run["steps"]>=run["limits"]["max_steps"]:continue
        errors=[]
        for message in run["messages"]:
            if message["role"]!="user":continue
            try:item=json.loads(message["content"])
            except ValueError:continue
            if item.get("type")=="tool_result" and not item.get("ok"):errors.append(item.get("error",{}).get("code"))
        if errors and all(e in {"INVALID_DECISION","INVALID_TOOL_ARGUMENTS","FINISH_REJECTED"} for e in errors):
            found.append({"path":str(path),"remaining_steps":run["limits"]["max_steps"]-run["steps"],"errors":errors})
    return found


@traced("video.autonomy.recoverable_order",kind="validation",capture_inputs=False)
def recoverable_order(directory):
    with get_tracer().read_batch():
        return _recoverable_order(directory)


def _recoverable_order(directory):
    path=Path(directory)/"agent/run.json"
    if not path.is_file():return False
    run=json.loads(path.read_text("utf-8"))
    missing_evaluation=False
    selection_regression=False
    missing_subtitle=False
    project_path=Path(directory)/"project.json"
    reports_path=Path(directory)/"reports.json"
    if project_path.is_file() and reports_path.is_file():
        from pianchang.acceptance import required_subtitle
        reports=json.loads(reports_path.read_text("utf-8"))
        for shot in json.loads(project_path.read_text("utf-8"))["shots"]:
            if required_subtitle(shot) and shot["id"] in reports:
                report=json.loads(Path(reports[shot["id"]]["report_path"]).read_text("utf-8"))
                subtitle=next((r for r in report["results"] if r["criterion_id"]=="script_subtitle"),None)
                if subtitle is None or (subtitle["status"]=="UNKNOWN" and subtitle.get("execution_status") in {"evidence_invalid","execution_error"}
                                        and not subtitle.get("frame_audit_version")):missing_subtitle=True
    history=Path(directory)/"repair-history.json"
    if history.is_file():
        from pianchang.video_repair import preserved_passes
        clips_path=Path(directory)/"clips.json"
        selected=json.loads(clips_path.read_text("utf-8")) if clips_path.is_file() else {}
        for item in json.loads(history.read_text("utf-8")).get("history",[]):
            summary=item.get("result_report")
            if summary and item.get("status")!="blocked_evaluation" and not json.loads(Path(summary["report_path"]).read_text("utf-8")).get("content_review_version"):
                missing_evaluation=True
            clip=item.get("result_clip")
            if (clip and summary and item.get("source_clip") and item.get("source_report") and
                    selected.get(item["shot_id"],{}).get("media_sha256")==clip["media_sha256"]):
                selection_regression=selection_regression or bool(
                    set(preserved_passes(item["source_clip"],item["source_report"],selected))-
                    set(preserved_passes(clip,summary,selected)))
    joint_selection=False
    if project_path.is_file() and reports_path.is_file() and history.is_file():
        from pianchang.video_repair import verified_selection_plan
        clips_path=Path(directory)/"clips.json"
        if clips_path.is_file():
            joint_selection=bool(verified_selection_plan(json.loads(project_path.read_text("utf-8")),
                json.loads(clips_path.read_text("utf-8")),json.loads(reports_path.read_text("utf-8")),
                json.loads(history.read_text("utf-8")).get("history",[]))["changes"])
    pending_provider=False
    state=Path(directory)/"autonomy.json"
    if state.exists():
        pending_provider=any(b.get("task_id") and b.get("manifest_path") and not b.get("terminal_failure")
            for b in json.loads(state.read_text("utf-8")).get("provider_blockers",{}).values())
    return bool(((run.get("result") or {}).get("reason") in {"context_limit","runtime_interruption","runtime_cleanup","trace_failure"} and run["steps"]<run["limits"]["max_steps"])
                or pending_provider and run["steps"]<run["limits"]["max_steps"]
                or recoverable_coordination(directory) or missing_evaluation or selection_regression or missing_subtitle or joint_selection)


@traced("video.autonomy.enqueue_recovery", kind="task", capture_inputs=False)
def enqueue_recovery(service,*,recover_trace_failures=False):
    """Production startup owns recovery, under the exclusive workbench process lock."""
    import copy
    from contextlib import nullcontext
    from pianchang.app_service import now
    queued=[]
    sessions=getattr(service,"_pi_sessions",None)
    def eligible(job):
        return (job.get("kind")=="studio_video" and job.get("status") in {"interrupted","failed"}
            and job.get("_autonomy_enabled") and not job.get("cancel_requested") and (recover_trace_failures or not job.get("trace_incomplete")))
    def current_ids(jobs):
        candidates={}
        for entry in jobs:
            if entry.get("kind")=="studio_video":candidates.setdefault(entry["project_id"],[]).append(entry)
        return {pid:next((j["id"] for j in reversed(rows) if j.get("message_id") or j.get("status") in {"queued","running"}),rows[-1]["id"]) for pid,rows in candidates.items()}
    with service.lock:
        pending=[copy.deepcopy(j) for j in service.state["jobs"] if eligible(j)]
        current=current_ids(service.state["jobs"])
    for snapshot in pending:
        if current.get(snapshot["project_id"])!=snapshot["id"]:continue
        job=copy.deepcopy(snapshot);recovered={}
        if job.get("trace_incomplete"):
            probe=service.tracer.start_span("video.autonomy.trace_storage_probe",kind="validation",new_trace=True,
                inputs={"job_id":job["id"],"purpose":"prove durable storage before any recovery dispatch"})
            probe.event("video.autonomy.trace_storage_check",{"path":"trace.storage.durable_write","expected":True,"actual":True,"status":"PASS"},kind="validation")
            service.tracer.end_span(probe.context,status="OK")
            recovered.update(trace_incomplete=False,trace_recovery_probe=probe.context)
            job.update(recovered)
        if not job.get("message_id"):
            from pianchang.studio_assets import studio_project_for_video
            from pianchang.collab_store import CollabStore
            view=studio_project_for_video(service,job["project_id"])
            store=CollabStore(view["store_path"])
            matches=[m for m in store.messages(view["run_id"],message_type="production.requested")
                     if m["trace_context"]["trace_id"]==job.get("trace_id")]
            if len(matches)>1:
                event("video.autonomy.ambiguous_order",{"job_id":job["id"],"message_ids":[m["message_id"] for m in matches]},kind="error")
                continue
            if matches:
                m=matches[0]
                recovered=dict(message_id=m["message_id"],run_id=view["run_id"],story_id=m["story_id"],
                    script_ref=m["payload"]["script"],spec_ref=m["payload"]["template"])
                job.update(recovered)
        checkpoint={"message_id":job.get("message_id")}
        work=None
        if job.get("run_id") and job.get("message_id") and getattr(service,"root",None):
            work=service.root/"outputs/collaborations/runs"/job["run_id"]/("video-spec-"+job["message_id"])
            path=work/"agent/run.json"
            if path.is_file():
                from pi_bridge.recovery import reconcile_checkpoint
                reconciliation=reconcile_checkpoint(service,job,work)
                if reconciliation.get("changed"):recovered["checkpoint_reconciliation"]=reconciliation
                record=json.loads(path.read_text("utf-8"))
                checkpoint.update({k:record.get(k) for k in ["steps","video_generations","pending_tool"]})
            for name in ["clips.json","reports.json"]:
                path=work/name
                if path.is_file():checkpoint[name]=digest_json(json.loads(path.read_text("utf-8")))
        checkpoint_hash=digest_json(checkpoint)
        if snapshot.get("_autonomy_recovery_checkpoint")==checkpoint_hash and snapshot.get("_autonomy_recovery_stalls",0)>=3:
            event("video.autonomy.recovery_limit",{"job_id":job["id"],"checkpoint":checkpoint,"reason":"同一执行点连续三次恢复没有进展；保留原额度与证据。"},kind="state")
            continue
        # File, media and collaboration reads never own the application/session
        # lock. The final claim below rechecks state after this expensive work.
        if snapshot.get("status")=="failed" or (snapshot.get("result") or {}).get("action_required"):
            if work is None or not recoverable_order(work):continue
        conversation=None
        if sessions:
            conversation=sessions.resolve({"project":job["project_id"]},"video")
            if job.get("message_id") and job.get("run_id") and not recovered:
                from pianchang.video_execution import current_commission
                commission=current_commission(service,job["project_id"])
                if commission["job_id"]!=job["id"] or not commission["resumable"]:continue
        _,project=service._project(job["project_id"])
        project_hash=digest_json(project)
        with sessions.lock if sessions else nullcontext(),service.lock:
            live=next((j for j in service.state["jobs"] if j["id"]==snapshot["id"]),None)
            if not live or not eligible(live) or current_ids(service.state["jobs"]).get(live["project_id"])!=live["id"]:continue
            if any(live.get(k)!=snapshot.get(k) for k in ("trace_id","message_id","run_id")):continue
            if conversation:
                active_id=sessions.repo.get(conversation["id"])["active_job"]
                active=next((j for j in service.state["jobs"] if j["id"]==active_id and j["status"] in {"queued","running"}),None)
                if active and active["id"]!=live["id"]:continue
            count=live.get("_autonomy_recovery_count",0)
            stalls=live.get("_autonomy_recovery_stalls",0) if live.get("_autonomy_recovery_checkpoint")==checkpoint_hash else 0
            if stalls>=3:continue
            previous=live.get("_trace_context")
            root=service.tracer.start_span("agent.task.studio_video_recovery",kind="task",new_trace=True,
                inputs={"job_id":live["id"],"frozen_request_id":job.get("message_id"),"recovery_number":count+1},
                links=[{**previous,"relation":"recovers"}] if previous else [])
            live["previous_execution"]={"status":live.get("status"),"trace_id":live.get("trace_id"),"error":live.get("error"),
                "summary":(live.get("result") or {}).get("summary"),"updated_at":live.get("updated_at")}
            live.update(recovered,status="queued",error=None,result=None,updated_at=now(),_autonomy_resuming=bool(job.get("message_id")),
                _autonomy_recovery_count=count+1,trace_id=root.trace_id,_trace_context=root.context,
                _autonomy_recovery_checkpoint=checkpoint_hash,_autonomy_recovery_stalls=stalls+1,
                message="项目执行器正在恢复原视频制作委托；保留原生成额度，不重复提交未知任务。")
            root.event("task.queued",{"job_id":live["id"],"budget_reset":False},kind="state")
            root.event("pi.recovery.resuming",{"summary":"项目执行器接续原制作委托，沿用检查点、现有产物与剩余额度；先核对未确认操作，再继续自动验收和修复。",
                "commission_id":job.get("message_id"),"checkpoint":checkpoint,"reconciliation":recovered.get("checkpoint_reconciliation"),"budget_reset":False},kind="state")
            if conversation:
                sessions.repo.update(conversation["id"],active_job=live["id"])
                live.update(session_id=conversation["id"],backend="pi")
            service._save()
            service.executor.submit(service._execute,live,copy.deepcopy(live["_autonomy_body"]),project_hash)
            queued.append(live["id"])
    return {"jobs":queued,"new_commissions":0}


class VideoAutonomy:
    def __init__(self, workspace, negotiation=None):
        self.w, self.negotiation = workspace, negotiation
        self.blocked = {}
        self.coordination = []
        self.coordination_errors = {}
        self.review_attempts = {}
        self.specification_blockers = {}
        self.selection_recoveries = []
        self.tools = [Tool("design_repair_strategy",
            "根据当前实际失败证据和所有历史方案设计不同的拍摄策略，并绑定当前候选准备修复。不能重复失败提示或放宽条件。",
            {"type":"object","properties":{"shot_id":{"type":"string","minLength":1,"maxLength":128}},
             "required":["shot_id"]}, self.design, effect="write",context_result=_strategy_context),
            Tool("coordinate_agents", "处理本次制作请求的剧本协商并读取实际回复；由项目执行器自行调度，不需要外部唤醒。",
                 {"type":"object","properties":{}}, self.coordinate, effect="write"),
            Tool("review_existing_shot", "恢复已存在视频但尚未实际执行或中断的验收。保留调用收据，不能重复不确定的模型请求。",
                 {"type":"object","properties":{"shot_id":{"type":"string","minLength":1,"maxLength":128}},
                  "required":["shot_id"]}, self.review_existing, effect="write"),
            Tool("record_specification_blocker", "将剧本 Agent 已核对的具体起止字段矛盾登记为受阻。须引用本轮实际协商和原文；不改变必过项及FAIL/UNKNOWN。其他可修字段继续。",
                 {"type":"object","properties":{
                     "shot_id":{"type":"string","minLength":1,"maxLength":128},
                     "phase":{"type":"string","enum":["start","end"]},
                     "field_path":{"type":"string","minLength":1,"maxLength":200},
                     "request_id":{"type":"string","minLength":1,"maxLength":128},
                     "script_quote":{"type":"string","minLength":4,"maxLength":500},
                     "reason":{"type":"string","minLength":20,"maxLength":1800}},
                  "required":["shot_id","phase","field_path","request_id","script_quote","reason"]},self.record_specification_blocker,effect="write")]
        self.tools.append(Tool("resume_pending_generation","只查询原 task_id，接纳并自动验收已完成视频；绝不重复视频 POST，不重置额度。其他可修工作优先。",
            {"type":"object","properties":{"shot_id":{"type":"string"}},"required":["shot_id"]},
            lambda shot_id:self.observe_pending(shot_id,wait=True),effect="write"))

    @traced("video.autonomy.queue", kind="validation", capture_inputs=False)
    def queue(self, record):
        w = self.w
        issues = w.acceptance_gate(validate=False)["issues"] if w.project else []
        scope = set((w.shooting_plan or {}).get("acceptance_loop", {}).get("shot_ids", []))
        deferred = set((w.shooting_plan or {}).get("deferred_shots", []))
        remaining = record["limits"]["max_video_generations"] - record["video_generations"]
        policy = (w.shooting_plan or {}).get("acceptance_loop", {})
        reservations = record.get("repair_reservations", {})
        actionable, blockers, exhausted = [], [], []
        for issue in issues:
            sid = issue["shot_id"]
            row = copy.deepcopy(issue)
            if sid in deferred or scope and sid not in scope:
                row["blocker_kind"] = "outside_authorized_scope"; blockers.append(row)
            elif sid in self.blocked:
                operation=self.blocked[sid]
                row.update(blocker_kind="unconfirmed_operation", operation=operation)
                if operation.get("task_id") and operation.get("manifest_path") and not operation.get("terminal_failure"):
                    row["next_action"]="resume_pending_generation";actionable.append(row)
                else:blockers.append(row)
            elif issue["criterion_id"]=="media_or_report":
                if sid in w.shots:
                    row["next_action"]="review_existing_shot";actionable.append(row)
                elif sid in (w.impact or {}).get("reuse",[]):
                    row["next_action"]="reuse_shot";actionable.append(row)
                elif remaining>0:
                    if getattr(w,"reference_store",None) and len([p for p in w._required(sid) if p["purpose"]!="voice"])>5:
                        prepared=w.reference_inputs(sid).read(sid)
                        if not prepared["ready"]:
                            available=[p for p in prepared["strategies"] if p["status"] not in {"FAIL","UNKNOWN","review_requested"} and not p["blockers"]]
                            row.update(reference_inputs=prepared,next_action="prepare_reference_inputs" if available else "request_script_revision")
                            if not available:row["blocker_kind"]="reference_inputs_need_collaboration";blockers.append(row);continue
                        else:row["next_action"]="generate_shot"
                    else:row["next_action"]="generate_shot"
                    actionable.append(row)
                else:
                    row["blocker_kind"]="authorized_repair_limit";exhausted.append(row)
            elif issue["status"]=="UNKNOWN" and "未执行" in issue.get("reason",""):
                row["next_action"]="review_existing_shot";actionable.append(row)
            elif issue["criterion_id"]=="script_subtitle" and issue["status"]=="FAIL":
                if w.repair.local_edits.get(sid,0)<policy.get("max_local_edits_per_shot",2):
                    row["next_action"]="prepare_shot_repair";row["repair_method"]="edit_shot_asset";actionable.append(row)
                else:
                    row["blocker_kind"]="local_edit_limit";exhausted.append(row)
            elif issue["status"] == "UNKNOWN":
                row["blocker_kind"] = "insufficient_evidence"; blockers.append(row)
            elif issue["criterion_id"] in {"evidence_integrity","candidate_binding","reference_submission","acceptance_binding","required_coverage"}:
                row["blocker_kind"] = "evidence_integrity"; blockers.append(row)
            elif self.state_failures_blocked(sid,issue):
                row["blocker_kind"]="evidenced_specification_conflict"
                row["field_evidence"]=[v for v in self.specification_blockers.values() if v["shot_id"]==sid]
                blockers.append(row)
            elif remaining <= 0 or reservations.get(sid, 0) >= policy.get("max_repairs_per_shot", 2):
                row["blocker_kind"] = "authorized_repair_limit"; exhausted.append(row)
            else:
                # Regression reports can include a downstream edge. Repair one of its actual sources.
                cid = issue["criterion_id"]
                if cid.startswith("continuity:") and sid not in cid.split(":",1)[1].split("->"):
                    continue
                actionable.append(row)
        actionable.sort(key=lambda r: (r["criterion_id"] != "action_sequence", r["shot_id"],r["criterion_id"]))
        result = {"version":VERSION,"actionable":actionable,"blocked":blockers,"exhausted":exhausted,
                  "remaining_video_generations":remaining,
                  "next_action":(actionable[0].get("next_action") or "design_repair_strategy") if actionable else "coordinate_agents_or_report_evidenced_blockers",
                  "all_required_passed":not issues}
        event("video.autonomy.work_partition", result, kind="validation")
        return result

    def state_failures_blocked(self,sid,issue):
        if issue["criterion_id"]!="start_end_state":return False
        from pianchang.video_collab_agent import _read
        report=_read(self.w.reports[sid]["report_path"])
        checks=next(r for r in report["results"] if r["criterion_id"]=="start_end_state").get("state_checks",[])
        failed=[c for c in checks if c["verdict"]=="FAIL"]
        return bool(failed) and all(sid+"/"+c["phase"]+"/"+c["path"] in self.specification_blockers for c in failed)

    @traced("video.autonomy.record_specification_blocker",kind="tool",capture_inputs=False)
    def record_specification_blocker(self,shot_id,phase,field_path,request_id,script_quote,reason):
        from pianchang.video_collab_agent import _read,_write
        n=self.negotiation
        if not n:raise ValueError("没有实际的跨 Agent 协商，不能登记已核对的规格矛盾。")
        request=n.store.get_message(request_id)
        if request["causation_id"]!=n.production_message_id:raise ValueError("协商请求不属于当前制作委托。")
        response=n.read_script_response(request_id)
        if response["status"]!="answered" or response["response"]["decision"]=="accept":
            raise ValueError("须先完成原剧本与验收规格核对；改稿任务不能用于豁免当前验收。")
        basis=request["payload"]["evidence_ref"]
        evidence=n.store.get_asset(basis["asset_id"],basis["revision_id"])["content"]
        if evidence.get("shot_id")!=shot_id or evidence["report"]["project_sha256"]!=digest_json(self.w.project):
            raise ValueError("协商证据不属于当前镜头及冻结工程。")
        report=_read(self.w.reports[shot_id]["report_path"])
        checks=next(r for r in report["results"] if r["criterion_id"]=="start_end_state").get("state_checks",[])
        field=next((c for c in checks if c["path"]==field_path and c["phase"]==phase and c["verdict"]!="PASS"),None)
        old=next((r for r in evidence["report"]["results"] if r["criterion_id"]=="start_end_state"),{})
        original=next((c for c in old.get("state_checks",[]) if c["path"]==field_path and c["phase"]==phase),None)
        shot=next(s for s in self.w.project["shots"] if s["id"]==shot_id)
        if (not field or not original or original["expected"]!=field["expected"] or
                not any(script_quote in text for text in [shot["description"],*[r["quote"] for r in shot.get("source_refs",[])]])):
            raise ValueError("字段、预期值或剧本引用不匹配；不能以泛泛说明停止修复。")
        result={"shot_id":shot_id,"phase":phase,"field_path":field_path,"expected":field["expected"],
                "actual":field["actual"],"original_verdict":field["verdict"],"script_quote":script_quote,
                "reason":reason,"request_id":request_id,"response":response,"evidence_ref":basis,
                "report_sha256":self.w.reports[shot_id]["report_sha256"],"criterion_status_unchanged":True}
        self.specification_blockers[shot_id+"/"+phase+"/"+field_path]=result
        _write(self.w.directory/"specification-blockers.json",self.specification_blockers)
        event("video.autonomy.specification_blocker",result,kind="validation")
        return result

    @traced("video.autonomy.tick", kind="task", capture_inputs=False)
    def tick(self, record):
        from pianchang.video_collab_agent import _write
        from pianchang.video_repair import verified_selection_plan
        if self.blocked:self.observe_pending(wait=False)
        if getattr(self.w,"project",None) and getattr(self.w,"repair",None):
            selection=verified_selection_plan(self.w.project,self.w.shots,self.w.reports,self.w.repair.history)
            if selection["changes"]:
                self.selection_recoveries.extend(self.w.repair.recover_best_candidates())
        try:
            if self.negotiation and self.w.project:
                self.request_clarifications()
            self.coordinate()
        except TraceError:raise
        except Exception as exc:
            key="coordination"
            self.coordination_errors[key]={"reason":str(exc),"attempts":self.coordination_errors.get(key,{}).get("attempts",0)+1}
            event("video.autonomy.coordination_blocked",self.coordination_errors[key],kind="error")
        result = self.queue(record)
        # Keep rich evidence on disk; send only the actionable ids and concise blockers to the planner.
        _write(self.w.directory / "autonomy.json", {**result,"provider_blockers":self.blocked,
                "coordination":self.coordination,"coordination_errors":self.coordination_errors,
                "review_attempts":self.review_attempts,"steps":record["steps"],
                "specification_blockers":self.specification_blockers,
                "candidate_selection":self.selection_recoveries,
                "video_generations":record["video_generations"],"repair_reservations":record.get("repair_reservations",{})})
        return {**{k:result[k] for k in ["remaining_video_generations","next_action","all_required_passed"]},
                **{k:[{f:r.get(f) for f in ["shot_id","criterion_id","status","blocker_kind","next_action","repair_method"]} for r in result[k]]
                   for k in ["actionable","blocked","exhausted"]},
                "coordination":self.coordination[-2:],
                "coordination_errors":self.coordination_errors,
                "rule":"只要actionable非空就必须继续；未知/画外/规格矛盾不得中断其他可修项。"}

    @traced("video.autonomy.observe_pending",kind="tool",capture_inputs=False)
    def observe_pending(self,shot_id=None,*,wait=False):
        from pianchang.video_agent import VideoRunner
        from pianchang.video_collab_agent import _read,_write
        observed=[];w=self.w
        for sid,blocked in list(self.blocked.items()):
            if shot_id and sid!=shot_id:continue
            if not blocked.get("task_id") or not blocked.get("manifest_path") or blocked.get("terminal_failure"):continue
            if not wait and blocked.get("next_poll_at",0)>time.time():continue
            path=Path(blocked["manifest_path"])
            try:
                manifest=_read(path);meta=manifest.get("metadata",{})
                valid=(manifest.get("task_id")==blocked["task_id"] and meta.get("script_asset_ref")==w.ref and
                    meta.get("shot_id")==sid and meta.get("dependency_sha256")==w.impact["shots"][sid]["target"]["sha256"])
                event("video.autonomy.pending_binding",{"path":sid,"expected":True,"actual":valid,"status":"PASS" if valid else "FAIL"},kind="validation")
                if not valid:raise ValueError("既有任务与当前冻结镜头绑定不符，禁止接纳或重提。")
                repair_id=(meta.get("acceptance_repair") or {}).get("repair_id")
                repair=w.repair.plans.get(repair_id) if repair_id else None
                if repair_id and not repair:raise ValueError("既有重拍缺少原修复方案，保留任务不重提。")
                result=VideoRunner(w.video_config_for(sid)).resume(path,wait=wait)
                if result.get("status")!="SUCCEEDED":
                    blocked.update(provider_status=result.get("status"),next_poll_at=time.time()+30)
                    observed.append({"shot_id":sid,"status":result.get("status"),"task_id":blocked["task_id"]});continue
                w.adopt_generated(sid,result,repair=repair)
                if repair:
                    history=next(h for h in w.repair.history if h["repair_id"]==repair_id)
                    history.update(status="reviewed",result_clip=copy.deepcopy(w.shots[sid]),result_report=copy.deepcopy(w.reports[sid]))
                    w.repair.assess_candidate(history);w.repair._paths()
                self.blocked.pop(sid,None)
                observed.append({"shot_id":sid,"status":"recovered_and_reviewed","task_id":blocked["task_id"]})
            except TraceError:raise
            except Exception as exc:
                current=_read(path) if path.is_file() else {}
                blocked.update(reason=str(exc),next_poll_at=time.time()+30,terminal_failure=current.get("status") in {"FAILED","CANCELED","UNKNOWN"})
                observed.append({"shot_id":sid,"status":"query_or_adoption_incomplete","reason":str(exc)})
        if observed:
            previous=_read(w.directory/"autonomy.json") if (w.directory/"autonomy.json").exists() else {}
            _write(w.directory/"autonomy.json",{**previous,"provider_blockers":self.blocked})
        return {"observations":observed,"new_video_posts":0,"budget_reset":False}

    @traced("video.autonomy.review_existing", kind="tool", capture_inputs=False)
    def review_existing(self, shot_id):
        if self.review_attempts.get(shot_id,0)>=2:
            raise ValueError("已两次实际尝试恢复该项检查，执行错误和收据已保留；继续其他镜头。")
        self.review_attempts[shot_id]=self.review_attempts.get(shot_id,0)+1
        try:
            result=self.w.review_shot(shot_id)
            self.blocked.pop(shot_id,None)
            return result
        except TraceError:raise
        except Exception as exc:
            self.blocked[shot_id]={"reason":"验收恢复执行错误："+str(exc),"kind":"check_execution_error",
                                   "attempts":self.review_attempts[shot_id]}
            return {"status":"blocked_check_execution","evidence":self.blocked[shot_id]}

    @traced("video.autonomy.request_clarifications", kind="tool", capture_inputs=False)
    def request_clarifications(self):
        from pianchang.video_collab_agent import _read
        n=self.negotiation
        existing=n.store.messages(causation_id=n.production_message_id,message_type="script.revision_requested")
        beats={r["payload"]["beat_id"] for r in existing}
        for sid,summary in self.w.reports.items():
            report=_read(summary["report_path"])
            row=next((r for r in report["results"] if r["criterion_id"]=="start_end_state" and r["status"]!="PASS"),None)
            if not row or not row.get("state_checks"):continue
            shot=next(s for s in self.w.project["shots"] if s["id"]==sid)
            beat=next((r["source_id"] for r in shot.get("source_refs",[]) if r["source_id"].startswith("B")),None)
            if not beat or beat in beats:continue
            asset_id=n.story_id+"/acceptance-evidence/"+sid+"/"+summary["report_sha256"][:16]
            content={"shot_id":sid,"shot":shot,"report":report,"report_path":summary["report_path"],
                 "media_sha256":report["media_sha256"],"report_sha256":summary["report_sha256"],
                 "contract":_read(Path(summary["report_path"]).parent/"contract.json")}
            try:evidence=n.store.get_asset(asset_id)
            except KeyError:
                evidence=n.store.publish_asset(asset_id,content,kind="inspection",producer="video",story_id=n.story_id,
                    dependencies=[{"asset_id":self.w.ref["asset_id"],"revision_id":self.w.ref["revision_id"],"role":"observes"}])
            if digest_json(evidence["content"])!=digest_json(content):raise ValueError("已有澄清证据资产不匹配。")
            n.request_script_revision(beat,"[验收规格核对，禁止改写剧本] "+sid+"："+row["reason"][:2400],
                "保持已冻结的原剧本。请对照绑定的验收证据、起止字段与原节拍，给出可执行的拍摄建议；"
                "无法同时满足时明确指出具体字段、原文和冲突原因。不能删除/降级必过项来声称通过。此请求只核对规格，不委托改写故事。",
                evidence["asset_id"],evidence["revision_id"])
            beats.add(beat)
        return {"requested_beats":sorted(beats)}

    @traced("video.autonomy.coordinate", kind="tool", capture_inputs=False)
    def coordinate(self):
        if not self.negotiation:
            return {"status":"not_configured"}
        if self.coordination_errors.get("coordination",{}).get("attempts",0)>=3:
            return {"status":"blocked_after_three_recorded_errors","evidence":self.coordination_errors}
        from pianchang.collab_negotiation import run_script_negotiation, consume_revision_work
        n = self.negotiation
        requests = n.store.messages(causation_id=n.production_message_id, message_type="script.revision_requested")
        observed = []
        for request in requests:
            mid = request["message_id"]
            if self.coordination_errors.get(mid,{}).get("attempts",0)>=3:
                observed.append({"request_id":mid,"status":"blocked","evidence":self.coordination_errors[mid]})
                continue
            folder = self.w.directory / "coordination" / digest_json(mid)[:12]
            try:
                if request["deliveries"].get("script") in {"pending","received","accepted","unknown","blocked"}:
                    run_script_negotiation(self.w.decision_client, folder / "response", store=n.store, request_id=mid)
                response = n.read_script_response(mid)
                work = response.get("response", {}).get("revision_work_id")
                if work and n.store.get_message(work)["deliveries"].get("script") == "pending":
                    consume_revision_work(self.w.decision_client, folder / "revision", store=n.store, message_id=work)
                    response = n.read_script_response(mid)
            except TraceError:raise
            except Exception as exc:
                error={"request_id":mid,"reason":str(exc),"attempts":self.coordination_errors.get(mid,{}).get("attempts",0)+1}
                self.coordination_errors[mid]=error
                event("video.autonomy.request_blocked",error,kind="error")
                observed.append({"request_id":mid,"status":"blocked","evidence":error})
                continue
            if not any(x["request_id"] == mid and x.get("response") == response for x in self.coordination):
                self.coordination.append({"request_id":mid,"response":response})
            for reply in n.store.messages(causation_id=mid, message_type="script.revision_response"):
                if n.store.claim("video", message_id=reply["message_id"]):
                    n.store.ack("video",reply["message_id"],"completed",result={"observed":response,
                        "frozen_criteria_changed":False})
            observed.append({"request_id":mid,"status":response["status"],
                             "response":response.get("response"),"published_revisions":response.get("published_revisions",[])})
        return {"observations":observed,"frozen_criteria_changed":False}

    def attempts(self, sid):
        from pianchang.video_collab_agent import _read
        attempts = []
        clips = [self.w.shots.get(sid)]
        for h in self.w.repair.history:
            if h["shot_id"] == sid:
                clips.extend([h.get("source_clip"),h.get("result_clip")])
        seen = set()
        for clip in clips:
            if not clip or not clip.get("manifest_path"):continue
            manifest = _read(clip["manifest_path"])
            if digest_json(manifest) != clip["generation_job_sha256"]:
                raise ValueError("历史方案的生成任务哈希不符："+sid)
            a = manifest.get("metadata",{}).get("acceptance_repair")
            if a and a["instruction"] not in seen:
                seen.add(a["instruction"]); attempts.append(a)
        return attempts

    @traced("video.autonomy.design_strategy", kind="tool", capture_inputs=False)
    def design(self, shot_id):
        from pianchang.video_collab_agent import _write, _read
        w = self.w
        if shot_id in self.blocked:
            raise ValueError("该镜头已有未确认的提交，不能设计再次付费生成。")
        report = w.repair.read(shot_id)
        rows = [r for r in report["issues"] if r["status"] == "FAIL" and
                (not r["criterion_id"].startswith("continuity:") or shot_id in r["criterion_id"].split(":",1)[1].split("->"))]
        if not rows:raise ValueError("该镜头无可重拍的明确失败项；保留未知项的具体证据缺口。")
        # Fix action first. Other mandatory checks remain in the automatic re-review and gate.
        rows = [r for r in rows if r["criterion_id"] == "action_sequence"] or rows
        attempts = self.attempts(shot_id)
        order=[s["id"] for s in w.project["shots"]]
        index=order.index(shot_id)
        adjacent=set(order[max(0,index-1):index]+order[index+1:index+2])
        neighbor_observations=[]
        for sid in adjacent:
            if sid not in w.reports:continue
            summary=w.reports[sid];actual=_read(summary["report_path"])
            if digest_json(actual)!=summary["report_sha256"]:raise ValueError("相邻镜头验收报告哈希不符。")
            neighbor_observations.append({"shot_id":sid,"media_sha256":actual["media_sha256"],
                "report_sha256":summary["report_sha256"],"observations":[r for r in actual["results"]
                    if r["criterion_id"] in {"action_sequence","start_end_state"} or r["criterion_id"].startswith("continuity:")]})
        schema={"type":"object","properties":{
            "strategy_family":{"type":"string","enum":FAMILIES},
            "change_from_previous":{"type":"string","minLength":20,"maxLength":1800},
            "instruction":{"type":"string","minLength":40,"maxLength":3500}},
            "required":["strategy_family","change_from_previous","instruction"]}
        messages=[{"role":"system","content":"你是项目的视频修复策略设计器。返回JSON，只含strategy_family、change_from_previous、instruction。"
            "必须从失败画面的实际观察提出新的可执行拍法，改变时间分配、动作路径、起始姿态或可见机位；不能只换措辞。"
            "逐秒编排连续动作，明确第一帧、每次动作的起止、最后一帧。先修复确实缺失的表演动作。"
            "保留原剧本、原批准身份/服装/场景参考及所有必过条件。不得添加剧本没有的对白、角色或剧情。"
            "衔接修复必须匹配实际邻镜头，不能引用无关边；规格矛盾仍要报告，不能通过删验收项或字幕冒充动作。"
            "如果已有失败方案，新方案必须说明具体改变了什么；优先换策略类别。"},
            {"role":"user","content":json.dumps({"families":FAMILIES,"shot":report["shots"],"failures":rows,
                "previous_actual_attempts":attempts,"related_acceptance_issues":report["issues"],
                "neighbor_shots":[s for s in w.project["shots"] if s["id"] in adjacent],
                "neighbor_actual_observations":neighbor_observations,
                "coordination":self.coordination[-2:],
                "retired_unconfirmed_designs":self._design_recovery_context()},ensure_ascii=False)}]
        from pianchang.bailian_text import text_client_for
        design_client = text_client_for(w.decision_client, "video.repair_strategy")
        for retry in range(3):
            if getattr(w,"reserve_strategy_review",None):w.reserve_strategy_review()
            result=design_client.complete_json(copy.deepcopy(messages))
            design=result["data"]
            try:
                _validate(design,schema)
                w.repair.validate_generation_strategy(shot_id,design["instruction"])
                for a in attempts:
                    ratio=difflib.SequenceMatcher(None,"".join(a["instruction"].split()),"".join(design["instruction"].split())).ratio()
                    event("video.autonomy.strategy_difference",{"path":shot_id,"expected":"similarity < 0.94", "actual":ratio,
                        "status":"PASS" if ratio<.94 else "FAIL"},kind="validation")
                    if ratio>=.94:raise ValueError("方案只是近似重复，请改动实际调度或时间分配。")
                break
            except (ValueError,KeyError) as exc:
                messages.append({"role":"user","content":"上份设计未执行任何视频提交。校验失败："+str(exc)+"。请改变实际拍法后重新设计。"})
        else:raise ValueError("三次策略设计都未提供不同的有效方案，未进行视频提交。")
        plan=w.repair.prepare(shot_id,[r["criterion_id"] for r in rows],design["instruction"],design["change_from_previous"])
        w.repair.plans[plan["repair_id"]]["strategy_design"] = design
        _write(w.directory/"repair-plans"/(plan["repair_id"]+".json"),w.repair.plans[plan["repair_id"]])
        w.repair._paths()
        return {**plan,"strategy_design":design,"next_action":"regenerate_shot"}

    def _design_recovery_context(self):
        path=self.w.directory/"strategy-recovery.json"
        return list(json.loads(path.read_text("utf-8"))["operations"].values()) if path.exists() else []

    @traced("video.autonomy.recover", kind="task", capture_inputs=False)
    def recover(self, record):
        from pianchang.video_collab_agent import _read, _write, _validated_media
        from pianchang.video_agent import VideoRunner
        w = self.w
        # Restore only the current immutable order, never infer media from another commission.
        saved_project = _read(w.directory / "project.json")
        if saved_project != w.template or digest_json(saved_project.get("script_source",{}).get("script")) != digest_json(w.script):
            raise ValueError("恢复工程与冻结委托不符。")
        w.project=saved_project;w.impact=_read(w.directory/"impact.json")
        if w.register_specs:
            w.reference_specs=w.register_specs(saved_project)
        w.shots=_read(w.directory/"clips.json") if (w.directory/"clips.json").exists() else {}
        w.reports=_read(w.directory/"reports.json") if (w.directory/"reports.json").exists() else {}
        # Startup defers reviews while loading individual reused clips. A restored
        # complete selection must leave that mode, or later repairs would get only technical checks.
        w.defer_content_review=False
        history=_read(w.directory/"repair-history.json") if (w.directory/"repair-history.json").exists() else {}
        w.repair.plans=history.get("plans",{});w.repair.history=history.get("history",[])
        w.repair.local_edits={sid:sum(h["shot_id"]==sid and h["strategy"]=="local_black_tail" for h in w.repair.history) for sid in w.shots}
        previous=_read(w.directory/"autonomy.json") if (w.directory/"autonomy.json").exists() else {}
        self.blocked=previous.get("provider_blockers",{})
        self.coordination=previous.get("coordination",[])
        self.coordination_errors=previous.get("coordination_errors",{})
        self.review_attempts=previous.get("review_attempts",{})
        self.specification_blockers=_read(w.directory/"specification-blockers.json") if (w.directory/"specification-blockers.json").exists() else {}
        self.selection_recoveries=previous.get("candidate_selection",[])
        for clip in w.shots.values():_validated_media(clip,require_origin=True)
        pending=record.get("pending_tool") or {}
        if not pending:
            unfinished={h["repair_id"] for h in w.repair.history if h["status"]=="started_or_unknown"}
            for msg in reversed(record.get("messages",[])):
                if msg["role"]!="assistant":continue
                try:decision=json.loads(msg["content"])
                except (ValueError,TypeError):continue
                if decision.get("arguments",{}).get("repair_id") in unfinished:
                    name=decision.get("tool")
                    pending={"tool":name,"arguments":decision["arguments"],
                             "effect":"video" if name=="regenerate_shot" else "write"}
                    break
        args=pending.get("arguments",{});sid=args.get("shot_id")
        outcomes=[]
        if pending.get("effect")=="video" and sid:
            repair=w.repair.plans.get(args.get("repair_id"))
            key=w.operation_key(sid,repair)
            matches=[]
            for path in Path(w.video_config.output_dir).resolve().glob("*/job.json"):
                job=_read(path)
                if job.get("metadata",{}).get("execution_key")==key:matches.append((path,job))
            from pianchang.video_agent import verified_local_failure
            not_sent = [path for path,j in matches if verified_local_failure(j, path)]
            matches = [(path,j) for path,j in matches if path not in not_sent]
            if not matches and not_sent:
                self.blocked.pop(sid,None)
                for history in w.repair.history:
                    if history["repair_id"] == args.get("repair_id") and history["status"] == "started_or_unknown":
                        history.update(status="local_failure_before_post",manifest_paths=[str(path) for path in not_sent])
                outcomes.append({"shot_id":sid,"status":"local_failure_before_post","new_video_posts":0,
                                 "manifest_paths":[str(path) for path in not_sent]})
            elif len(matches)!=1 or not matches[0][1].get("task_id"):
                self.blocked[sid]={"reason":"已有视频操作缺少唯一可核对的provider task_id；禁止重复POST。",
                    "execution_key":key,"manifest_paths":[str(p) for p,j in matches],"pending_tool":pending}
                outcomes.append({"shot_id":sid,"status":"blocked_unconfirmed_submission",**self.blocked[sid]})
                for h in w.repair.history:
                    if h["repair_id"]==args.get("repair_id"):h.update(status="blocked_unconfirmed_submission",evidence=self.blocked[sid])
            else:
                path,job=matches[0]
                try:
                    if job["metadata"].get("script_asset_ref")!=w.ref or job["metadata"].get("dependency_sha256")!=w.impact["shots"][sid]["target"]["sha256"]:
                        raise ValueError("已有任务的剧本或镜头依赖不匹配。")
                    result=VideoRunner(w.video_config_for(sid)).resume(path,wait=False)
                    if result.get("status")!="SUCCEEDED":
                        self.blocked[sid]={"reason":"原云端任务仍在处理，继续使用同一 task_id 查询。","manifest_path":str(path),
                            "task_id":job["task_id"],"provider_status":result.get("status"),"next_poll_at":time.time()+30,"new_video_posts":0}
                        outcomes.append({"shot_id":sid,"status":"provider_pending","task_id":job["task_id"],"new_video_posts":0})
                    else:
                        w.adopt_generated(sid,result,repair=repair)
                        for h in w.repair.history:
                            if h["repair_id"]==args.get("repair_id"):
                                h.update(status="reviewed",result_clip=copy.deepcopy(w.shots[sid]),result_report=copy.deepcopy(w.reports[sid]))
                        self.blocked.pop(sid,None)
                        outcomes.append({"shot_id":sid,"status":"recovered_and_reviewed","task_id":job["task_id"],"new_video_posts":0})
                except TraceError:raise
                except Exception as exc:
                    self.blocked[sid]={"reason":str(exc),"manifest_path":str(path),"task_id":job["task_id"],"new_video_posts":0}
                    outcomes.append({"shot_id":sid,"status":"blocked_recovery",**self.blocked[sid]})
        elif pending.get("effect")=="write":
            if pending.get("tool")=="design_repair_strategy":
                from pi_bridge.recovery import reconcile_design
                receipt=reconcile_design(w.directory,record,get_tracer())
                outcomes.append(receipt)
                self.blocked.pop(sid,None)
            # Never blindly repeat a partially completed edit/negotiation. Persist the unresolved operation.
            if sid and pending.get("tool")=="edit_shot_asset":
                repair_id=args.get("repair_id")
                receipt=w.directory/"edited-candidates"/repair_id/"lineage.json"
                if receipt.is_file():
                    from pianchang.video_repair import validate_lineage
                    lineage=_read(receipt)
                    reference={"path":str(receipt),"sha256":digest_json(lineage)}
                    validate_lineage(reference,output_path=lineage["output_path"],output_sha256=lineage["output_sha256"])
                    clip={**lineage["source_clip"],"operation":"edited","media_path":lineage["output_path"],
                          "media_sha256":lineage["output_sha256"],"media_lineage":reference,"repair_id":repair_id}
                    _validated_media(clip,require_origin=True)
                    w.shots[sid]=clip
                    _write(w.directory/"clips.json",w.shots)
                    h=next(h for h in w.repair.history if h["repair_id"]==repair_id)
                    w.repair._complete(h)
                    self.blocked.pop(sid,None)
                    outcomes.append({"shot_id":sid,"status":"recovered_local_edit_and_reviewed","new_video_posts":0})
                else:
                    self.blocked[sid]={"reason":"本地修改中断且缺少完整产物谱系，保留部分文件，未冒认修改成功。","pending_tool":pending}
        w.repair._paths()
        self.observe_pending(wait=False)
        pending_evaluations=w.repair.recover_pending_evaluations()
        for history in w.repair.history:
            if history["status"]=="reviewed":w.repair.assess_candidate(history)
        candidate_selection=w.repair.recover_best_candidates()
        self.selection_recoveries.extend(candidate_selection)
        if w.vision_config is not None and w.reference_store is not None:
            from pianchang.content_review import run_content_reviews
            run_content_reviews(w)
        self.coordinate()
        _write(w.directory/"autonomy.json",{**previous,"provider_blockers":self.blocked,"coordination":self.coordination,
               "candidate_selection":self.selection_recoveries})
        return {"outcomes":outcomes,"blocked_shots":self.blocked,"budget_preserved":True,
                "candidate_selection":candidate_selection,
                "pending_evaluations":pending_evaluations,
                "content_review_deferred":w.defer_content_review,
                "new_video_posts":0,"next_action":"继续其他可修镜头；受阻项保留原证据。"}
