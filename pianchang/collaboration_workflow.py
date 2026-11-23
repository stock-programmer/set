"""Local collaboration worker and a reproducible commission for the existing red-carpet project."""
from __future__ import annotations

import argparse
import json
import re
import uuid
from pathlib import Path

from pianchang.app_service import read_json, write_json
from pianchang.acceptance import file_sha256
from pianchang.bailian_observer import load_observer_config
from pianchang.bailian_text import BailianTextClient, load_text_config
from pianchang.bailian_video import load_config
from pianchang.collab_store import CollabStore
from pianchang.script_collab_agent import publish_script_revision, run_script_agent
from pianchang.story_spec import digest_json, validate_compilation
from trace_system import TraceError, current_context, event, trace, traced


ROOT = Path(__file__).resolve().parents[1]
HOME_DIR = ROOT / "outputs" / "collaborations"
SOURCE = ROOT / "outputs/web/stories/da776ea36ad54d2f8a28259c89d2ab00/project.json"
CHANGE = "仅修改 B003：保留她扯下头纱、抓起手包、撞开房门、披肩滑落、冲入走廊的动作；在门口加入一个可见的新动作：停步一秒，回望镜中的自己，再奔入走廊。保持其他节拍与人物、道具起止状态。"


def ref(asset):
    return {k: asset[k] for k in ("asset_id", "revision_id", "sha256")}


def dep(asset, role, source_id=None):
    value = {"asset_id": asset["asset_id"], "revision_id": asset["revision_id"], "role": role}
    if source_id:
        value["source_id"] = source_id
    return value


def open_ref(store, reference):
    asset = store.get_asset(reference["asset_id"], reference["revision_id"])
    if ref(asset) != {k: reference[k] for k in ("asset_id", "revision_id", "sha256")}:
        raise ValueError("资产版本与委托绑定的内容哈希不符")
    return asset


def causal(message, relation):
    return {**message["trace_context"], "relation": relation}


class VideoAgentStopped(ValueError):
    """The production loop returned a persisted, incomplete result."""


class Collaboration:
    def __init__(self, directory, run_id, *, env_file=None):
        if not re.fullmatch("[a-f0-9]{32}", run_id):
            raise ValueError("无效的协作 ID")
        self.directory = Path(directory).resolve()
        self.folder = self.directory / "runs" / run_id
        self.run_id = run_id
        self.story = "story-" + run_id
        self.store = CollabStore(self.directory / "assets.sqlite3")
        self.env_file = Path(env_file or ROOT / ".env")
        self.index = self.folder / "overview.json"

    def write(self, **values):
        record = read_json(self.index) if self.index.is_file() else {}
        record.update(values)
        write_json(self.index, record)

    def stage(self, name, trace_id, state, revision, summary):
        record = read_json(self.index)
        stages = record.get("stages", [])
        row = next((r for r in stages if r["name"] == name), None)
        if row is None:
            row = {}
            stages.append(row)
        row.update(name=name, trace_id=trace_id, status=state, revision=revision, summary=summary)
        self.write(stages=stages)
        if state != "running":
            event("collaboration.stage", row, kind="state")

    def progress(self, name, revision):
        def notify(data):
            tid = current_context()["trace_id"]
            self.stage(name, tid, "running", revision, str(data.get("summary") or data.get("phase") or name))
        return notify

    @traced("collaboration.script_assignment", kind="task")
    def script_task(self, *, revision, original=None, base=None, result_message=None, goal, beat=None, task_message=None):
        script_id = self.story + "/script"
        inputs = base["content"] if base else original
        name = "剧本修订" if base else "剧本首次发布"
        terms = ["回望"] if beat and goal == CHANGE else []
        links = [causal(result_message, "responds_to_production")] if result_message else []
        if task_message:
            links.append(causal(task_message, "commissioned_revision"))
        if result_message:
            received = self.store.claim("script", message_id=result_message["message_id"])
            if received is None:
                received = self.store.get_message(result_message["message_id"])
                if received["deliveries"].get("script") != "completed":
                    raise ValueError("制作回执已被领取，但处理状态未确认")
            else:
                self.store.ack("script", received["message_id"], "completed", result={"observed": True})
            goal += "\n收到制作回执，按本次改稿要求工作：" + json.dumps(received["payload"], ensure_ascii=False)

        def publish(candidate, report):
            return publish_script_revision(self.store, candidate, report, asset_id=script_id, story_id=self.story,
                base_revision=base["revision_id"] if base else None, source_sha256=base["sha256"] if base else None,
                correlation_id=self.run_id, causation_id=(task_message or result_message or {}).get("message_id"))

        config = load_text_config(self.env_file)
        result = run_script_agent(BailianTextClient(config), self.folder / ("script-" + revision), story_id=self.story,
            goal=goal, publish=publish, mode="revise" if base else "import", script=inputs,
            base_revision=base["revision_id"] if base else None, beat_id=beat, constraints={"required_terms": terms},
            links=links, trace_attributes={"correlation_id": self.run_id}, progress=self.progress(name, revision))
        status = result["loop_result"]["status"]
        self.stage(name, result["trace_id"], status, revision, result["loop_result"]["summary"])
        if not result["loop_result"]["task_complete"]:
            raise ValueError("剧本 Agent 停止：" + result["loop_result"]["summary"])
        asset = open_ref(self.store, result["receipt"])
        if asset["revision_id"] != revision:
            raise ValueError("发布版本与委托不符")
        return asset

    @traced("collaboration.commission", kind="task")
    def commission(self, script, template, prior, video_budget, *, shooting_plan=None):
        cap = template["content"].get("generation_request_limit") if template else None
        if cap is not None and (shooting_plan is None or shooting_plan.get("generation_request_limit") != cap
                                or type(video_budget) is not int or video_budget > cap):
            raise ValueError("分组制作必须确认含总请求上限的拍摄计划。")
        payload = {"script": ref(script), "template": ref(template) if template else None,
                   "previous": ref(prior) if prior else None, "max_video_generations": video_budget,
                   "goal": "自主制作、验收并针对证据修复，所有镜头及衔接必过项通过后交付人工预览。"}
        key = self.run_id + "/commission/" + script["revision_id"]
        if template and template["kind"] == "production_spec":
            key += "/spec/" + digest_json([ref(template), ref(prior) if prior else None])
        if shooting_plan is not None:
            payload["shooting_plan"] = shooting_plan
            key += "/plan/" + shooting_plan["plan_id"]
        message_id=uuid.uuid5(uuid.NAMESPACE_URL,key).hex
        try:existing=self.store.get_message(message_id)
        except KeyError:existing=None
        if existing and existing["payload"]==payload:return existing  # retain legacy frozen payloads verbatim
        from pianchang.video_execution import execution_limits
        payload["execution_limits"]=execution_limits(shooting_plan,video_budget,len(template["content"].get("shots",[])) if template else 0)
        return self.store.send("production.requested", sender="commissioner", recipient="video",
            message_id=message_id,
            payload=payload, story_id=self.story, correlation_id=self.run_id, causation_id=script["event_id"])

    @traced("collaboration.lock_shot_references", kind="tool", capture_inputs=False)
    def lock_shot_references(self, project, script, previous, request):
        prior = previous["content"].get("reference_specs", {}) if previous else {}
        if not prior and not any(s.get("required_refs") for s in project["shots"]):
            return {}
        scope = project.get("reference_story_id", self.story)
        result = {}
        with self.store.transaction():
            for shot in project["shots"]:
                sid = shot["id"]
                pins = shot.get("required_refs", [])
                if not pins and sid not in prior:
                    continue
                for pin in pins:
                    asset = open_ref(self.store, pin)
                    if asset["kind"] != "reference_media" or asset["story_id"] != scope:
                        raise ValueError("参考素材与获准的资产范围不一致：" + sid)
                content = {"shot_id":sid, "script_ref":ref(script), "project_sha256":digest_json(project),
                           "required_refs":pins, "reference_story_id":scope}
                asset_id = self.story + "/shot-requirements/" + sid
                try:
                    existing = self.store.get_asset(asset_id)
                except KeyError:
                    existing = None
                if existing and existing["content"] == content:
                    result[sid] = ref(existing)
                    continue
                locked = self.store.publish_asset(asset_id, content, kind="shot_reference_spec", producer="video",
                    # A failed attempt can have recorded requirements without producing media.
                    # Append to the requirement history; the delivery itself still CAS-checks its production base.
                    story_id=self.story, parent_revision=existing["revision_id"] if existing else None,
                    correlation_id=self.run_id, causation_id=request["message_id"], dependencies=[
                        dep(script, "target_script"), *[dep(pin, "required_reference", sid) for pin in pins]])
                result[sid] = ref(locked)
        return result

    @traced("collaboration.register_delivery", kind="tool")
    def register_delivery(self, production, script, previous, request):
        if not production.get("media_accepted"):
            raise ValueError("自动验收未全部通过，不能发布为完成的视频制作交付。")
        prior = previous["content"] if previous else {}
        bindings, checks = {}, {}
        common = {"producer": "video", "story_id": self.story, "correlation_id": self.run_id,
                  "causation_id": request["message_id"]}
        for sid, shot in production["shots"].items():
            old = prior.get("media_refs", {}).get(sid)
            target = next(s for s in production["project"]["shots"] if s["id"] == sid)
            pins = target.get("required_refs", [])
            submitted_deps = []
            if pins or shot.get("required_refs"):
                from pianchang.reference_usage import verify_reference_exchange
                from pianchang.video_agent import verify_reference_receipt
                job = read_json(shot["manifest_path"])
                receipt = job.get("reference_receipt", {})
                if (verify_reference_receipt(job)["status"] != "PASS" or receipt.get("status") != "ACCEPTED"
                        or receipt.get("required_refs") != pins or receipt != shot.get("reference_receipt")
                        or receipt.get("submitted_refs") != shot.get("submitted_refs")
                        or job.get("task_id") != shot["task_id"] or digest_json(job) != shot["generation_job_sha256"]
                        or verify_reference_exchange(job,receipt,receipt.get("trace_id"))["status"] != "MATCHED_RESPONSE"):
                    raise ValueError("选用素材缺少与当前参考要求一致的真实提交回执：" + sid)
                submitted_deps = [dep(pin, "submitted_reference", sid) for pin in receipt["submitted_refs"]]
                if receipt.get("reference_bundle"):
                    submitted_deps.append(dep(receipt["reference_bundle"],"reference_input_bundle",sid))
                    submitted_deps.extend(dep(pin,"derived_reference",sid) for pin in pins
                        if not any(all(pin.get(k)==sent.get(k) for k in ("asset_id","revision_id","sha256","media_sha256")) for sent in receipt["submitted_refs"]))
            carried = shot.get("partial_source")
            if carried and carried != request["payload"].get("shooting_plan", {}).get("partial"):
                raise ValueError("部分制作素材来源与已批准的计划不符。")
            continuation = shot.get("continuation_source")
            if continuation and continuation != request["payload"].get("shooting_plan", {}).get("continuation"):
                raise ValueError("恢复素材来源与已批准的计划不符。")
            if shot["operation"] == "reused" and (not (carried or continuation) or
                    (old and open_ref(self.store, old)["content"]["media_sha256"] == shot["media_sha256"])):
                asset = open_ref(self.store, old)
                if asset["content"]["media_sha256"] != shot["media_sha256"]:
                    raise ValueError("复用素材的哈希与原始版本不符")
            else:
                source_ids = production["impact"]["shots"][sid]["target"]["source_ids"]
                asset = self.store.publish_asset(self.story + "/media/" + sid, shot, kind="shot_media",
                    parent_revision=old["revision_id"] if old else None,
                    dependencies=[*[dep(script, "script_source", source) for source in source_ids], *submitted_deps], **common)
            bindings[sid] = ref(asset)
            old_check = prior.get("check_refs", {}).get(sid)
            evidence = self.store.publish_asset(self.story + "/check/" + sid, production["reports"][sid],
                kind="shot_check", parent_revision=old_check["revision_id"] if old_check else None,
                dependencies=[dep(script, "target_script"), dep(asset, "examined_media")], **common)
            checks[sid] = ref(evidence)
        package = {**production, "media_refs": bindings, "check_refs": checks}
        template = request.get("payload", {}).get("template")
        if template and open_ref(self.store, template)["kind"] == "production_spec":
            package["spec_ref"] = ref(template)
        template_dep = [dep(template, "production_input")] if template else []
        return self.store.publish_asset(self.story + "/production", package, kind="production",
            parent_revision=previous["revision_id"] if previous else None,
            dependencies=[dep(script, "target_script"), *template_dep, *[dep(a, "reference_spec") for a in production.get("reference_specs", {}).values()],
                          *[dep(a, "selected_media") for a in bindings.values()],
                          *[dep(a, "verification") for a in checks.values()]], **common)

    @traced("collaboration.load_partial", kind="validation", capture_inputs=False, capture_output=False)
    def _load_partial(self, pointer, request):
        if not pointer:
            return None
        original = self.store.get_message(pointer["request_id"])
        if ((original["story_id"], original["correlation_id"], original["message_type"]) !=
                (self.story, self.run_id, "production.requested") or
                original["deliveries"].get("video") != "blocked" or
                any(original["payload"].get(key) != request["payload"].get(key) for key in ("script", "template", "previous"))):
            raise ValueError("待重拍的部分制作记录与本次委托不匹配。")
        checkpoint = read_json(self.folder / ("video-spec-" + original["message_id"]) / "partial.json")
        actual_sha = digest_json(checkpoint)
        event("collaboration.partial_digest", {"path":"partial.sha256", "expected":pointer["sha256"],
              "actual":actual_sha, "status":"PASS" if actual_sha == pointer["sha256"] else "FAIL"}, kind="validation")
        if (checkpoint["request_id"] != original["message_id"] or actual_sha != pointer["sha256"]
                or checkpoint["shooting_plan"] != original["payload"]["shooting_plan"]
                or checkpoint["script_ref"] != request["payload"]["script"]):
            raise ValueError("部分制作记录的内容指纹或来源委托不符。")
        from pianchang.video_collab_agent import _validated_media
        for sid, clip in checkpoint["shots"].items():
            if clip["shot_id"] != sid or clip["script_ref"] != checkpoint["script_ref"]:
                raise ValueError("已完成镜头与此前制作委托的版本不符：" + sid)
            _validated_media(clip, require_origin=True)
        for sid, failure in checkpoint["failed_shots"].items():
            manifest = read_json(failure["manifest_path"])
            if (failure["shot_id"] != sid or failure["status"] not in {"FAILED", "CANCELED", "CANCELLED"}
                    or manifest["status"] != failure["status"] or manifest["task_id"] != failure["task_id"]
                    or digest_json(manifest) != failure["manifest_sha256"]):
                raise ValueError("先前云端任务的最终失败状态无法核实：" + sid)
        return checkpoint

    def _decision_not_sent(self, request_id, record):
        from tools.workbench_loop_observer import _trace
        origin = record.get("trace_context") or {}
        if (record.get("trace_incomplete") or not origin.get("trace_id") or not origin.get("span_id")
                or (record.get("process_cleanup") or {}).get("confirmed_exit") is not True):
            return False
        evidence = _trace(self.env_file.parent, origin["trace_id"], origin["span_id"])
        proof = evidence.get("planner_not_sent") or {}
        if (not evidence["trace_failed"] and not evidence["outstanding"]
                and proof.get("trace_id") == origin["trace_id"] and proof.get("run_span_id") == origin["span_id"]
                and proof.get("commission_id") == request_id and proof.get("run_id") == self.run_id):
            return proof
        return None

    def worker(self, request_id, *, resume=False, explicit_continuation=False):
        """Consume one addressed, immutable request. An already-claimed task is never replayed."""
        from pianchang.collab_negotiation import VideoNegotiation
        from pianchang.video_collab_agent import run_video_agent
        incoming = self.store.get_message(request_id)
        if (incoming["correlation_id"], incoming["story_id"], incoming["message_type"], incoming["recipient"]) != (
                self.run_id, self.story, "production.requested", "video"):
            raise ValueError("消息不属于此协作的制作委托")
        published = self.store.get_message(incoming["causation_id"])
        args = incoming["payload"]
        if (published["message_type"] != "script.revision_published"
                or ref(published["payload"]) != ref(args["script"])):
            raise ValueError("委托版本与发布事件不符")
        rev = args["script"]["revision_id"]
        name = "视频首次制作" if args["previous"] is None else "视频改稿更新"
        links = [causal(incoming, "commissioned_by"), causal(published, "consumes_publication")]
        if args.get("shooting_plan", {}).get("continuation"):
            origin = self.store.get_message(args["shooting_plan"]["continuation"]["request_id"])
            links.append(causal(origin, "continues"))
        with trace("agent.task.video", kind="agent", inputs={"request": incoming, "publication": published},
                   links=links, attributes={"correlation_id": self.run_id, "revision": rev, "agent": "video"}) as root:
            request = self.store.claim("video", message_id=request_id)
            if request is None:
                from pianchang.video_autonomy import (_can_resume_unprepared_pi_order, can_resume_planned_pi_order,
                                                      can_prepare_reused_continuation, can_resume_paid_pi_order, recoverable_order)
                status=incoming["deliveries"].get("video")
                work = self.folder / ("video-spec-" + request_id)
                recoverable_block = status == "blocked" and recoverable_order(work)
                resume_reason = "existing_operation_reconciliation"
                if resume and explicit_continuation and status in {"blocked", "accepted", "received", "unknown"}:
                    run_path = work / "agent/run.json"
                    if run_path.is_file():
                        record = read_json(run_path)
                        if (record.get("status") == "needs_input"
                                and (record.get("result") or {}).get("reason") == "pi_stopped"):
                            if record.get("video_generations") and not record["result"].get("completion_check"):
                                proof = self._decision_not_sent(request_id, record)
                                if not proof or not can_resume_paid_pi_order(work, args["shooting_plan"], record, proof,
                                        request_id=request_id, run_id=self.run_id, script=args["script"],
                                        output=load_config(self.env_file, allow_missing=True).output_dir,
                                        limits=args.get("execution_limits")):
                                    raise ValueError("已付款镜头、会话或冻结额度无法核对，须核实原任务")
                                recoverable_block = True
                                resume_reason = "paid_pi_decision_not_sent"
                            elif status == "blocked" and not recoverable_block:
                                if can_prepare_reused_continuation(work, args.get("shooting_plan"), record):
                                    recoverable_block = True
                                    resume_reason = "explicit_reused_continuation_preparation"
                                elif _can_resume_unprepared_pi_order(work, args.get("shooting_plan"), record):
                                    recoverable_block = True
                                    resume_reason = "explicit_unprepared_pi_decision"
                                elif (can_resume_planned_pi_order(work, args.get("shooting_plan"), record)
                                      and self._decision_not_sent(request_id, record)):
                                    recoverable_block = True
                                    resume_reason = "planned_pi_decision_not_sent"
                if (not resume or (status not in {"accepted","unknown","received"} and not recoverable_block)
                        or not args.get("shooting_plan",{}).get("acceptance_loop",{}).get("autonomy_version")):
                    raise ValueError("委托已被领取或处理，须核实原任务")
                request = incoming
                event("collaboration.resume_existing_order", {"request_id":request_id,
                      "previous_status":incoming["deliveries"].get("video"),"budget_reset":False,
                      "reason":resume_reason},kind="state")
            try:
                notice = self.store.claim("video", message_id=published["message_id"])
                if notice:
                    self.store.ack("video", notice["message_id"], "completed", result={"observed_version": rev})
                script = open_ref(self.store, args["script"])
                previous = open_ref(self.store, args["previous"]) if args["previous"] else None
                spec = open_ref(self.store, args["template"]) if args["template"] else None
                spec_update = spec is not None and spec["kind"] == "production_spec"
                if spec_update and (spec["story_id"] != self.story or
                                    not validate_compilation(spec["content"], script["content"])["ready_for_generation"]):
                    raise ValueError("参考制作规格与委托的故事或剧本版本不符")
                allowed_base = {script["parent_revision"]}
                if spec_update:
                    allowed_base.add(script["revision_id"])
                    name = "视频参考更新 " + spec["revision_id"]
                if previous and (previous["story_id"] != self.story or previous["kind"] != "production"
                        or previous["content"]["script_ref"]["asset_id"] != script["asset_id"]
                        or previous["content"]["script_ref"]["revision_id"] not in allowed_base):
                    raise ValueError("基础制作版本无法绑定本次改稿的父版本")
                if spec_update and previous and ref(previous) != ref(self.store.get_asset(previous["asset_id"])):
                    raise ValueError("参考更新的基础制作版本已经过期。")
                template = spec["content"] if spec else None
                if not resume or request["deliveries"]["video"]=="received":
                    self.store.ack("video", request_id, "accepted", result={"script": ref(script), "trace_id": root.trace_id})
                self.stage(name, root.trace_id, "running", rev, "已接受制作委托")
                work_dir = "video-spec-" + request_id if spec_update else "video-" + rev
                plan = args.get("shooting_plan")
                partial = self._load_partial((plan or {}).get("partial"), request)
                if (plan or {}).get("continuation"):
                    from pianchang.studio_recovery import load_continuation
                    partial = load_continuation(self.folder.parents[3], self.run_id, plan["continuation"], args)
                video = run_video_agent(script=script["content"], script_ref=ref(script), directory=self.folder / work_dir,
                    text_config=load_text_config(self.env_file), video_config=load_config(self.env_file),
                    vision_config=load_observer_config(self.env_file), template_project=template,
                    reference_store=self.store, story_id=self.story,
                    register_specs=lambda project: self.lock_shot_references(project, script, previous, request),
                    previous=previous["content"] if previous else None, max_video_generations=args["max_video_generations"],
                    max_steps=(args.get("execution_limits") or {}).get("max_model_calls"),
                    shooting_plan=plan, partial=partial,
                    negotiation=VideoNegotiation(self.store, script_ref=ref(script), story_id=self.story,
                        production_message_id=request_id, evidence_refs=[ref(previous)] if previous else [],recovering=resume),
                    inbound_message={"message_id": request_id, "type": request["message_type"], "goal": args["goal"],
                        "publication_id": published["message_id"], "change": published["payload"],
                        "script": ref(script), "max_video_generations": args["max_video_generations"]},
                    progress=self.progress(name, rev),resume=resume)
                if not video["production"]:
                    if (plan or {}).get("acceptance_loop",{}).get("autonomy_version"):
                        stopped={"status":"needs_input","loop":video["loop"],
                                 "diagnostic_path":str(self.folder/work_dir/"autonomy.json"),"task_complete":False}
                        if request["deliveries"]["video"]=="blocked":
                            event("collaboration.recovery_still_blocked",stopped,kind="state")
                        else:self.store.ack("video",request_id,"blocked",recovery=resume,result=stopped)
                    if video.get("partial"):
                        checkpoint = video["partial"]
                        payload = {"status": "retake_approval_required", "script": args["script"],
                                   "partial": {"request_id": request_id, "sha256": digest_json(checkpoint)},
                                   "failed_shots": checkpoint["failed_shots"], "reason": video["loop"]["summary"]}
                        with self.store.transaction():
                            self.store.send("production.result", sender="video", recipient="script", category="result",
                                story_id=self.story, payload=payload, correlation_id=self.run_id, causation_id=request_id)
                            self.store.ack("video", request_id, "blocked", result=payload)
                    raise VideoAgentStopped("视频 Agent 停止：" + video["loop"]["summary"])
                with self.store.transaction():
                    package = self.register_delivery(video["production"], script, previous, request)
                    production = package["content"]
                    payload = {"production": ref(package), "script": ref(script), "media_accepted": production["media_accepted"],
                        "regenerated": production["impact"]["regenerate"], "reused": production["impact"]["reuse"]}
                    result = self.store.send("production.result", sender="video", recipient="script", category="result",
                        story_id=self.story, payload=payload, correlation_id=self.run_id, causation_id=request_id)
                    self.store.ack("video", request_id, "completed", recovery=resume,result={"message_id": result["message_id"], **payload})
                items = read_json(self.index).get("productions", [])
                items.append({"revision": rev, "affected": payload["regenerated"], "reused": payload["reused"],
                    "media_accepted": production["media_accepted"], "preview_path": production["preview"]["preview_path"],
                    "preview_sha256": production["preview"]["preview_sha256"], "recheck_edges": production["impact"]["recheck_edges"],
                    "unresolved": {sid: row["unresolved"] for sid, row in production["reports"].items()}})
                self.write(productions=items)
                self.stage(name, root.trace_id, "completed", rev, video["loop"]["summary"])
                root.set_output(payload)
                return package, result
            except TraceError:
                raise
            except Exception as exc:
                if self.store.get_message(request_id)["deliveries"]["video"] not in {"completed", "blocked","unknown"}:
                    with self.store.transaction():
                        self.store.ack("video", request_id, "unknown", result={"reason": str(exc)})
                        self.store.send("production.result", sender="video", recipient="script", category="result",
                            story_id=self.story, payload={"status":"blocked","script":args["script"],"reason":str(exc)},
                            correlation_id=self.run_id, causation_id=request_id)
                self.stage(name, root.trace_id, "blocked", rev, str(exc))
                raise

    def run(self, project, change=CHANGE, beat="B003"):
        script = project["script_source"]["script"]
        if not validate_compilation(project, script)["ready_for_generation"]:
            raise ValueError("来源工程未通过制作规格检查")
        self.folder.mkdir(parents=True, exist_ok=False)
        with trace("collaboration.commission.lifecycle", kind="task", inputs={"source_sha256":digest_json(project),
                "change":change,"beat":beat,"order":"制作首版，再依据源节拍改稿更新"},
                attributes={"correlation_id":self.run_id,"title":script["title"]}) as session:
            self.write(id=self.run_id, title=script["title"], trace_id=session.trace_id, status="running",
                       requested_change=change, target_beat=beat,
                       stages=[], productions=[])
            try:
                self.store.subscribe("video", "script.revision_published", self.story)
                template = self.store.publish_asset(self.story + "/source-project", project, kind="input_project",
                    producer="commissioner", story_id=self.story, correlation_id=self.run_id)
                first = self.script_task(revision="r1", original=script, goal="读取并核验此冻结剧本，审查来源和结构，通过后发布首版供独立制作。")
                order = self.commission(first, template, None, len(project["shots"]))
                previous, result = self.worker(order["message_id"])
                second = self.script_task(revision="r2", base=first, result_message=result, goal=change, beat=beat)
                order2 = self.commission(second, None, previous, 1)
                latest, delivered = self.worker(order2["message_id"])
                with trace("script.observe_production_result", kind="task", links=[causal(delivered, "observes_result")]) as observation:
                    received = self.store.claim("script", message_id=delivered["message_id"])
                    self.store.ack("script", received["message_id"], "completed", result={"observed": ref(latest)})
                    observation.set_output(received["payload"])
                    self.stage("剧本端接收最终回执", observation.trace_id, "completed", "r2", "已接收制作结果与验收结论")
                self.write(status="completed")
                session.set_output({"production":ref(latest),"stages":read_json(self.index)["stages"]})
            except TraceError:
                raise
            except Exception as exc:
                self.write(status="blocked",error=str(exc))
                session.set_output({"stages":read_json(self.index)["stages"],"reason":str(exc)})
                raise


@traced("collaboration.list", kind="tool", capture_output=False)
def list_runs(service):
    directory = service.root / "outputs" / "collaborations"
    if not (directory / "assets.sqlite3").is_file():
        return {"runs":[]}
    store = CollabStore(directory / "assets.sqlite3")
    runs = []
    for path in sorted(directory.glob("runs/*/overview.json"), key=lambda p:p.stat().st_mtime, reverse=True):
        record = read_json(path)
        messages = store.messages(record["id"])
        record["productions"] = []
        from pianchang.preview_access import preview_status
        pid=record.get("project_id")
        gate=preview_status(service,pid) if pid else {"ready":False}
        for m in messages:
            if m["message_type"] != "production.result" or "production" not in m["payload"]:
                continue
            package = open_ref(store, m["payload"]["production"])["content"]
            impact = package["impact"]
            preview = Path(package["preview"]["preview_path"])
            intact = preview.is_file() and file_sha256(preview) == package["preview"]["preview_sha256"]
            record["productions"].append({
                "revision": package["script_ref"]["revision_id"], "affected": impact["regenerate"],
                "production_revision": m["payload"]["production"]["revision_id"],
                "reused": impact["reuse"], "changed_sources": impact["changed_sources"],
                "reasons": {sid: row["changed_components"] for sid, row in impact["shots"].items()},
                "recheck_edges": impact["recheck_edges"], "media_accepted": package["media_accepted"],
                "unresolved": {sid: row["unresolved"] for sid, row in package["reports"].items()},
                "preview_url": service.asset(preview,project_id=pid) if intact and gate["ready"] and gate.get("production_ref")==m["payload"]["production"] else None, "preview_verified": intact,
            })
        record["messages"] = [{"type":m["message_type"],"message_id":m["message_id"],"sender":m["sender"],
            "recipient":m["recipient"],"trace_id":m["trace_id"],"causation_id":m["causation_id"],
            "payload":m["payload"],"receipts":[{"agent":r["agent"],"status":r["status"],
            "trace_id":r["trace_context"]["trace_id"]} for r in store.receipts(m["message_id"])]}
            for m in messages if m["message_type"] != "asset.revision_published"]
        runs.append(record)
    return {"runs":runs}


@traced("cli.collaboration", kind="cli")
def main(argv=None):
    parser = argparse.ArgumentParser(description="制作首版、修改一个节拍、按依赖更新视频并记录双方 Trace。")
    parser.add_argument("--project", type=Path, default=SOURCE)
    parser.add_argument("--directory", type=Path, default=HOME_DIR)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--beat", default="B003")
    parser.add_argument("--change", default=CHANGE)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--consume", help="只领取指定的已授权视频委托，需给定现有 --run-id")
    modes.add_argument("--reference-spec", help="在同一剧本版本上，根据已发布的制作规格更新参考")
    parser.add_argument("--spec-revision", help="固定的制作规格版本，如 r2")
    parser.add_argument("--base-production", help="被修改的制作版本，如 r2")
    parser.add_argument("--max-video-generations", type=int, help="参考更新任务可提交的新候选次数")
    args = parser.parse_args(argv)
    if args.reference_spec and (not args.run_id or not args.spec_revision or not args.base_production
                                or args.max_video_generations is None or args.max_video_generations < 0):
        parser.error("更新参考需要 --run-id、--spec-revision、--base-production 和非负的 --max-video-generations")
    session = Collaboration(args.directory, args.run_id or uuid.uuid4().hex)
    print("Collaboration", session.run_id, flush=True)
    if args.consume:
        session.worker(args.consume)
    elif args.reference_spec:
        spec = session.store.get_asset(args.reference_spec, args.spec_revision)
        prior = session.store.get_asset(session.story + "/production", args.base_production)
        script = open_ref(session.store, prior["content"]["script_ref"])
        if spec["story_id"] != session.story or spec["kind"] != "production_spec":
            raise ValueError("需要本故事的 production_spec 资产。")
        order = session.commission(script, spec, prior, args.max_video_generations)
        if session.store.get_message(order["message_id"])["deliveries"]["video"] == "pending":
            session.worker(order["message_id"])
        else:
            print("委托已有执行记录：" + order["message_id"])
    else:
        session.run(read_json(args.project), args.change, args.beat)
    print(session.index, flush=True)


if __name__ == "__main__":
    main()
