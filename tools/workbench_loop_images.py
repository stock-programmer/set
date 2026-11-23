"""Resolve an image route before requesting input and resume the original asset scope."""
import re
import time

from pianchang.app_service import read_json
from tools.workbench_loop_ark import apply_ark_image_route, inspect_ark_image_route
from tools.workbench_loop_input import human_request
from trace_system import TraceError


def _settings_stamp(root):
    values = []
    for path in (root / ".env", root / "config/models.json"):
        try:
            values.append(path.stat().st_mtime_ns)
        except FileNotFoundError:
            values.append(None)
    return values


def _question(job, flow, reason, basis):
    confirm = {"name": "confirm", "type": "confirm", "label": "我已完成上述操作，重新核对并继续", "required": False}
    endpoint = {"name": "endpoint_id", "type": "text", "label": "可用于生成参考图的方舟接入点 ID",
                "hint": "如果已有可用接入点，请填写 ep- 开头的 ID。", "required": False}
    fields = [endpoint, confirm]
    if reason in {"missing_api_key", "invalid_api_key", "catalog_permission_denied"}:
        detail = "方舟当前的生图连接缺少可用凭据或读取权限。请在本机更新该连接使用的密钥或权限，然后确认。"
        fields = [confirm]
    elif reason in {"unsupported_image_connection", "invalid_image_configuration", "model_alias_conflict"}:
        detail = "当前参考图连接无法自动修正。请在本机的 config/models.json 中核对 reference.image 及其连接后确认。"
        fields = [confirm]
    elif reason == "provider_access":
        detail = "方舟已拒绝本次可用路线。请为生图账号开通模型，或提供同账号能生成参考图的接入点。"
    else:
        detail = "已检查模型目录和本机可查询的接入点，仍缺少可验证的生图路线。请提供可用接入点，或在方舟配置好权限后确认。"
    return human_request(job, "reference_image_access", [basis, reason, flow.get("answer_id"), flow["denied"]],
                         "需要方舟生图权限或可用接入点", detail + " 收到答复后，旁路会继续原批次的缺图。",
                         fields=fields, submit_label="提交并继续补图")


def _answer(watcher, job, request):
    if not request:
        return None
    path = watcher.folder / "answers" / (request["id"] + ".json")
    if path.is_file():
        reply = read_json(path)
        if (reply.get("job_id"), reply.get("project_id"), reply.get("request_id")) == (
                job["id"], job["project_id"], request["id"]):
            return reply
    return None


def _asset_rows(watcher, job, targets):
    state = read_json(watcher.root / "outputs/web/state.json")
    rows = state.get("asset_work", {}).get(job["project_id"], {}).get("items", {})
    return [row for row in rows.values() if [row.get("subject_id"), row.get("purpose")] in targets]


def advance_images(watcher, record, job, jobs, dependency):
    """Only the failed asset order owns recovery. Dependent video jobs stay observers."""
    if job["kind"] not in {"asset_production", "asset_regenerate"} or job.get("_image_recovery_from"):
        return False
    flow = dict(record.get("image_flow") or {})
    intervention = (dependency or {}).get("human_intervention") or {}
    if not flow:
        items = [i for i in intervention.get("items", []) if i.get("job_id") == job["id"] and i.get("model_id")]
        if intervention.get("kind") != "reference_image_model_access" or not items:
            return False
        denied = {i["model_id"]: i.get("provider_code", intervention.get("provider_code")) for i in items}
        flow = {"targets": [[i["subject_id"], i["purpose"]] for i in items], "original_denied": denied,
                "denied": denied.copy(), "attempts": []}
    if flow.get("terminal") and record.get("reason") != "image_recovery_reconciliation":
        return True

    def publish(status, reason, message, *, request=None, **changes):
        flow.update(changes)
        extra = {}
        if status == "succeeded":
            from tools.workbench_loop_dependency import recheck_dependency
            extra["dependency"] = recheck_dependency(watcher.root, job)
        watcher.update(record, status=status, reason=reason, progress_message=message,
                       human_request=request, image_flow=dict(flow), **extra)

    total = len(flow["targets"])
    if job.get("cancel_requested") or job.get("trace_incomplete"):
        publish("blocked", "original_order_stopped", "原制作任务已停止，补图同步停止。", terminal=True)
        return True
    child_id = flow.get("child_id")
    if child_id:
        child = jobs.get(child_id)
        if not child:
            # A submit receipt can arrive one tick before the updated workbench snapshot.
            return True
        if child.get("_image_recovery_from") != job["id"] or child.get("project_id") != job["project_id"]:
            publish("unknown", "image_recovery_receipt_unknown", "补图工单回执无法与原任务匹配，正在保留现场。", terminal=True)
            return True
        if child["status"] in {"queued", "running"}:
            match = re.match(r"(?:恢复参考图|复核参考图|资产)\s+(\d+/\d+)", child.get("message") or "")
            progress = " · " + match[1] if match else ""
            detail = ("正在用已有图片重新核验主体和对照关系" if flow.get("after_reinspect")
                      else f"正在用 {flow['model_id']} 生成并核验缺少的参考图")
            publish("handling", "image_recovery_running", detail+progress+"。")
            return True
        receipt = (child.get("result") or {}).get("image_recovery") or {}
        if child["status"] == "interrupted" and not child.get("trace_incomplete") and not child.get("cancel_requested"):
            # The asset worker rechecks original submissions, charge evidence and untouched targets.
            publish("handling", "image_recovery_reconcile", "正在核对中断前的提交回执，并继续可核实的剩余项。",
                    after_interrupted=child_id, after_review=None, after_reinspect=None, after_not_sent=None,
                    after_model_rejection=None, child_id=None)
            return True
        if receipt.get("source_job_id") != job["id"] or receipt.get("model_id") != flow["model_id"]:
            publish("unknown", "image_recovery_receipt_unknown", "补图结果缺少可核对的原任务回执，已保留任务记录。", terminal=True)
            return True
        if receipt["status"] == "model_rejected":
            denied = {**flow["denied"], flow["model_id"]: receipt.get("provider_code") or "InvalidEndpointOrModel.NotFound"}
            publish("handling", "image_route_discovery", "已取得模型拒绝回执，正在核对其他可用的方舟路线。",
                    denied=denied, child_id=None, pending_model=None, supplied_model=None,
                    after_interrupted=None, after_not_sent=None, after_review=None, after_reinspect=None,
                    after_model_rejection=child_id if flow.get("after_authorized_review") else None)
            return True
        assets = _asset_rows(watcher, job, flow["targets"])
        count = sum(bool(row.get("selected_pin")) for row in assets)
        if count == total:
            publish("succeeded", "image_recovery_complete", f"{total} 项缺图已经生成、通过核验并选用。", terminal=True)
        elif receipt["status"] == "completed":
            active_request = record.get("human_request") or {}
            answer = _answer(watcher, job, active_request) if active_request.get("revision_items") else None
            review_check = ("asset_reviews", child_id)
            if (not answer and record.get("reason") == "image_recovery_review"
                    and time.monotonic() < watcher.next_check.get(review_check, 0)):
                return True
            from pianchang.image_recovery import recoverable_image_reviews, reinspectable_image_reviews
            unresolved = {row.get("job_id") for row in assets if not row.get("selected_pin")}
            owners = [jobs[jid] for jid in flow["attempts"] if jid in unresolved and jid in jobs
                      and jobs[jid].get("_image_recovery_from") == job["id"]]
            for owner in owners:
                if reinspectable_image_reviews(watcher.root, job, owner):
                    publish("handling","image_review_reinspect","正在按正确的主体和对照关系重新审看已有图片。",
                            after_reinspect=owner["id"], after_review=None, after_not_sent=None, after_authorized_review=None,
                            after_interrupted=None, child_id=None, next_check=0)
                    return True
            for owner in owners:
                if recoverable_image_reviews(watcher.root, job, owner):
                    publish("handling", "image_recovery_quality", "正在依据本次自检报告和原委托剩余次数修订候选。",
                            after_review=owner["id"], after_reinspect=None, after_not_sent=None, after_authorized_review=None,
                            after_interrupted=None, child_id=None, next_check=0)
                    return True
            review = []
            for row in assets:
                pin, report = row.get("candidate") or {}, row.get("review") or {}
                if (row.get("job_id") in flow["attempts"] and row.get("status") == "needs_attention" and not row.get("selected_pin")
                        and pin.get("revision_id") and report.get("review_ref") and not report.get("observation_error")
                        and report.get("status") in {"FAIL", "UNKNOWN"} and report.get("checks")):
                    review.append({"subject_id":row["subject_id"], "purpose":row["purpose"], "asset_id":pin["asset_id"],
                                   "revision_id":pin["revision_id"], "review_ref":report["review_ref"],
                                   "candidate_job_id":row["job_id"]})
            if review:
                from pianchang.image_recovery import authorizable_image_reviews
                eligible = [item for owner in owners for item in authorizable_image_reviews(watcher.root, job, owner)
                            if item in review]
                fields = []
                message = f"本批已有 {count}/{total} 项选用，{len(review)} 项可查看候选图和报告。"
                if eligible:
                    maximum = len(eligible)
                    message += f"其中 {maximum} 项仍有可修订的缺陷，原额度已用完。追加授权后，每项最多再生成 1 张并自检。本次最多生成 {maximum} 张，生成和自检可能计费。"
                    fields.append({"name":"authorize_revision","type":"confirm","required":True,
                                   "label":f"我授权上述 {maximum} 项各再修订一次并自检"})
                title = "剩余参考图的修订需要追加授权" if eligible else "部分参考图需要审看"
                ask = human_request(job, "review_asset_candidates", [child_id,review,eligible], title, message,
                                    fields=fields, submit_label="授权并修订本批候选" if eligible else "查看待审参考图")
                ask.update(candidate_job_id=child_id,items=review,revision_items=eligible,navigation_label="查看候选图与报告")
                if (eligible and answer and answer.get("request") == ask
                        and answer["answers"].get("authorize_revision") is True):
                    publish("handling","image_revision_authorized","已收到本批追加授权，正在复核待修资产的版本和额度。",
                            after_authorized_review=ask["id"], after_reinspect=None, after_review=None,
                            after_interrupted=None, after_not_sent=None, after_model_rejection=None, child_id=None, next_check=0)
                    return True
                watcher.next_check[review_check] = time.monotonic()+60
                publish("needs_input","image_recovery_review",ask["message"], request=ask)
            else:
                watcher.next_check[review_check] = time.monotonic()+60
                publish("blocked", "image_recovery_review", f"本批补图已结束，{count}/{total} 项已选用。其余项的生成或核验结果见补图工单。")
        elif receipt["status"] in {"not_sent","stopped_unknown"}:
            if time.time() < flow.get("next_check", 0):
                return True
            from pianchang.image_recovery import unsent_image_request
            from tools.workbench_loop_ark import probe_image_transport
            proof = unsent_image_request(watcher.root, child)
            if not proof:
                publish("unknown", "image_recovery_reconciliation", "正在核对原请求的发送和受理记录。已有素材与工单均已保留。",
                        next_check=time.time()+60, terminal=False)
                return True
            failures = flow.get("not_sent_count", 0)
            delay = min(900, 15 * 2**min(failures, 6))
            publish("handling", "image_route_probe", "已核实上次请求在发出前中断，正在用同一代理和接入地址检查连接。",
                    terminal=False)
            probe = probe_image_transport(watcher.root, proof)
            if probe["status"] == "reachable":
                publish("handling", "image_recovery_reconcile", "已核实上次生图请求未发送，网络已恢复，正在继续原批次。",
                        child_id=None, after_not_sent=child_id, after_interrupted=None, after_review=None, after_reinspect=None, after_model_rejection=None,
                        not_sent_count=failures+1, next_check=time.time()+(delay if failures else 0))
            else:
                publish("handling", "image_route_unavailable", "原请求未发送，同一线路暂不可达，旁路将自动复查。",
                        next_check=time.time()+delay)
        else:
            publish("blocked", "image_scope_changed", "补图时制作依据已变化，请查看当前版本的资产任务。", terminal=True)
        return True

    count = sum(bool(row.get("selected_pin")) for row in _asset_rows(watcher, job, flow["targets"]))
    if count == total:
        publish("succeeded", "image_recovery_complete", f"{total} 项参考图均已选用。", terminal=True)
        return True

    request = record.get("human_request")
    stamp = _settings_stamp(watcher.root)
    if request:
        reply = _answer(watcher, job, request)
        if reply:
            flow.update(answer_id=request["id"], supplied_model=reply["answers"].get("endpoint_id"),
                        denied={} if reply["answers"].get("confirm") else flow["denied"],
                        pending_model=None)
            publish("handling", "feedback_answer_received", "已收到补充信息，正在重新核对生图路线。")
        elif flow.get("settings_stamp") == stamp:
            return True
    if time.time() < flow.get("next_check", 0):
        return True
    if not flow.get("pending_model"):
        publish("handling", "image_route_discovery", "正在查询方舟生图模型、当前接入点和上次的拒绝回执。")
        proof = inspect_ark_image_route(watcher.root, flow["denied"], supplied_model=flow.get("supplied_model"))
        decision = apply_ark_image_route(watcher.root, proof) if proof["status"] == "candidate" else proof
        if decision["status"] == "retry_later":
            publish("handling", decision["reason"], "方舟目录或本机配置暂不可读，旁路将自动重查。",
                    next_check=time.time()+60)
            return True
        if decision["status"] != "ready":
            reason = decision["reason"]
            ask = _question(job, flow, reason, [proof.get("config_digest"), flow["targets"]])
            publish("needs_input", reason, ask["message"], request=ask, settings_stamp=stamp)
            return True
        model = decision["model_id"]
        publish("handling", "image_route_ready", f"已核对参考图路线 {model}，正在恢复原工单中缺少的图片。",
                pending_model=model, model_id=model, next_check=0, discovery_trace_id=proof.get("trace_id"))
    from tools.workbench_loop_voice import ImageRecoveryRejected, request_image_recovery
    options = {k: flow[k] for k in ("answer_id", "after_interrupted", "after_not_sent", "after_review", "after_reinspect") if flow.get(k)}
    if flow.get("after_authorized_review"):
        options = {"after_authorized_review":flow["after_authorized_review"],
                   **{k:v for k,v in options.items() if k in ("after_not_sent","after_interrupted")}}
        if flow.get("after_model_rejection"):
            options["after_model_rejection"] = flow["after_model_rejection"]
    try:
        jid = request_image_recovery(watcher.root, job, flow["pending_model"], **options)
    except TraceError:
        raise
    except ImageRecoveryRejected as exc:
        if exc.reason == "image_transport_unavailable":
            publish("handling", exc.reason, "原请求未发送，正在等待当前生图线路恢复。", next_check=time.time()+60)
        elif exc.reason == "image_route_changed":
            publish("handling", exc.reason, "配置已变化，正在重新核对当前生图模型。", pending_model=None)
        elif exc.reason == "image_scope_changed":
            count = sum(bool(row.get("selected_pin")) for row in _asset_rows(watcher, job, flow["targets"]))
            publish("resolved" if count < total else "succeeded", exc.reason,
                    f"原批次目前已选用 {count}/{total} 项，其余制作范围以当前资产工单为准。", terminal=True)
        else:
            publish("blocked", exc.reason, "工作台已拒绝本次补图命令，请在制作动态查看原工单回执。", terminal=True)
        return True
    except (OSError, ValueError, RuntimeError) as exc:
        # Resend only the same local idempotent command. The worker owns charge reconciliation.
        publish("handling", "image_order_reconcile", "正在用原工单标识核对补图提交回执。",
                next_check=time.time()+45, submit_error=type(exc).__name__)
        return True
    attempts = list(dict.fromkeys([*flow["attempts"], jid]))
    publish("handling", "image_recovery_queued", f"已恢复原批次的缺图，生成和核验进度见补图工单。",
            child_id=jid, attempts=attempts, next_check=0)
    return True
