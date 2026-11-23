"""Recover the still-missing images of one asset commission, using its actual receipts."""
import re
import sqlite3
from contextlib import closing, nullcontext
from pathlib import Path

from pianchang.app_service import read_json
from pianchang.asset_dispatch import _basis, _matches_basis, _pin, asset_work_state
from pianchang.asset_diagnostics import asset_diagnostic
from pianchang.model_routes import load_routes
from pianchang.studio_assets import studio_asset_catalog
from pianchang.story_spec import digest_json
from trace_system import event, traced


class ImageRecoveryConflict(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def image_recovery_key(pid, body):
    source = body.get("resume_images_from")
    model = body.get("model_id")
    if (set(body) - {"resume_images_from", "model_id", "after_interrupted", "after_review", "after_not_sent", "after_reinspect", "answer_id","after_authorized_review","after_model_rejection"}
            or not isinstance(source, str) or not re.fullmatch("[a-f0-9]{32}", source)
            or not isinstance(model, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}", model)):
        raise ValueError("参考图恢复请求缺少有效的原任务或模型。")
    for name, size in (("after_interrupted", 32), ("after_review", 32), ("after_not_sent", 32), ("after_reinspect",32),
                       ("answer_id", 64), ("after_authorized_review",64), ("after_model_rejection",32)):
        if name in body and (not isinstance(body[name], str) or not re.fullmatch(f"[a-f0-9]{{{size}}}", body[name])):
            raise ValueError("参考图恢复请求的回执标识无效。")
    if sum(key in body for key in ("after_interrupted", "after_review", "after_not_sent", "after_reinspect","after_model_rejection")) > 1:
        raise ValueError("参考图恢复请求包含相互冲突的回执。")
    if "after_authorized_review" in body:
        if set(body) - {"resume_images_from","model_id","after_authorized_review","after_interrupted","after_not_sent","after_model_rejection"}:
            raise ValueError("一次追加修订不能与其他生图或复核授权合并。")
        prior = body.get("after_interrupted") or body.get("after_not_sent") or body.get("after_model_rejection")
        return "image-revision-" + digest_json([pid,source,body["after_authorized_review"],prior,body["model_id"] if prior else None])
    if "after_model_rejection" in body:
        raise ValueError("缺少对应这次修订的追加授权。")
    return "image-recovery-" + digest_json([pid, body])


def _original_basis(service, job):
    root = job.get("_trace_context") or {}
    rows = service.tracer.get_events(job["trace_id"], names=["agent.task." + job["kind"]], span_id=root.get("span_id"))
    starts = [row for row in rows if row["kind"] == "span.start"]
    if len(starts) == 1:
        data = service.tracer.resolve(starts[0]["data"])
        inputs = service.tracer.resolve(data.get("inputs")) or {}
        attrs = service.tracer.resolve(data.get("attributes")) or {}
        if (inputs.get("project_id") == job["project_id"] and inputs.get("action") == job["kind"]
                and attrs.get("job_id") == job["id"]):
            return (inputs.get("body") or {}).get("basis")
    return None


def _original_attempts(root, source, subject, purpose):
    from tools.workbench_loop_dependency import _unit_calls
    calls = _unit_calls(root, source, subject, purpose)
    if not calls or not calls[-1].get("provider_code"):
        return 0
    return calls[-1].get("remaining_attempts", 1)


def _image_not_sent(call):
    return (call.get("request_kind") == "image" and call.get("dispatch") == "not_sent"
            and call.get("phase") in {"connect_tcp", "start_tls"}
            and call.get("error_type") in {"ConnectError", "ConnectTimeout"}
            and call.get("response_seq") is None and call.get("http_status") is None and bool(call.get("error_seq")))


def _unsent_call(root, child, subject, purpose, model):
    from tools.workbench_loop_dependency import _unit_calls
    calls = _unit_calls(root, child, subject, purpose)
    if not calls:
        return None
    last = calls[-1]
    if last.get("model_id") != model or not _image_not_sent(last):
        return None
    if any((c.get("dispatch") != "not_sent" and (not c.get("response_seq") or not 200 <= (c.get("http_status") or 0) < 500
            or c.get("http_status") == 408)) or (c["request_kind"] == "image" and not c.get("provider_code")
            and c.get("dispatch") != "not_sent") for c in calls[:-1]):
        return None
    return {**last, "subject_id":subject, "purpose":purpose}


@traced("workbench.watch.verify_unsent_image", kind="validation", capture_inputs=False, capture_output=False)
def unsent_image_request(root, child_job):
    result = child_job.get("result") or {}
    receipt = result.get("image_recovery") or {}
    if (child_job.get("status") != "failed" or receipt.get("status") not in {"stopped_unknown", "not_sent"}
            or not child_job.get("_image_recovery_from") or receipt.get("source_job_id") != child_job["_image_recovery_from"]
            or not child_job.get("_image_recovery_model") or receipt.get("model_id") != child_job["_image_recovery_model"]
            or child_job.get("trace_incomplete") or child_job.get("cancel_requested")):
        return None
    proofs = []
    for issue in (result.get("asset_diagnostics") or {}).get("items", []):
        retained = any(a.get("pin") == issue.get("candidate") and a.get("unchanged") is True and
                       (a["pin"]["subject_id"], a["pin"]["purpose"]) == (issue["subject_id"],issue["purpose"])
                       for a in receipt.get("revision_attempts",[]) if a.get("pin"))
        if issue.get("trace_id") != child_job.get("trace_id") or issue.get("candidate") and not (
                retained and child_job.get("_image_revision_answer_id") == receipt.get("revision_request_id")):
            continue
        proof = _unsent_call(Path(root), child_job, issue["subject_id"], issue["purpose"], receipt["model_id"])
        if proof:
            proofs.append(proof)
    return proofs[0] if len(proofs) == 1 else None

def _legacy_mixed_subject_review(report):
    if report.get("visual_input_version") or report.get("observation_error"):
        return False
    subject = report["candidate"]["subject_id"]
    other = {f"anchor_{i}" for i, pin in enumerate(report.get("anchor_pins", []))
             if pin.get("subject_id") and pin["subject_id"] != subject}
    return bool(other) and any(c.get("required",True) and c.get("status") in {"FAIL","UNKNOWN"}
                and (c.get("path") in other or c.get("path") in {"subject","asset_use","style","palette","concept","revision"})
                for c in report.get("checks", []))


def _unsupported_portrait_identity(report):
    """Identify v2 evidence that rejected an original casting image for lacking an identity exemplar."""
    if (report.get("visual_input_version") != "reference-visual-input/2" or report.get("subject_input_version") or
            report.get("observation_error") or (report.get("candidate") or {}).get("purpose") != "portrait"):
        return False
    brief = report.get("brief") or {}
    pin = report["candidate"]
    if (brief.get("purpose") != "portrait" or brief.get("subject_id") != pin.get("subject_id") or
            not (brief.get("canonical") or {}).get("name") or brief.get("declared_refs") or not isinstance(report.get("anchors"),list) or
            [a.get("pin") for a in report["anchors"]] != report.get("anchor_pins") or
            any(not a.get("pin") or a.get("explicit") or a["pin"].get("subject_id") == pin["subject_id"] for a in report["anchors"])):
        return False
    required = {c.get("path"):c for c in report.get("checks",[]) if c.get("required",True)}
    if (not {"subject","asset_use","style","palette","concept"}.issubset(required) or
            any(c.get("status") != "PASS" for path,c in required.items() if path!="subject")):
        return False
    subject = required["subject"]
    if subject.get("status") not in {"FAIL","UNKNOWN"} or subject.get("frame_ids") != ["candidate"]:
        return False
    note = subject.get("actual") or ""
    missing = re.search(r"(?:未给出|未提供|未说明|未指定|未描述|没有|缺乏|缺少).{0,20}(?:专属(?:标志|特征)|"
                        r"身份(?:参考|标志|特征)|(?:参考|本人)(?:照片|肖像)|可辨[认识]的.{0,6}(?:标志|特征)|明确的外貌特征)",note)
    unverifiable = re.search(r"(?:无法|难以|不能|不足以)(?:直接)?(?:证实|确认|证明|确定|辨认|辨识|核实)",note)
    if not missing or not unverifiable:
        return False
    from pianchang.reference_subject import has_portrait_appearance_hint
    return not has_portrait_appearance_hint(brief)


def _needs_corrected_visual_review(report):
    return _legacy_mixed_subject_review(report) or _unsupported_portrait_identity(report)


def _review_targets(service, source, child, catalog, work, jobs, *, reinspect=False, extra_revision=False):
    from pianchang.asset_review import POLICY_VERSION, _repair_instructions, review_asset_id
    from tools.workbench_loop_dependency import _unit_calls
    if (not child or child.get("_image_recovery_from") != source["id"] or child.get("status") != "failed"
            or child.get("project_id") != source["project_id"] or child.get("trace_incomplete") or child.get("cancel_requested")
            or ((child.get("result") or {}).get("image_recovery") or {}).get("status") != "completed"):
        return []
    family = [j for j in jobs if j.get("_image_recovery_from") == source["id"]]
    if any(j.get("trace_incomplete") for j in family):
        return []
    original = {(i["subject_id"],i["purpose"]) for i in source["result"]["asset_diagnostics"]["items"]}
    heads = {}
    for entity in catalog["entities"]:
        for ref in entity["references"]:  # The catalog orders each asset's newest revision first.
            heads.setdefault(ref["asset_id"], _pin(ref))
    targets = []
    for row in work["items"]:
        key = row["subject_id"], row["purpose"]
        pin, report = row.get("candidate"), row.get("review") or {}
        ref = report.get("review_ref") or {}
        if (key not in original or key[1] == "voice" or row.get("job_id") != child["id"] or row["status"] != "needs_attention"
                or row.get("selected_pin") or not pin or heads.get(pin["asset_id"]) != pin or report.get("candidate") != pin
                or report.get("trace_id") != child.get("trace_id") or report.get("status") not in {"FAIL", "UNKNOWN"}
                or report.get("policy_version") != POLICY_VERSION or ref.get("asset_id") != review_asset_id(pin)):
            continue
        corrected_review = _needs_corrected_visual_review(report)
        if reinspect != corrected_review or not reinspect and not _repair_instructions(report):
            continue
        # Require the current immutable report, including its actual observations.
        with closing(sqlite3.connect(Path(catalog["store_path"]).resolve().as_uri() + "?mode=ro", uri=True)) as db:
            head = db.execute("SELECT revision_id,sha256 FROM assets WHERE story_id=? AND asset_id=? AND kind='reference_review' "
                              "ORDER BY seq DESC LIMIT 1", (catalog["story_id"],ref["asset_id"])).fetchone()
        if head != (ref.get("revision_id"),ref.get("sha256")) or digest_json({k:v for k,v in report.items() if k!="review_ref"}) != ref.get("sha256"):
            continue
        if reinspect:
            targets.append({"subject_id":key[0], "purpose":key[1], "owner":child["id"], "feedback":row.get("feedback",""),
                            "pin":pin, "review_ref":ref})
            continue
        if extra_revision and (report["status"] != "FAIL"
                or any(c.get("required",True) and c.get("status") not in {"PASS","FAIL"} for c in report.get("checks",[]))
                or not any(c.get("required",True) and c.get("status")=="FAIL" and "candidate" in (c.get("frame_ids") or [])
                           for c in report.get("checks", []))):
            continue
        limit = _original_attempts(service.root, source, *key)
        if not 1 <= limit <= 2:
            continue
        calls = [c for member in family for c in _unit_calls(service.root, member, *key) if c["request_kind"] == "image"]
        if any(not c.get("provider_code") and not _image_not_sent(c) and
               not (200 <= (c.get("http_status") or 0) < 300 and c.get("response_seq")) for c in calls):
            continue
        spent = sum(200 <= (c.get("http_status") or 0) < 300 for c in calls)
        family_traces = {member.get("trace_id") for member in family}
        reviewed = {(a["pin"]["asset_id"], a["pin"]["revision_id"]) for a in row.get("attempts", [])
                    if a.get("trace_id") in family_traces and isinstance(a.get("pin"), dict) and a.get("review_ref")}
        eligible = spent >= limit if extra_revision else 0 < spent < limit
        if eligible and spent == len(reviewed) and (pin["asset_id"], pin["revision_id"]) in reviewed:
            targets.append({"subject_id":key[0], "purpose":key[1], "owner":child["id"], "feedback":row.get("feedback",""),
                            "remaining_attempts":1 if extra_revision else limit-spent, "pin":pin, "review_ref":ref})
    return targets


@traced("workbench.watch.inspect_image_reviews", kind="validation", capture_inputs=False, capture_output=False)
def recoverable_image_reviews(root, original_job, child_job):
    return _read_review_targets(root, original_job, child_job, reinspect=False)


@traced("workbench.watch.inspect_legacy_image_reviews", kind="validation", capture_inputs=False, capture_output=False)
def reinspectable_image_reviews(root, original_job, child_job):
    return _read_review_targets(root, original_job, child_job, reinspect=True)


def _answer_item(target):
    return {"subject_id":target["subject_id"], "purpose":target["purpose"], "asset_id":target["pin"]["asset_id"],
            "revision_id":target["pin"]["revision_id"], "review_ref":target["review_ref"], "candidate_job_id":target["owner"]}


@traced("workbench.watch.authorizable_image_reviews", kind="validation", capture_inputs=False, capture_output=False)
def authorizable_image_reviews(root, original_job, child_job):
    return [_answer_item(target) for target in _read_review_targets(root, original_job, child_job,
                                                                    reinspect=False, extra_revision=True)]


def _read_review_targets(root, original_job, child_job, *, reinspect, extra_revision=False):
    from pianchang.preview_access import ReadOnlyPreviewSource
    from trace_system import get_tracer
    service = ReadOnlyPreviewSource(root)
    service.lock, service.tracer = nullcontext(), get_tracer(Path(root) / "outputs/traces")
    service.state = read_json(service.state_dir / "state.json")
    pid = original_job["project_id"]
    jobs = {j["id"]:j for j in service.state["jobs"] if j.get("project_id") == pid}
    original, child = jobs.get(original_job["id"]), jobs.get(child_job["id"])
    if not original or not child or original.get("cancel_requested") or original.get("trace_incomplete"):
        return []
    catalog = studio_asset_catalog(service, pid)
    if catalog.get("pending_script_ref") or not _matches_basis(service, catalog, _original_basis(service, original)):
        return []
    return _review_targets(service, original, child, catalog, asset_work_state(service, pid, catalog), jobs.values(),
                           reinspect=reinspect, extra_revision=extra_revision)


def _authorized_targets(service, source, jobs, catalog, work, body, unsent):
    from tools.workbench_loop_dependency import _unit_calls
    rid = body["after_authorized_review"]
    path = service.root / "outputs/workbench-loop/watch/answers" / (rid+".json")
    saved = read_json(path) if path.is_file() else {}
    request = saved.get("request") or {}
    pid, jid = source["project_id"], source["id"]
    if (saved.get("request_id"),saved.get("project_id"),saved.get("job_id")) != (rid,pid,jid) or (
            request.get("kind") != "review_asset_candidates" or request.get("id") != rid or
            request.get("project_id") != pid or request.get("job_id") != jid or
            (saved.get("answers") or {}).get("authorize_revision") is not True or
            not re.fullmatch("[a-f0-9]{32}", str(saved.get("trace_id")))):
        raise ImageRecoveryConflict("image_scope_changed","没有对应此工单和候选的追加修订授权。")
    items = request.get("revision_items")
    if (not isinstance(items,list) or not items or not isinstance(request.get("items"),list) or
            any(not isinstance(i,dict) or i not in request["items"] or not isinstance(i.get("candidate_job_id"),str) for i in items)
            or len({(i.get("subject_id"),i.get("purpose")) for i in items}) != len(items)):
        raise ImageRecoveryConflict("image_scope_changed","追加修订范围与已答复的问题不一致。")
    previous_id = body.get("after_model_rejection") or body.get("after_not_sent") or body.get("after_interrupted")
    previous = jobs.get(previous_id,{})
    prior_receipt = (previous.get("result") or {}).get("image_recovery") or {}
    if previous_id and (previous.get("_image_revision_answer_id") != rid or previous.get("_image_recovery_from") != jid
                        or previous.get("cancel_requested") or previous.get("trace_incomplete")):
        raise ImageRecoveryConflict("image_scope_changed","无法核对此次追加授权的上一份工单。")
    if body.get("after_model_rejection") and (previous.get("status") != "failed" or prior_receipt.get("status") != "model_rejected"
            or prior_receipt.get("source_job_id") != jid or prior_receipt.get("revision_request_id") != rid
            or prior_receipt.get("model_id") != previous.get("_image_recovery_model")
            or prior_receipt.get("model_id") == body["model_id"]):
        raise ImageRecoveryConflict("image_scope_changed","无法核实上一条生图线路明确拒绝这次修订。")
    retained = {(a["pin"]["subject_id"],a["pin"]["purpose"]):a for a in prior_receipt.get("revision_attempts",[])
                if a.get("unchanged") is True and a.get("pin")}
    proven = {(unsent["subject_id"],unsent["purpose"])} if unsent else set()
    if body.get("after_model_rejection"):
        for key in retained:
            images = [c for c in _unit_calls(service.root,previous,*key) if c["request_kind"]=="image"]
            if (len(images)==1 and images[0].get("provider_code") and images[0]["provider_code"]==prior_receipt.get("provider_code")
                    and images[0].get("model_id")==previous["_image_recovery_model"] and images[0].get("response_seq")):
                proven.add(key)
        if not proven:
            raise ImageRecoveryConflict("image_scope_changed","未核实方舟对此次修订的明确拒绝回执。")
    allowed = {(i["subject_id"],i["purpose"]):i for i in items}
    previous_owners, rows = {}, []
    for row in work["items"]:
        key = row["subject_id"],row["purpose"]
        claim, attempt = allowed.get(key), retained.get(key)
        if (key in proven and claim and attempt and row.get("job_id")==previous_id and
                attempt["pin"]==row.get("candidate") and attempt["review_ref"]==claim.get("review_ref")):
            previous_owners[key] = previous_id
            row = {**row, "job_id":claim["candidate_job_id"]}
        rows.append(row)
    inspected_work = {**work, "items":rows}
    targets = []
    for owner in dict.fromkeys(item["candidate_job_id"] for item in items):
        child = jobs.get(owner,{})
        if not previous_id and child.get("_image_recovery_model") != body["model_id"]:
            continue
        for target in _review_targets(service,source,child,catalog,inspected_work,jobs.values(),extra_revision=True):
            if _answer_item(target) in items:
                target["owner"] = previous_owners.get((target["subject_id"],target["purpose"]),target["owner"])
                targets.append(target)
    if not targets:
        raise ImageRecoveryConflict("image_scope_changed","已授权的候选没有仍符合条件的修订项。")
    return targets, saved.get("trace_id")


@traced("studio.assets.verify_image_recovery", kind="validation", capture_inputs=False, capture_output=False)
def verified_image_recovery(service, pid, body, catalog, *, probe_transport=True):
    from tools.workbench_loop_dependency import recheck_dependency

    image_recovery_key(pid, body)
    source_id, model = body["resume_images_from"], body["model_id"]
    with service.lock:
        jobs = {j["id"]: dict(j) for j in service.state["jobs"] if j.get("project_id") == pid}
    source = jobs.get(source_id, {})
    if (source.get("kind") not in {"asset_production", "asset_regenerate"}
            or source.get("status") not in {"failed", "interrupted"} or not source.get("trace_id")
            or any(source.get(k) for k in ("trace_incomplete", "cancel_requested", "_image_recovery_from"))):
        raise ImageRecoveryConflict("image_scope_changed", "不能核实这份原始参考图委托。")
    if catalog.get("pending_script_ref") or not _matches_basis(service, catalog, _original_basis(service, source)):
        raise ImageRecoveryConflict("image_scope_changed", "原资产委托的剧本或设定已改变。")
    if "answer_id" in body:
        rid = body["answer_id"]
        answer = read_json(service.root / "outputs/workbench-loop/watch/answers" / (rid + ".json"))
        values = answer.get("answers") or {}
        if (answer.get("project_id") != pid or answer.get("job_id") != source_id or answer.get("request_id") != rid
                or not (values.get("confirm") is True or values.get("endpoint_id") == model)):
            raise ValueError("这次补充的信息与原工单或接入点不匹配。")
    if "after_interrupted" in body:
        previous = jobs.get(body["after_interrupted"], {})
        if (previous.get("_image_recovery_from") != source_id or previous.get("_image_recovery_model") != model
                or previous.get("status") != "interrupted" or previous.get("trace_incomplete")
                or previous.get("_image_revision_answer_id") != body.get("after_authorized_review")):
            raise ImageRecoveryConflict("image_scope_changed", "没有匹配的中断恢复工单。")

    route = None
    if not body.get("after_reinspect"):
        route = load_routes(service.root / ".env").resolve("reference.image")
        if route.model_id != model or not route.api_key:
            raise ImageRecoveryConflict("image_route_changed", "当前参考图路由与已核对的模型不一致。")
    unsent = None
    if "after_not_sent" in body:
        previous = jobs.get(body["after_not_sent"], {})
        if previous.get("_image_recovery_from") == source_id and previous.get("_image_recovery_model") == model:
            unsent = unsent_image_request(service.root, previous)
        if not unsent:
            raise ImageRecoveryConflict("image_scope_changed", "没有核实到同一参考图请求在发送前中断的回执。")
        if probe_transport:
            from tools.workbench_loop_ark import probe_image_transport
            probe = probe_image_transport(service.root, unsent)
            if probe.get("status") != "reachable" or probe.get("model_id") != model:
                raise ImageRecoveryConflict("image_transport_unavailable", "生图线路仍待连通，保留原请求继续探测。")
    work = asset_work_state(service, pid, catalog)
    order = {**body, "basis": _basis(catalog), "style_sha256": digest_json(work["style"]),
             "image_route_sha256": digest_json(route.snapshot()) if route else None}
    if body.get("after_authorized_review"):
        targets, answer_trace = _authorized_targets(service,source,jobs,catalog,work,body,unsent)
        return {**order, "targets":targets, "revision_answer_trace_id":answer_trace}
    if body.get("after_review") or body.get("after_reinspect"):
        prior = jobs.get(body.get("after_review") or body["after_reinspect"], {})
        if prior.get("_image_recovery_model") != model:
            raise ImageRecoveryConflict("image_route_changed", "当前参考图路由与已核对的模型不一致。")
        targets = _review_targets(service, source, prior, catalog, work, jobs.values(), reinspect=bool(body.get("after_reinspect")))
        if not targets:
            raise ImageRecoveryConflict("image_scope_changed", "原工单没有仍可按现有报告和剩余额度修订的参考图。")
        return {**order, "targets":targets}
    issues = ((source.get("result") or {}).get("asset_diagnostics") or {}).get("items") or []
    original = {(i.get("subject_id"), i.get("purpose")) for i in issues}
    existing = {(entity["id"], ref["purpose"]) for entity in catalog["entities"] for ref in entity["references"]}
    targets, evidence = [], {}
    for row in work["items"]:
        key = (row["subject_id"], row["purpose"])
        owner_id = row.get("job_id")
        owner = jobs.get(owner_id, {})
        if (key not in original or key in existing or key[1] == "voice" or row.get("candidate")
                or row.get("selected_pin") or row["status"] != "needs_attention"
                or owner.get("status") not in {"failed", "interrupted"} or owner.get("trace_incomplete")
                or (owner_id != source_id and owner.get("_image_recovery_from") != source_id)):
            continue
        if owner_id not in evidence:
            check = recheck_dependency(service.root, owner) or {}
            intervention = check.get("human_intervention") or {}
            evidence[owner_id] = {(i.get("subject_id"), i.get("purpose")): i.get("remaining_attempts", 1)
                                  for i in intervention.get("items", []) if intervention.get("kind") == "reference_image_model_access"
                                  and i.get("job_id") == owner_id}
            if unsent and owner_id == body["after_not_sent"]:
                evidence[owner_id][(unsent["subject_id"], unsent["purpose"])] = 1
        if key in evidence[owner_id]:
            attempts = (evidence[owner_id][key] if owner_id == source_id else _original_attempts(service.root, source, *key))
            if not 1 <= attempts <= 2:
                continue
            targets.append({"subject_id": key[0], "purpose": key[1], "owner": owner_id, "feedback": row.get("feedback", ""),
                            "remaining_attempts": attempts})
    if not targets:
        raise ImageRecoveryConflict("image_scope_changed", "原工单没有仍缺少素材且可以安全恢复的参考图。")
    return {**order, "targets": targets}


def run_image_recovery(service, job, order, catalog):
    from pianchang.asset_dispatch import _produce_item

    pid = job["project_id"]
    body = {k: order[k] for k in ("resume_images_from", "model_id", "after_interrupted", "after_review", "after_not_sent", "after_reinspect", "answer_id","after_authorized_review","after_model_rejection") if k in order}
    # This order was enqueued only after a reachable probe. Reconcile its original receipt again.
    verified = verified_image_recovery(service, pid, body, catalog, probe_transport=False)
    for field in ("basis", "style_sha256", "image_route_sha256"):
        if verified[field] != order[field]:
            raise ValueError("排队期间原资产委托或参考图路由已变化。")
    targets = [row for row in order["targets"] if row in verified["targets"]]
    if not targets:
        raise ValueError("恢复期间原工单的待制作素材已变化。")
    receipt = {"source_job_id": body["resume_images_from"], "model_id": body["model_id"], "status": "completed",
               "attempted": 0, "generated": 0, "selected": 0, "remaining": len(targets)}
    issues = []
    if body.get("after_reinspect"):
        receipt.update(reinspected=0, reviews=[])
    if body.get("after_authorized_review"):
        receipt.update(revision_request_id=body["after_authorized_review"], max_generations_per_item=1, revision_attempts=[])
    for target in targets:
        # Selection of a preceding image can revise shot bindings without changing this asset's basis.
        catalog = studio_asset_catalog(service, pid)
        if catalog.get("pending_script_ref") or _basis(catalog) != order["basis"]:
            receipt["status"] = "scope_changed"
            break
        work = asset_work_state(service, pid, catalog)
        unit = next(u for u in work["items"] if (u["subject_id"], u["purpose"]) == (target["subject_id"], target["purpose"]))
        entity = next(e for e in catalog["entities"] if e["id"] == target["subject_id"])
        previous_pin = target.get("pin")
        media = next((_pin(r) for r in entity["references"] if r["purpose"] == unit["purpose"] and
                      (not previous_pin or r["asset_id"] == previous_pin["asset_id"])), None)
        if (unit.get("job_id") != target["owner"] or unit["status"] != "needs_attention" or unit.get("candidate") != previous_pin
                or unit.get("selected_pin") or digest_json(work["style"]) != order["style_sha256"]
                or media != previous_pin or previous_pin and (unit.get("review") or {}).get("review_ref") != target["review_ref"]):
            receipt["status"] = "scope_changed"
            continue
        task = {"feedback":target["feedback"], "spec_sha256":catalog["spec"]["sha256"]}
        if body.get("after_reinspect"):
            task.update(pin=previous_pin, review_only=True, previous_review=target["review_ref"])
        else:
            task.update(image_route_sha256=order["image_route_sha256"], remaining_attempts=target["remaining_attempts"])
        if previous_pin and not body.get("after_reinspect"):
            task.update(asset_id=previous_pin["asset_id"], parent_revision=previous_pin["revision_id"],
                        repair_review_ref=target["review_ref"])
        action = "复核参考图" if body.get("after_reinspect") else "恢复参考图"
        service._update(job, message=f"{action} {receipt['attempted'] + 1}/{len(targets)} · {entity['name']}")
        _produce_item(service, pid, job, catalog, unit, task, work["style"])
        receipt["attempted"] += 1
        if body.get("after_authorized_review"):
            receipt["revision_attempts"].append({"pin":previous_pin, "review_ref":target["review_ref"],
                "unchanged":unit.get("candidate")==previous_pin, "result_pin":unit.get("candidate")})
        if unit["status"] == "needs_attention":
            issues.append(asset_diagnostic(unit, entity["name"], span_id=unit.get("error_span_id")))
        if body.get("after_reinspect"):
            new_ref = (unit.get("review") or {}).get("review_ref")
            completed = bool(new_ref and new_ref != target["review_ref"])
            receipt["reinspected"] += int(completed)
            receipt["selected"] += int(unit["status"] == "approved")
            receipt["remaining"] -= int(unit["status"] == "approved")
            receipt["reviews"].append({"pin":previous_pin,"previous_review_ref":target["review_ref"],
                                       "review_ref":new_ref if completed else None,"status":(unit.get("review") or {}).get("status") if completed else "UNKNOWN"})
            continue
        generated = unit.get("candidate") and unit["candidate"] != previous_pin
        if generated:
            receipt["generated"] += 1
            receipt["remaining"] -= 1
            receipt["selected"] += int(unit["status"] == "approved")
        if unit.get("rejected_image_code") and unit.get("rejected_image_model") == order["model_id"]:
            receipt.update(status="model_rejected", provider_code=unit["rejected_image_code"])
        elif generated:
            continue
        else:
            proof = _unsent_call(service.root, job, unit["subject_id"], unit["purpose"], order["model_id"])
            if proof:
                receipt.update(status="not_sent", transport_failure=proof)
            else:
                receipt["status"] = "stopped_unknown"
        break
    summary = f"本次续做 {receipt['attempted']} 项参考图，已生成 {receipt['generated']} 项，已选用 {receipt['selected']} 项。"
    if body.get("after_reinspect"):
        summary = f"已对 {receipt['reinspected']} 项现有参考图追加复核报告，已选用 {receipt['selected']} 项。"
    result = {"image_recovery": receipt, "summary": summary, "processed": receipt["attempted"],
              "needs_attention": bool(issues) or receipt["status"] != "completed", "trace_id": job["trace_id"]}
    if issues:
        result["asset_diagnostics"] = {"project_id": pid, "summary": summary, "items": issues}
    event("studio.assets.image_recovery.result", receipt, kind="state")
    return result
