"""Current video commission and its immutable execution limits; no second Agent."""
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from trace_system import event,traced


class StandaloneAgentRetired(ValueError):
    def __init__(self,pid):
        self.conversation={"scope":{"project":pid},"role":"video"}
        super().__init__("独立 Agent 执行入口已合并，请在当前故事的视频制作 Agent 对话中提出要求；额度与验收标准沿用当前制作委托。")


@traced("video.execution.limits",kind="validation",capture_inputs=False)
def execution_limits(plan,video_budget,shot_count=0):
    valid=type(video_budget) is int and video_budget>=0 and type(shot_count) is int and shot_count>=0
    event("video.execution.limit_input",{"path":"execution_limits.video_budget_and_shots","expected":"nonnegative integers",
        "actual":{"video_budget":video_budget,"shot_count":shot_count},"status":"PASS" if valid else "FAIL"},kind="validation")
    if not valid:raise ValueError("制作额度与镜头数量必须为非负整数。")
    policy=(plan or {}).get("acceptance_loop")
    steps=(max(30,len(policy["shot_ids"])*(6+3*policy["max_repairs_per_shot"])+20) if policy else
        max(18,2*shot_count+8) if plan is not None else 18)
    return {"max_video_generations":video_budget,"max_model_calls":steps,
        "max_repairs_per_shot":(policy or {}).get("max_repairs_per_shot"),
        "max_local_edits_per_shot":(policy or {}).get("max_local_edits_per_shot")}


@traced("video.execution.current_commission",kind="validation",capture_inputs=False)
def current_commission(service,pid,*,view=None,jobs=None):
    from pianchang.studio_assets import studio_project_for_video
    view=view if view is not None else studio_project_for_video(service,pid)
    if jobs is None:
        with service.lock:jobs=list(service.state["jobs"])
    # Include completed commissions: an older failure must never become current
    # merely because it is resumable. The state index is append-only in time.
    candidates=[j for j in jobs if j.get("kind")=="studio_video" and j.get("project_id")==pid]
    source=next((j for j in reversed(candidates) if j.get("message_id") or j.get("status") in {"queued","running"}),candidates[-1] if candidates else None)
    result={"project_id":pid,"story_id":view["story_id"],"job_id":source["id"] if source else None,
        "commission_id":(source or {}).get("message_id"),"status":(source or {}).get("status","not_commissioned"),
        "resumable":False,"blockers":[],"limits":None,"usage":None,"plan":None,"last_execution":None,
        "completion_rule":"所有镜头及衔接必过项通过后才完成视频制作并开放人工预览。"}
    if not source:
        result["blockers"].append("尚无已确认的制作委托。请先核对本阶段拍摄计划及执行限制。")
        return result
    cid=source.get("message_id");run_id=source.get("run_id")
    if not cid or not run_id:
        result["blockers"].append("拍摄计划仍在准备，尚未保存冻结委托与额度。")
        return result
    if not all(re.fullmatch(r"[a-f0-9]{32}",str(x)) for x in (cid,run_id)):
        raise ValueError("制作委托标识无效，停止恢复。")
    uri = Path(view["store_path"]).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.row_factory = sqlite3.Row
        order = db.execute("""SELECT m.story_id, m.recipient, m.message_type, m.payload,
                d.status AS delivery_status FROM messages m LEFT JOIN deliveries d
                ON d.message_id=m.message_id AND d.agent='video' WHERE m.message_id=?""", (cid,)).fetchone()
    if order is None:
        raise KeyError("Unknown collaboration message: " + cid)
    valid=(order["story_id"]==view["story_id"] and order["recipient"]=="video"
           and order["message_type"]=="production.requested")
    event("video.execution.commission_scope",{"path":"commission.story_and_recipient","expected":True,"actual":valid,"status":"PASS" if valid else "FAIL"},kind="validation")
    if not valid:raise ValueError("当前制作委托与故事不匹配。")
    body=json.loads(order["payload"]);plan=body.get("shooting_plan") or {};policy=plan.get("acceptance_loop") or {}
    result.update(plan=plan,script_ref=body.get("script"),spec_ref=body.get("template"),trace_id=source.get("trace_id"))
    if body.get("script")!=view.get("script_ref") or body.get("template")!=view.get("spec"):
        result["blockers"].append("当前剧本或制作规格已变更；旧委托不能沿用到新版，请先确认新版计划。")
    work=Path(view["store_path"]).parent/"runs"/run_id/("video-spec-"+cid)
    path=work/"agent/run.json"
    record=json.loads(path.read_text("utf-8")) if path.is_file() else {}
    budget=body["max_video_generations"]
    result["limits"]={"source":"frozen_commission","max_video_generations":budget,
        "max_repairs_per_shot":policy.get("max_repairs_per_shot"),"max_local_edits_per_shot":policy.get("max_local_edits_per_shot"),
        "max_model_calls":(record.get("limits") or {}).get("max_steps",(body.get("execution_limits") or {}).get("max_model_calls"))}
    if record:
        reserved=record.get("video_generations",0)
        result["usage"]={"reserved_video_generations":reserved,"remaining_video_generations":max(0,budget-reserved),
            "model_calls":record.get("steps",0),"repair_reservations":record.get("repair_reservations",{})}
        if (record.get("limits") or {}).get("max_video_generations")!=budget:
            result["blockers"].append("运行检查点与冻结委托的额度不一致，须核对原回执，不能重置额度。")
    from pianchang.reference_inputs import external_review_usage
    reference_external=len(external_review_usage(work/"reference-inputs")["reservations"])
    from pi_bridge.recovery import strategy_external_usage
    strategy_external=strategy_external_usage(work)
    external=reference_external+strategy_external
    if external:
        if result["usage"] is None:result["usage"]={"reserved_video_generations":0,"remaining_video_generations":budget,"model_calls":0,"repair_reservations":{}}
        result["usage"].update(model_calls=record.get("steps",0)+external,reference_input_review_calls=reference_external,
                               unconfirmed_strategy_calls=strategy_external)
        result["reference_inputs_usage_path"]=str(work/"reference-inputs/review-reservations.json")
    latest=next((j for j in reversed(jobs) if j.get("kind")=="pi_video" and j.get("project_id")==pid and j.get("commission_id")==cid),None)
    if latest and (source.get("updated_at") or "")>(latest.get("updated_at") or latest.get("created_at") or ""):
        latest=source
    if latest:
        result["last_execution"]={k:latest.get(k) for k in ("id","status","trace_id","message")}
        if (latest.get("result") or {}).get("task_complete"):
            result["blockers"].append("此委托已由当前会话完成交付。修改制作范围须先确认新版计划。")
        result["status"]=latest.get("status")
    result["delivery_status"]=order["delivery_status"]
    if result["delivery_status"]=="completed":result["blockers"].append("此委托已有交付回执，请核对已验收版本；不能恢复更早的失败任务。")
    if source["status"]=="succeeded":result["blockers"].append("此委托已交付，不能恢复更早的失败任务。")
    result["resumable"]=source["status"] in {"failed","interrupted"} and not result["blockers"]
    return result
