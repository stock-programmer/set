"""Read frozen production inputs and real job outcomes, without submitting media."""
from pianchang.bailian_video import IMAGE_PURPOSES, VideoError, config_for_shooting_plan, validate_reference_capabilities
from pianchang.collab_store import CollabStore
from trace_system import event, traced


@traced("pi.video.production_diagnostics", kind="validation", capture_inputs=False)
def production_diagnostics(service, session):
    with service.tracer.read_batch():
        return _production_diagnostics(service,session)


def _production_diagnostics(service, session):
    from pianchang.studio_assets import studio_project_for_video
    from pianchang.video_execution import current_commission
    if not session.get("project_id"):
        return {"commission":None,"jobs":[],"shots":[],"reason":"故事尚无制作工程。"}
    view=studio_project_for_video(service,session["project_id"])
    current=current_commission(service,session["project_id"],view=view)
    with service.lock:
        jobs=[{k:j.get(k) for k in ("id","kind","status","message","error","trace_id","updated_at")}
              for j in service.state["jobs"] if j.get("project_id")==session["project_id"]
              and (j.get("id")==current.get("job_id") or
                   j.get("session_id")==session["id"] and j.get("status") in {"failed","interrupted"})][-6:]
    result={"commission":current,"jobs":jobs,"shots":[],
        "evidence_scope":"Live read of frozen inputs and retained job outcomes; no generation or replay.",
        "interpretation":"last_execution=null means no linked conversation continuation, not that generation never ran. Approved asset count does not prove model input compatibility."}
    if not current.get("commission_id"):return result
    store=CollabStore(view["store_path"])
    order=store.get_message(current["commission_id"])
    pin=order["payload"]["template"]
    asset=store.get_asset(pin["asset_id"],pin["revision_id"])
    valid=asset["story_id"]==session["story_id"] and asset["sha256"]==pin["sha256"]
    event("pi.video.diagnostic_scope",{"path":"diagnostic.frozen_spec","expected":True,"actual":valid,"status":"PASS" if valid else "FAIL"},kind="validation")
    if not valid:raise ValueError("制作诊断的冻结规格不属于当前故事或哈希不匹配。")
    result["spec_ref"]=pin
    deferred=set((current.get("plan") or {}).get("deferred_shots",[]))
    for shot in asset["content"].get("shots",[]):
        if shot["id"] in deferred:continue
        refs=shot.get("required_refs",[])
        images=[r for r in refs if r.get("purpose") in IMAGE_PURPOSES]
        config=config_for_shooting_plan(service.video_config,refs,order["payload"].get("shooting_plan") or {})
        item={"shot_id":shot["id"],"model":config.model,"image_count":len(images),
              "image_subjects":[r["subject_id"] for r in images],"references":refs,
              "validation_scope":"Current deterministic capability revalidation of frozen inputs, not a historical provider response."}
        try:
            validate_reference_capabilities(config,refs)
            item.update(status="PASS",error=None)
        except VideoError as exc:
            item.update(status="FAIL",error=str(exc))
        if item["status"]=="FAIL":
            from .input_tools import commission_reference_inputs
            from trace_system import TraceError
            try:item["model_input"]=commission_reference_inputs(service,session).read(shot["id"])
            except TraceError:raise
            except (KeyError,ValueError,OSError,AttributeError) as exc:
                item["model_input"]={"ready":False,"error":"输入编排资料无法读取："+str(exc)}
        result["shots"].append(item)
    result["preflight_ready"]=all(s["status"]=="PASS" or (s.get("model_input") or {}).get("ready") for s in result["shots"])
    return result
