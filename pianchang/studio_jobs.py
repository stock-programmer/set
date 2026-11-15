"""Workbench jobs joining the independent script/video agents through pinned assets."""
from __future__ import annotations

import re
import uuid
from pathlib import Path

from pianchang.app_service import project_id_for_path, read_json, story_constraints, write_json
from pianchang.bailian_text import BailianTextClient
from pianchang.collaboration_workflow import Collaboration, dep, open_ref, ref
from pianchang.script_collab_agent import ScriptWorkspace, publish_script_revision, run_script_agent
from pianchang.story_agent import StoryRunner
from pianchang.story_spec import digest_json, validate_compilation, validate_script, validate_structure_constraints
from trace_system import current_context, event, traced


def studio_run_id(project_id):
    """Stable collaboration identity for a project which predates the workbench."""
    return uuid.uuid5(uuid.NAMESPACE_URL, "drama-studio/project/" + project_id).hex


def _checked(path, expected, actual):
    ok = expected == actual
    event("studio.check", {"path": path, "expected": expected, "actual": actual,
                           "status": "PASS" if ok else "FAIL"}, kind="validation")
    if not ok:
        raise ValueError("工作台版本或输入不符：" + path + "；请刷新当前故事。")


@traced("studio.validate_submission", kind="validation")
def validate_submission(action, body):
    if action == "studio_review":
        pointer = body.get("continuation")
        if (set(body) != {"continuation"} or not isinstance(pointer, dict)
                or set(pointer) != {"request_id", "sha256"}
                or not re.fullmatch(r"[a-f0-9]{32}", str(pointer.get("request_id")))
                or not re.fullmatch(r"[a-f0-9]{64}", str(pointer.get("sha256")))):
            raise ValueError("自动验收须绑定已有制作委托及素材指纹。")
        return
    if action == "studio_video":
        if set(body) - {"spec_ref", "script_ref", "project_sha256", "max_video_generations", "plan_id", "proposal_id", "repair_existing_only"}:
            raise ValueError("制作委托含有未知参数。")
        if "repair_existing_only" in body and (type(body["repair_existing_only"]) is not bool or "plan_id" not in body):
            raise ValueError("已有素材修复须绑定当前拍摄计划。")
        if "plan_id" in body:
            if not re.fullmatch("[a-f0-9]{64}", str(body["plan_id"])) or "max_video_generations" in body:
                raise ValueError("请按页面显示的拍摄计划开始制作。")
            if "proposal_id" in body and not re.fullmatch("[a-f0-9]{64}", str(body["proposal_id"])):
                raise ValueError("额外重拍计划无效，请刷新。")
        elif ("proposal_id" in body or type(body.get("max_video_generations")) is not int
              or body["max_video_generations"] < 0):
            raise ValueError("请提交当前拍摄计划，或为旧版委托明确填写授权生成数量。")
        for key in ("script_ref", "spec_ref"):
            pin = body.get(key)
            if pin is not None and (not isinstance(pin, dict) or set(pin) != {"asset_id", "revision_id", "sha256"}
                    or not isinstance(pin["asset_id"], str) or not pin["asset_id"]
                    or not re.fullmatch(r"r[1-9]\d*", str(pin["revision_id"]))
                    or not re.fullmatch("[a-f0-9]{64}", str(pin["sha256"]))):
                raise ValueError(f"{key} 须包含完整的资产 ID、版本与 SHA256。")
        if body.get("spec_ref") is None and not re.fullmatch("[a-f0-9]{64}", str(body.get("project_sha256"))):
            raise ValueError("首次委托须确认当前制作工程 SHA256。")
        return
    if set(body) - {"idea", "episodes", "scenes", "beats", "script", "style"}:
        raise ValueError("剧本委托含有未知参数。")
    if not isinstance(body.get("style",""), str) or len(body.get("style","")) > 3000:
        raise ValueError("全剧制作要求最多 3000 字。")
    if "script" in body:
        if set(body) - {"script", "style"} or not isinstance(body["script"], dict):
            raise ValueError("导入时只提交 screenplay/1 剧本对象。")
        issues = validate_script(body["script"])
        if issues:
            raise ValueError("导入剧本不符合 screenplay/1：" + str(issues[0]))
    else:
        if not isinstance(body.get("idea"), str) or not 1 <= len(body["idea"].strip()) <= 6000:
            raise ValueError("请输入 1～6000 字符的创意。")
        validate_structure_constraints(story_constraints(body))


def _session(service, run_id, title, pid=None):
    session = Collaboration(service.root / "outputs" / "collaborations", run_id, env_file=service.root / ".env")
    if not session.index.exists():
        session.write(id=run_id, title=title, project_id=pid, trace_id=current_context()["trace_id"],
                      stages=[], productions=[], status="running")
    session.store.subscribe("video", "script.revision_published", session.story)
    return session


@traced("studio.script_job", kind="task", capture_inputs=False)
def run_script_job(service, job, body):
    client=BailianTextClient(service.text_config)
    if getattr(client,"agent_backend",None)!="offline_legacy":
        from pi_bridge.service import get_sessions
        return get_sessions(service).run_initial(job,body)
    directory = service._story_directory(job["id"])
    original = body.get("script")
    title = original["title"] if original else body["idea"][:60]
    session = _session(service, job["id"], title)
    service._update(job, run_id=session.run_id, message="剧本 Agent 正在检查并发布剧本")
    name = "剧本首次发布"
    def progress(data):
        tid = current_context()["trace_id"]
        session.stage(name, tid, "running", "r1", data.get("summary") or "正在处理剧本")
        service._update(job, script_trace_id=tid, story_progress=data, message=data.get("summary") or name)

    def publish(script, report):
        return publish_script_revision(session.store, script, report, asset_id=session.story + "/script",
                                       story_id=session.story, correlation_id=session.run_id)

    constraints = story_constraints(body)
    result = run_script_agent(BailianTextClient(service.text_config), session.folder / "script-r1",
        story_id=session.story, goal="核验并原样发布导入的剧本。" if original else body["idea"],
        mode="import" if original else "create", constraints=constraints, script=original, publish=publish,
        trace_attributes={"correlation_id": session.run_id, "job_id": job["id"]}, progress=progress)
    loop = result["loop_result"]
    session.stage(name, result["trace_id"], loop["status"], "r1", loop["summary"])
    if not loop["task_complete"]:
        session.write(status="blocked", error=loop["summary"])
        raise ValueError("剧本 Agent 停止：" + loop["summary"])
    script = open_ref(session.store, result["receipt"])
    data = {"run_id": session.run_id, "story_id": session.story, "script_ref": ref(script),
            "script_trace_id": result["trace_id"]}
    session.write(title=script["content"]["title"], script_ref=ref(script), status="script_published")
    service._update(job, **data, message="剧本版本已发布；正在编译分镜与人物、场景、道具设定")
    compile_dir = session.folder / "compile-r1"
    summary = StoryRunner(service.text_config).compile(script["content"], compile_dir, max_repairs=2)
    report = read_json(compile_dir / "validation-report.json")
    write_json(directory / "validation-report.json", report)
    if not summary["ready_for_generation"]:
        return {**data, "project_id": None, "ready_for_generation": False,
                "summary": "剧本已发布；分镜规格检查失败，详见验证报告。"}
    project = read_json(compile_dir / "project.json")
    pid = project_id_for_path(directory / "project.json")
    source = session.store.publish_asset(session.story + "/source-project", project, kind="input_project",
        producer="commissioner", story_id=session.story, correlation_id=session.run_id,
        dependencies=[dep(script, "target_script")])
    data.update(project_id=pid, source_ref=ref(source), ready_for_generation=True)
    write_json(directory / "studio.json", data)
    session.write(project_id=pid, source_ref=ref(source))
    from pianchang.asset_dispatch import schedule_published_assets
    data["asset_job"] = schedule_published_assets(service, directory, pid, job, body.get("style", ""), project=project)
    return {**data, "summary": "剧本与分镜已保存，制作委托可选用这个版本。"}


def _source_script(session, view):
    project = view["project"]
    script = project.get("script_source", {}).get("script")
    if not isinstance(script, dict):
        raise ValueError("工程缺少可核验的 screenplay/1 剧本。请先导入剧本。")
    report = validate_compilation(project, script)
    _checked("production_spec.ready_for_generation", True, report["ready_for_generation"])
    if view.get("script_ref"):
        asset = open_ref(session.store, view["script_ref"])
    else:
        revisions = session.store.revisions(session.story + "/script")
        asset = next((r for r in reversed(revisions) if r["sha256"] == digest_json(script)), None)
        if asset is None:
            if revisions:
                raise ValueError("当前工程引用的剧本与此协作已发布的版本不符。")
            def publish(candidate, report):
                return publish_script_revision(session.store, candidate, report, asset_id=session.story + "/script",
                                               story_id=session.story, correlation_id=session.run_id)
            # A validated external script can commission video without running a writing model.
            workspace = ScriptWorkspace(session.folder / "script-input-r1", story_id=session.story,
                goal="核验并登记外部制作工程中的冻结剧本", mode="import", script=script, base_revision=None,
                beat_id=None, constraints={}, publish=publish)
            workspace.directory.mkdir(parents=True, exist_ok=True)
            workspace.check_script()
            asset = open_ref(session.store, workspace.publish_script())
    _checked("script.story_id", session.story, asset["story_id"])
    _checked("script.sha256", digest_json(script), asset["sha256"])
    return asset


def _handoff_spec(session, script, view, body):
    selected = body.get("spec_ref")
    project = view["project"]
    if selected is not None:
        _checked("spec.ref", selected, ref(view["spec"]))
        spec = open_ref(session.store, selected)
    else:
        _checked("spec.project_sha256", body["project_sha256"], digest_json(project))
        if view["spec"].get("revision_id"):
            spec = open_ref(session.store, view["spec"])
        else:
            from pianchang.reference_assets import resolve_references
            dependencies = [dep(script, "target_script")]
            for shot in project["shots"]:
                pins = shot.get("required_refs", [])
                if pins:
                    resolve_references(session.store, pins, story_id=session.story)
                    dependencies.extend(dep(pin, "required_reference", shot["id"]) for pin in pins)
            spec = session.store.publish_asset(view["spec"]["asset_id"], project, kind="production_spec",
                producer="production", story_id=session.story, correlation_id=session.run_id,
                causation_id=script["event_id"], dependencies=dependencies)
    _checked("spec.story_id", session.story, spec["story_id"])
    _checked("spec.kind", "production_spec", spec["kind"])
    _checked("spec.sha256", digest_json(project), spec["sha256"])
    return spec


@traced("studio.video.plan", kind="tool", capture_inputs=False)
def studio_video_plan(view, previous, *, incomplete_snapshot=...):
    """Calculate the production tools' shot scope against a pinned delivery."""
    from pianchang.studio_retakes import extra_retake_proposal
    from pianchang.video_collab_agent import analyze_impact

    _checked("plan.production_ref", view["production_ref"], ref(previous) if previous else None)
    project = view["project"]
    plan = {"project_id": view["project_id"], "script_ref": view["script_ref"],
            "spec_ref": ref(view["spec"]) if view["spec"]["revision_id"] else None,
            "project_sha256": view["spec"]["sha256"], "production_ref": view["production_ref"],
            "new_shots": [], "retake_shots": [], "reuse_shots": []}
    if view.get("store_path"):
        from pianchang.model_routes import load_routes
        env_file = Path(view["store_path"]).resolve().parents[2] / ".env"
        registry = load_routes(env_file)
        purposes = {"video.reference_to_video" if shot.get("required_refs") else "video.text_to_video"
                    for shot in project["shots"]}
        plan["video_routes"] = {purpose: registry.resolve(purpose).snapshot() for purpose in sorted(purposes)}
    if previous is None:
        script = project.get("script_source", {}).get("script")
        if not script:
            raise ValueError("工程缺少可核验的剧本，请先完成剧本和分镜。")
        _checked("制作规格可用于拍摄", True, validate_compilation(project, script)["ready_for_generation"])
        plan["new_shots"] = [shot["id"] for shot in project["shots"]]
    elif project == previous["content"]["project"]:
        # This exact project already produced a versioned package. Every shot still
        # has the same inputs; the worker verifies the recorded clips before reuse.
        plan["reuse_shots"] = [shot["id"] for shot in project["shots"]]
    else:
        impact = analyze_impact(project, previous["content"]["project"])
        plan["reuse_shots"] = impact["reuse"]
        for sid in impact["regenerate"]:
            destination = "new_shots" if impact["shots"][sid]["base"] is None else "retake_shots"
            plan[destination].append(sid)
    from pianchang.studio_recovery import read_incomplete_production
    incomplete = (read_incomplete_production(view) if view.get("store_path") else None) if incomplete_snapshot is ... else incomplete_snapshot
    if incomplete and not incomplete["active"]:
        ready = set(incomplete["available"])
        plan["new_shots"] = [sid for sid in plan["new_shots"] if sid not in ready]
        plan["retake_shots"] = [sid for sid in plan["retake_shots"] if sid not in ready]
        plan["reuse_shots"] = [s["id"] for s in project["shots"] if s["id"] in ready or s["id"] in plan["reuse_shots"]]
        plan["continuation"] = {"request_id": incomplete["request_id"], "sha256": incomplete["sha256"]}
        unresolved = incomplete["unknown_shots"] + incomplete["failed_shots"]
        if unresolved:
            plan["unresolved_shots"] = unresolved
            plan["blocked_reason"] = "以下已有任务的结果或重拍授权仍需处理，暂不重复生成：" + "、".join(unresolved)
    from pianchang.video_repair import acceptance_policy
    plan["acceptance_loop"] = acceptance_policy([s["id"] for s in project["shots"]])
    from pianchang.video_execution import execution_limits
    budget=len(plan["new_shots"])+len(plan["retake_shots"])+len(plan["acceptance_loop"]["shot_ids"])*plan["acceptance_loop"]["max_repairs_per_shot"]
    plan["execution_limits"]=execution_limits(plan,budget,len(project["shots"]))
    plan["plan_id"] = digest_json(plan)
    # Content failures are handled inside the same production agent loop.
    # Legacy terminal provider failures keep their explicit recovery proposal.
    proposal = extra_retake_proposal(view, previous, plan["reuse_shots"])
    plan["extra_retake"] = proposal if proposal and proposal.get("partial") else None
    return plan


def _approved_plan(view, previous, body):
    plan = studio_video_plan(view, previous)
    _checked("拍摄计划已更新", plan["plan_id"], body["plan_id"])
    if plan.get("blocked_reason"):
        raise ValueError(plan["blocked_reason"])
    proposal = plan.pop("extra_retake")
    if body.get("repair_existing_only"):
        if not plan.get("continuation") or not plan["reuse_shots"] or proposal:
            raise ValueError("当前没有可独立恢复的已有候选，或原生成结果仍需核实。")
        from pianchang.video_repair import acceptance_policy
        plan.update(repair_existing_only=True, deferred_shots=plan["new_shots"] + plan["retake_shots"],
                    new_shots=[], retake_shots=[], base_plan_id=plan["plan_id"],
                    acceptance_loop=acceptance_policy(plan["reuse_shots"]))
        from pianchang.video_execution import execution_limits
        plan["execution_limits"]=execution_limits(plan,len(plan["reuse_shots"])*plan["acceptance_loop"]["max_repairs_per_shot"],len(view["project"]["shots"]))
        plan["plan_id"] = digest_json(plan)
        return plan
    if "proposal_id" not in body:
        if proposal and proposal.get("partial"):
            raise ValueError("上次制作已使用这份计划。请查看并决定是否批准额外重拍。")
        return plan
    _checked("额外重拍计划已更新", (proposal or {}).get("proposal_id"), body["proposal_id"])
    if proposal.get("partial"):
        plan.update(new_shots=[], retake_shots=proposal["shot_ids"], reuse_shots=proposal["reused_shots"],
                    partial=proposal["partial"])
    else:
        retakes = set(plan["retake_shots"]) | set(proposal["shot_ids"])
        plan["retake_shots"] = [s["id"] for s in view["project"]["shots"] if s["id"] in retakes]
        plan["reuse_shots"] = [sid for sid in plan["reuse_shots"] if sid not in retakes]
    plan.update(approval_proposal=proposal, proposal_id=proposal["proposal_id"], base_plan_id=plan["plan_id"])
    from pianchang.video_execution import execution_limits
    plan["execution_limits"]=execution_limits(plan,len(plan["new_shots"])+len(plan["retake_shots"])+len(plan["acceptance_loop"]["shot_ids"])*plan["acceptance_loop"]["max_repairs_per_shot"],len(view["project"]["shots"]))
    plan["plan_id"] = digest_json(plan)
    return plan


def _production_order(session, script, spec, view, budget, shooting_plan=None):
    previous = open_ref(session.store, view["production_ref"]) if view.get("production_ref") else None
    if shooting_plan is not None:
        # The first commission of an imported project publishes its script/spec pins.
        # On refresh the preview then has new pins, but attempts on that very same input
        # (including interrupted or uncertain submissions) still belong to the original order.
        if "proposal_id" not in shooting_plan and "continuation" not in shooting_plan:
            for existing in reversed(session.store.messages(session.run_id, message_type="production.requested")):
                payload = existing["payload"]
                if (payload["script"] == ref(script) and payload["template"] == ref(spec)
                        and payload["previous"] == view["production_ref"]
                        and not (payload.get("shooting_plan") or {}).get("proposal_id")):
                    return existing
        return session.commission(script, spec, previous, budget, shooting_plan=shooting_plan)
    previous_order_id = session.store.get_message(previous["event_id"])["causation_id"] if previous else None
    # HTTP retries can arrive after a delivery changed the latest production reference.
    # Recover the original immutable order before deriving an order from that new parent.
    for message in reversed(session.store.messages(session.run_id, message_type="production.requested")):
        payload = message["payload"]
        if (payload["script"] == ref(script) and payload["template"] == ref(spec)
                and (previous is None or payload["previous"] == ref(previous) or message["message_id"] == previous_order_id)):
            return message
    from pianchang.video_collab_agent import analyze_impact
    planned = analyze_impact(spec["content"], previous["content"]["project"] if previous else None)
    shots = planned["regenerate"]
    event("studio.video_budget.check", {"path": "max_video_generations", "expected": {"minimum": len(shots)},
          "actual": budget, "status": "PASS" if len(shots) <= budget else "FAIL", "shots": shots}, kind="validation")
    if len(shots) > budget:
        raise ValueError(f"本次制作需要生成 {len(shots)} 个镜头（{'、'.join(shots)}），超过授权上限 {budget}。"
                         "请调整上限再委托；尚未提交视频生成。")
    return session.commission(script, spec, previous, budget)


@traced("studio.check_legacy_video_scope", kind="validation", capture_inputs=False)
def check_legacy_video_scope(service, pid, shot_id=None):
    """Direct old entry points cannot see references bound to a separate immutable spec."""
    if any(j.get("kind") == "asset_production" and j.get("project_id") == pid for j in service.list_jobs()):
        raise ValueError("此故事已由资产 Agent 接手。请从「视频制作 Agent」使用已确认的镜头参考版本。")
    if not (service.root / "outputs" / "collaborations" / "assets.sqlite3").is_file():
        return
    from pianchang.studio_assets import studio_project_for_video
    view = studio_project_for_video(service, pid)
    if not view["spec"].get("revision_id"):
        return
    shots = [s["id"] for s in view["project"]["shots"] if s.get("required_refs")
             and (shot_id is None or s["id"] == shot_id)]
    event("studio.legacy_generation.check", {"path": "studio.bound_shots", "expected": [], "actual": shots,
          "status": "FAIL" if shots else "PASS"}, kind="validation")
    if shots:
        raise ValueError("镜头已在制作规格中绑定参考资产。请从制作流程的「委托视频 Agent」发起生成：" + "、".join(shots))


@traced("studio.video_job.resume", kind="task", capture_inputs=False)
def resume_video_job(service, job):
    """Resume the same frozen commission, not a newly computed shooting plan."""
    session = _session(service, job["run_id"], "恢复已授权的视频制作", job["project_id"])
    try:
        order=session.store.get_message(job["message_id"])
        package, delivered = session.worker(job["message_id"],resume=order["deliveries"]["video"]!="pending")
    except ValueError:
        path=session.folder/("video-spec-"+job["message_id"])/"agent/run.json"
        if not path.is_file():raise
        record=read_json(path)
        if record.get("status") not in {"needs_input","budget_exhausted"}:raise
        return {"action_required":True,"task_complete":False,"summary":record["result"]["summary"],
                "loop":record["result"],"diagnostic_path":str(path.parent.parent/"autonomy.json")}
    return {"task_complete":True,"production_ref":ref(package),"message_id":delivered["message_id"],
            "summary":"原制作委托已自动恢复，必过验收通过并交付预览。"}


@traced("studio.video_job", kind="task", capture_inputs=False)
def run_video_job(service, job, body):
    if job.get("_autonomy_resuming"):
        return resume_video_job(service,job)
    from pianchang.studio_assets import studio_project_for_video
    pid = job["project_id"]
    pinned = body.get("spec_ref")
    planned_request = "plan_id" in body
    view = studio_project_for_video(service, pid, pinned["revision_id"] if pinned and not planned_request else None)
    session = _session(service, view.get("run_id") or studio_run_id(pid), view["project"]["title"], pid)
    _checked("project.story_id", session.story, view["story_id"])
    if "script_ref" in body:
        _checked("displayed_script.ref", body["script_ref"], view["script_ref"])
    if pinned is not None:
        _checked("spec.ref", pinned, ref(view["spec"]))
    else:
        _checked("spec.project_sha256", body["project_sha256"], digest_json(view["project"]))
    from pianchang.asset_dispatch import check_reference_approval, check_video_asset_readiness
    from pianchang.studio_assets import studio_asset_catalog
    catalog = studio_asset_catalog(service, pid)
    _checked("reference_catalog.spec", view["spec"], catalog["spec"])
    readiness_project = view["project"]
    if body.get("repair_existing_only"):
        from pianchang.studio_recovery import read_incomplete_production
        snapshot = read_incomplete_production(view)
        ids = set((snapshot or {}).get("available", {}))
        readiness_project = {**view["project"], "shots":[s for s in view["project"]["shots"] if s["id"] in ids]}
    check_video_asset_readiness(service, pid, readiness_project, catalog=catalog)
    unique_pins = {(p["asset_id"], p["revision_id"]):p for s in readiness_project["shots"] for p in s.get("required_refs", [])}
    for pin in unique_pins.values():
        check_reference_approval(service, pid, pin, catalog=catalog)
    shooting_plan = None
    budget = body.get("max_video_generations")
    with session.store.transaction():
        if planned_request:
            # Read the head again while holding the same writer lock as publication. A new
            # spec, delivery or review cannot expand the scope the button displayed.
            locked_view = studio_project_for_video(service, pid)
            for key in ("spec", "script_ref", "production_ref", "pending_script_ref"):
                _checked("plan.current." + key, view[key], locked_view[key])
            previous = open_ref(session.store, view["production_ref"]) if view["production_ref"] else None
            shooting_plan = _approved_plan(locked_view, previous, body)
            if shooting_plan.get("continuation"):
                from pianchang.studio_recovery import prepare_continuation
                prepare_continuation(locked_view, shooting_plan["continuation"])
            budget = len(shooting_plan["new_shots"]) + len(shooting_plan["retake_shots"])
            if shooting_plan.get("acceptance_loop"):
                budget += len(shooting_plan["acceptance_loop"]["shot_ids"]) * shooting_plan["acceptance_loop"]["max_repairs_per_shot"]
            event("studio.video_plan.approved", {"project_id": pid, "plan": shooting_plan,
                  "authorized_video_submissions": budget}, kind="state")
        script = _source_script(session, view)
        latest = session.store.get_asset(script["asset_id"])
        if ref(latest) != ref(script):
            event("studio.script_version.check", {"path": "commission.script", "expected": ref(latest),
                  "actual": ref(script), "status": "FAIL"}, kind="validation")
            raise ValueError(f"剧本 {latest['revision_id']} 已发布，当前制作规格仍对应 {script['revision_id']}。"
                             "请先适配新版制作规格，再提交视频制作委托。")
        spec = _handoff_spec(session, script, view, body)
        order = _production_order(session, script, spec, view, budget, shooting_plan)
    data = {"run_id": session.run_id, "story_id": session.story, "project_id": pid,
            "script_ref": ref(script), "spec_ref": ref(spec), "message_id": order["message_id"]}
    service._update(job, **data,
        _autonomy_enabled=bool((shooting_plan or {}).get("acceptance_loop",{}).get("autonomy_version")),
        _autonomy_body=body,message="制作委托已保存；视频 Agent 将读取冻结剧本和镜头资产")
    status = order["deliveries"]["video"]
    if status == "completed":
        results = session.store.messages(session.run_id, causation_id=order["message_id"], message_type="production.result")
        delivered = next(m for m in results if "production" in m["payload"])
        package = open_ref(session.store, delivered["payload"]["production"])
    elif status == "pending":
        if order["payload"]["max_video_generations"] > budget:
            raise ValueError("待执行委托的授权生成数超过本次上限。")
        try:
            package, delivered = session.worker(order["message_id"])
        except ValueError:
            partial = _partial_decision(service, pid, session, data)
            if partial:
                return partial
            raise
    else:
        if status == "blocked":
            partial = _partial_decision(service, pid, session, data)
            if partial:
                return partial
        raise ValueError("此版本已有制作委托（" + status + "）。请查阅原委托的 Trace，未重新提交。")
    if not package["content"].get("media_accepted"):
        raise ValueError("自动验收尚未全部通过，视频制作未完成，不能交付人工预览。")
    preview = package["content"]["preview"]
    preview_path = service._inside(preview["preview_path"], [service.root / "outputs", service.state_dir])
    _checked("preview.sha256", preview["preview_sha256"], service._hash(preview_path))
    session.write(project_id=pid, status="completed")
    result = {**data, "production_ref": ref(package), "video_trace_id": package["trace_id"],
            "preview_url": service.asset(preview_path,project_id=pid), "media_accepted": package["content"]["media_accepted"],
            "result_message_id": delivered["message_id"], "summary": "视频 Agent 自动修复与验收已通过，已交付人工预览。"}
    if any(r.get("status") == "FAIL" for r in package["content"].get("reports", {}).values()):
        current = studio_project_for_video(service, pid)
        if current["production_ref"] == ref(package) and not current["pending_script_ref"]:
            proposal = studio_video_plan(current, package)["extra_retake"]
            if proposal:
                result.update(action_required=True, extra_retake=proposal,
                              summary="预览已交付，等待确认额外重拍。" + proposal["reason"])
    return result


def _partial_decision(service, pid, session, data):
    """Offer another take only for the specific blocked request with terminal evidence."""
    from pianchang.studio_assets import studio_project_for_video
    from pianchang.studio_retakes import extra_retake_proposal

    messages = session.store.messages(session.run_id, causation_id=data["message_id"], message_type="production.result")
    message = next((msg for msg in messages if msg["payload"].get("partial")), None)
    if not message:
        return None
    current = studio_project_for_video(service, pid)
    if current["pending_script_ref"]:
        return None
    proposal = extra_retake_proposal(current, None, [])
    if not proposal or proposal.get("partial", {}).get("request_id") != data["message_id"]:
        return None
    return {**data, "action_required": True, "extra_retake": proposal, "video_trace_id": message["trace_id"],
            "result_message_id": message["message_id"], "summary": "等待确认额外重拍。" + proposal["reason"]}
