"""The conversation and production worker share the same frozen input compiler."""
from pathlib import Path
from pianchang.collab_store import CollabStore
from pianchang.reference_inputs import ReferenceInputs
from trace_system import event,traced


@traced("pi.video.reference_inputs",kind="tool",capture_inputs=False,capture_output=False)
def commission_reference_inputs(service,session,reserve_review=None):
    from pianchang.studio_assets import studio_project_for_video
    from pianchang.video_execution import current_commission
    view=studio_project_for_video(service,session["project_id"])
    current=current_commission(service,session["project_id"],view=view)
    if not current.get("commission_id"):raise ValueError("尚无冻结制作委托，不能擅自编排其他版本。")
    with service.lock:source=next(dict(j) for j in service.state["jobs"] if j["id"]==current["job_id"])
    store=CollabStore(view["store_path"]);order=store.get_message(current["commission_id"])
    pin=order["payload"]["template"];asset=store.get_asset(pin["asset_id"],pin["revision_id"])
    valid=asset["sha256"]==pin["sha256"] and asset["story_id"]==session["story_id"]
    event("pi.video.reference_input_scope",{"path":"reference_input.frozen_spec","expected":True,"actual":valid,"status":"PASS" if valid else "FAIL"},kind="validation")
    if not valid:raise ValueError("冻结输入规格哈希或故事归属不符。")
    from pianchang.bailian_video import config_for_shooting_plan
    shots={s["id"]:s for s in asset["content"]["shots"]}
    def config_for_shot(shot_id):
        return config_for_shooting_plan(service.video_config,shots[shot_id].get("required_refs",[]),
                                        order["payload"].get("shooting_plan") or {})
    def approved(pins):
        from pianchang.asset_dispatch import check_reference_approval
        from pianchang.studio_assets import studio_asset_catalog
        catalog=studio_asset_catalog(service,session["project_id"])
        for pin in pins:check_reference_approval(service,session["project_id"],pin,catalog=catalog)
    folder=Path(store.path).parent/"runs"/source["run_id"]/("video-spec-"+current["commission_id"])/"reference-inputs"
    def reserve():
        from pianchang.reference_inputs import reserve_external_review
        if not reserve_review:raise ValueError("只读会话不能预留视觉模型调用。")
        reserve_review()
        reserve_external_review(folder,(current.get("limits") or {}).get("max_model_calls"))
    return ReferenceInputs(store=store,story_id=session["story_id"],project=asset["content"],directory=folder,
        config=config_for_shot,vision_config=service.vision_config,reserve_review=reserve if reserve_review else None,validate_sources=approved)


@traced("studio.reference_inputs.view",kind="tool",capture_inputs=False)
def reference_inputs_view(service,view,commission):
    if not commission.get("commission_id"):return []
    compiler=commission_reference_inputs(service,{"project_id":view["project_id"],"story_id":view["story_id"]})
    deferred=set((commission.get("plan") or {}).get("deferred_shots",[]));rows=[]
    for shot in compiler.project["shots"]:
        if shot["id"] in deferred:continue
        status=compiler.read(shot["id"]);selected=status["selected"]
        first=status["strategies"][0]
        row={"shot_id":shot["id"],"ready":status["ready"],"original_image_count":first["original_image_count"],
             "provider_image_count":selected["provider_image_count"] if selected else None,"strategy":selected["strategy"] if selected else None,
             "strategies":status["strategies"],"boards":[],"checks":[]}
        if selected and selected.get("bundle"):
            pin=selected["bundle"];manifest=compiler.store.get_asset(pin["asset_id"],pin["revision_id"])["content"]
            row.update(bundle=pin,checks=manifest["checks"],trace_id=manifest["review"]["context"]["trace_id"])
            for i,slot in enumerate(manifest["slots"],1):
                if slot["board"]:
                    path=Path(slot["board"]["path"])
                    row["boards"].append({"slot":i,"sources":slot["source_refs"],"media_sha256":slot["provider_ref"]["media_sha256"],
                        "url":service.asset(path,slot["provider_ref"]["media_sha256"])})
        rows.append(row)
    return rows
