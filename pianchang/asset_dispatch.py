"""Durable work orders between script publication, reference production and approval."""
from __future__ import annotations

import copy
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

from pianchang.app_service import service_tool
from pianchang.collab_store import CollabStore
from pianchang.reference_assets import _prepare_binding
from pianchang.story_spec import digest_json, validate_compilation
from pianchang.studio_assets import studio_asset_catalog, studio_project_for_video
from trace_system import TraceError, current_context, event, traced, use_context

ASSET_ACTIONS = {"asset_production", "asset_regenerate", "asset_inspect", "studio_reference"}


@traced("studio.assets.auto_schedule", kind="task")
def schedule_published_assets(service, directory, pid, source_job, style="", project=None):
    from pianchang.app_service import write_json
    key = "assets-auto-" + source_job["id"]
    intent = {"project_id": pid, "key": key, "body": {"style": style} if style else {}, "context": current_context()}
    write_json(directory / "asset-intent.json", intent)
    if project is not None:
        # Startup can recover the order even if the server exits the
        # instant the new project becomes visible in the web catalog.
        write_json(directory / "project.json", project)
    job = service.submit("asset_production", pid, intent["body"], key)
    return {"id": job["id"], "trace_id": job["trace_id"]}


@traced("studio.assets.recover_intents", kind="task", capture_output=False)
def recover_asset_intents(service):
    from pianchang.app_service import read_json, project_id_for_path
    for path in (service.state_dir / "stories").glob("*/asset-intent.json"):
        try:
            intent = read_json(path)
            project = path.parent / "project.json"
            if not project.is_file() or intent["project_id"] != project_id_for_path(project):
                continue
            if any(j.get("_idempotency_key") == intent["key"] for j in service.state["jobs"]):
                continue
            with use_context(intent["context"], tracer=service.tracer):
                service.submit("asset_production", intent["project_id"], intent["body"], intent["key"])
        except (KeyError, ValueError, OSError) as exc:
            event("studio.assets.recovery_failed", {"path": str(path), "error": str(exc)}, kind="error")


@traced("studio.assets.reconcile_selections", kind="task", capture_inputs=False)
def reconcile_asset_selections(service, pid, items):
    """Adopt stored, verified legacy candidates using their original review evidence."""
    results = []
    for item in items:
        if item["status"] != "awaiting_review":
            continue
        pin = item.get("candidate")
        review_ref = (item.get("review") or {}).get("review_ref")
        row = {"subject_id": item["subject_id"], "purpose": item["purpose"],
               "candidate": pin, "review_ref": review_ref, "project_id": pid}
        with service._asset_write_lock(pid):
            current = _record(service, pid).get("items", {}).get(_key(item["subject_id"], item["purpose"]), {})
            if (current.get("status") != "awaiting_review" or current.get("candidate") != pin
                    or (current.get("review") or {}).get("review_ref") != review_ref):
                continue
            try:
                _check("历史资产的候选版本及自检记录", True, bool(pin and review_ref))
                receipt = select_verified_asset(service, pid, {**_identity(pin), "review_ref": review_ref})
            except TraceError:
                raise
            except (KeyError, TypeError, ValueError, FileNotFoundError) as exc:
                message = service.sanitize(str(exc))
                _store_unit(service, pid, {**current, "status": "needs_attention", "error": message,
                            "trace_id": current_context()["trace_id"], "error_span_id": current_context()["span_id"]})
                row.update(status="blocked", reason=message)
            else:
                row.update(status="selected", shot_ids=receipt["shot_ids"], spec=receipt["spec"])
            event("studio.assets.reconcile_selection", row, kind="state")
            results.append(row)
    return results


def _schedule_missing_assets(service, pid):
    # Use the effective script/spec (including a published revision) rather than the source template.
    view = studio_project_for_video(service, pid)
    validation = validate_compilation(view["project"])
    if not validation["ready_for_generation"]:
        return {"status": "blocked", "reason": "工程尚未通过剧本及制作规格检查。"}
    catalog = studio_asset_catalog(service, pid)
    if catalog.get("pending_script_ref"):
        return {"status": "blocked", "reason": "新版剧本已发布，当前制作规格需要同步。"}
    work = asset_work_state(service, pid, catalog)
    pending = [row for row in work["items"] if row["status"] == "awaiting_review"]
    if pending:
        # A selection receipt opens this story's trace, even on a startup that covers many stories.
        caller = current_context()
        links = [{"trace_id": caller["trace_id"], "span_id": caller["span_id"], "relation": "scheduled_by"}] if caller else []
        with service.tracer.trace("studio.assets.select_project", inputs={"project_id": pid, "basis": work["basis"],
                "candidate_count": len(pending)}, attributes={"project_id": pid}, links=links) as adoption:
            outcomes = reconcile_asset_selections(service, pid, pending)
            adoption.set_output(outcomes)
        event("studio.assets.project_selection", {"project_id": pid, "trace_id": adoption.context["trace_id"],
              "selected": sum(o["status"] == "selected" for o in outcomes), "checked": len(outcomes)}, kind="state")
    batches = [j for j in work["jobs"] if j["kind"] == "asset_production"]
    active = next((j for j in batches if j["status"] in {"queued", "running"}), None)
    if active:
        return {"status": "queued", "reason": "已有整批资产制作任务。", "job_id": active["id"]}
    # Interrupted/generating units can already have incurred charges. The execution
    # boundary checkpoints each unit before calling its first provider tool.
    remaining = sorted((row["subject_id"], row["purpose"]) for row in work["items"] if row["status"] == "planned")
    if not remaining:
        return {"status": "covered", "reason": "当前版本的资产已进入制作流程。"}
    routes = getattr(service, "model_routes", None)
    if routes is None:
        configured = bool(service.video_config.api_key)
    else:
        purposes = {"voice.synthesis" if purpose == "voice" else "reference.image" for _, purpose in remaining}
        selected = [routes.resolve(purpose) for purpose in purposes]
        configured = all(service.video_config.api_key if route.connection == "bailian.media" else route.api_key
                         for route in selected)
    if not configured:
        return {"status": "blocked", "reason": "等待配置待制作参考素材所用连接的 API Key，再启动工作台即可自动制作。"}
    # A previous queued/interrupted batch may have exited without reaching a
    # single unit. Allow a new order for untouched work even in that case.
    previous_id = batches[-1]["id"] if batches else None
    key = "asset-library-" + digest_json([pid, work["basis"], remaining, previous_id])
    job = service.submit("asset_production", pid, {}, key, library_backfill=True)
    return {"status": "queued", "reason": f"已安排 {len(remaining)} 项资产。", "job_id": job["id"]}


@service_tool("studio.assets.library_schedule")
def enqueue_library_assets(service):
    """Discover compiled stories and dispatch missing work when the actual server starts."""
    # Restore committed publication requests with their exact style and parent Trace
    # before issuing generic backfill orders for older projects.
    recover_asset_intents(service)
    results = {}
    for pid in service._discover():
        try:
            decision = _schedule_missing_assets(service, pid)
        except TraceError:
            raise
        except (KeyError, TypeError, ValueError, OSError) as exc:
            decision = {"status": "blocked", "reason": service.sanitize(str(exc))}
        event("studio.assets.library_decision", {"project_id": pid, **decision}, kind="state")
        with service.lock:
            service.state.setdefault("asset_auto_dispatch", {})[pid] = decision
            service._save()
        results[pid] = decision
    return results


def _check(path, expected, actual):
    passed = expected == actual
    event("studio.asset_work.check", {"path": path, "status": "PASS" if passed else "FAIL",
          "expected": expected, "actual": actual}, kind="validation")
    if not passed:
        raise ValueError(f"{path} 已变化，请刷新后核对当前版本。")


def _identity(asset):
    return {k: asset[k] for k in ("asset_id", "revision_id", "sha256")}


def _key(subject, purpose):
    return digest_json([subject, purpose])


def _basis(catalog):
    # A shot binding publishes a new spec. It does not change what the reference should portray.
    return digest_json([catalog["story_id"], catalog.get("source_script_sha256") or (catalog.get("script_ref") or {}).get("sha256"),
        [[e["id"], e["version"], e["canonical"], e["source_refs"]]
         for e in sorted(catalog["entities"], key=lambda e: e["id"])]])


def _matches_basis(service, catalog, recorded, pin=None, *, uncertain=False):
    """Read older receipts in place. Never assign an old review to a changed script."""
    if recorded == _basis(catalog):
        return True
    if not recorded:
        return False
    # The first desktop release hashed the script reference object and the
    # entity list, initially in source order and later sorted by stable ID.
    # In particular, a locally compiled screenplay had a null script reference.
    _, source = service._project(catalog["project_id"])
    entities = {e["id"]: [e["id"], e["version"], e["canonical"], e["source_refs"]] for e in catalog["entities"]}
    orders = [list(entities), sorted(entities)]
    if set(source.get("entities", {})) == set(entities):
        orders.append(list(source["entities"]))
    story = catalog["story_id"]
    for order in orders:
        rows = [entities[eid] for eid in order]
        script_ref = catalog.get("script_ref")
        if script_ref and recorded == digest_json([story, script_ref, rows]):
            return True  # This historical fingerprint pins the immutable script.
        if recorded != digest_json([story, None, rows]):
            continue
        if uncertain and not pin:
            # The legacy null reference cannot establish which screenplay was
            # billed. Retain uncertainty; never replay that operation on startup.
            return True
        if not pin:
            continue
        entry = next((r for e in catalog["entities"] for r in e["references"] if _pin(r) == pin), None)
        origin = (entry or {}).get("origin") or {}
        sha = origin.get("project_sha256")
        script = source.get("script_source", {}).get("script")
        if sha and sha == digest_json(source) and digest_json(script if script is not None else source) == catalog["source_script_sha256"]:
            return True
        # Subsequent bindings leave immutable production specs. The spec ID
        # includes the script content hash and scopes these revisions to it.
        if sha and catalog["spec"].get("revision_id"):
            uri = Path(catalog["store_path"]).resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True)) as db:
                return db.execute("SELECT 1 FROM assets WHERE asset_id=? AND kind='production_spec' AND sha256=?",
                                  (catalog["spec"]["asset_id"], sha)).fetchone() is not None
    return False


def _style(service, pid):
    view = studio_project_for_video(service, pid)
    script = view["project"].get("script_source", {}).get("script", {})
    parts = [str(script[k]) for k in ("style", "visual_style", "palette", "concept", "idea", "logline") if script.get(k)]
    return ("遵循全剧统一设定。除剧本特别指定外，采用写实影视质感、自然材质与克制的饱和度；"
            "服装和道具颜色遵循各自原文，照明色温符合故事现场。全剧创作依据：" + " ".join(parts))[:3000]


def _record(service, pid):
    return service.state.get("asset_work", {}).get(pid, {})


def _unit(subject, purpose, basis, style):
    return {"subject_id": subject, "purpose": purpose, "basis": basis, "style_sha256": digest_json(style),
            "status": "planned", "candidate": None, "selected_pin": None, "attempts": [], "review": None,
            "trace_id": None, "job_id": None, "feedback": "", "error": None}


@traced("studio.assets.work_state", kind="tool", capture_output=False)
def asset_work_state(service, pid, catalog=None):
    catalog = catalog if catalog is not None else studio_asset_catalog(service, pid)
    basis = _basis(catalog)
    with service.lock:
        work = copy.deepcopy(_record(service, pid))
        auto_dispatch = copy.deepcopy(service.state.get("asset_auto_dispatch", {}).get(pid))
        jobs = [{k: j.get(k) for k in ("id", "kind", "status", "trace_id", "message", "error")}
                for j in service.state["jobs"] if j["project_id"] == pid and j["kind"] in ASSET_ACTIONS]
    job_status = {j["id"]: j["status"] for j in jobs}
    # An independent inspection can supersede the review that justified a selection.
    # Compare its immutable review ID and revision against the latest stored evidence.
    review_heads, legacy_proofs = {}, {}
    db_path = Path(catalog["store_path"])
    if work.get("items") and db_path.is_file():
        with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            review_heads = {a: {"asset_id": a, "revision_id": r, "sha256": h} for a, r, h in db.execute(
                "SELECT asset_id,revision_id,sha256 FROM assets WHERE story_id=? AND kind='reference_review' ORDER BY seq",
                (catalog["story_id"],))}
            if any(item.get("approval") and not item["approval"].get("review_ref") for item in work["items"].values()):
                legacy_proofs = {a: {"asset_id": rid, "revision_id": rev, "sha256": sha} for a, rid, rev, sha in db.execute(
                    "SELECT a.asset_id,r.asset_id,r.revision_id,r.sha256 FROM assets a JOIN dependencies d "
                    "ON d.asset_id=a.asset_id AND d.revision_id=a.revision_id JOIN assets r ON r.asset_id=d.dep_asset_id "
                    "AND r.revision_id=d.dep_revision_id WHERE a.story_id=? AND a.kind='reference_approval' "
                    "AND d.role='verification' ORDER BY a.seq", (catalog["story_id"],))}
    style = work.get("style", "")
    items = []
    for entity in catalog["entities"]:
        for purpose in entity["purposes"]:
            key = _key(entity["id"], purpose)
            saved = work.get("items", {}).get(key, {})
            current = _unit(entity["id"], purpose, basis, style)
            if saved.get("style_sha256") == current["style_sha256"] and _matches_basis(
                    service, catalog, saved.get("basis"), saved.get("candidate"),
                    uncertain=saved.get("status") in {"generating", "needs_attention"}):
                current = {**saved, "basis": basis}
            elif saved:
                current["attempts"] = saved["attempts"]
                # This lineage is only used to remove its obsolete bindings after the new
                # style/script passes verification. It never counts as a current selection.
                current["previous_pin"] = (saved.get("approval") or {}).get("pin") or saved.get("previous_pin")
            if current["status"] == "generating" and job_status.get(current["job_id"]) not in {"queued", "running"}:
                current.update(status="needs_attention", error="上一次任务中断，供应商受理状态需核实。")
            approval = current.get("approval") or {}
            pin = approval.get("pin")
            proof = approval.get("review_ref")
            if pin and not proof:
                proof = legacy_proofs.get(_approval_id(catalog["story_id"], pin))
            current["selected_pin"] = pin if proof and review_heads.get(proof["asset_id"]) == proof else None
            if current["status"] == "approved" and not current["selected_pin"]:
                current.update(status="needs_attention", error="已选版本的自检记录已更新，需要按最新自检结果重新选用。")
            items.append(current)
    counts = dict(Counter(row["status"] for row in items))
    return {"items": items, "jobs": jobs, "style": style, "basis": basis, "counts": counts, "auto_dispatch": auto_dispatch,
            "production_started": bool(work.get("production_started") or any(j["kind"] == "asset_production" for j in jobs))}


@traced("studio.assets.work_validate", kind="validation", capture_output=False)
def validate_asset_order(service, pid, action, body):
    catalog = studio_asset_catalog(service, pid)
    if catalog.get("pending_script_ref"):
        raise ValueError("新版剧本已发布，请先更新对应的制作规格。")
    allowed = {"style"} if action == "asset_production" else {"pin"} if action == "asset_inspect" else {
        "subject_id", "purpose", "feedback", "asset_id", "parent_revision", "style"}
    if set(body) - allowed:
        raise ValueError("资产委托包含未支持的参数。")
    for field in ("style", "feedback"):
        if field in body and (not isinstance(body[field], str) or len(body[field]) > 3000):
            raise ValueError("制作要求与修改意见各限 3000 字。")
    result = dict(body, basis=_basis(catalog))
    if action == "asset_production":
        return result
    if action == "asset_inspect":
        original = body.get("pin")
        if not isinstance(original, dict):
            raise ValueError("请提供待审媒体的精确版本。")
        result.update(subject_id=original.get("subject_id"), purpose=original.get("purpose"))
    entity = next((e for e in catalog["entities"] if e["id"] == result.get("subject_id")), None)
    if not entity or result.get("purpose") not in entity["purposes"]:
        raise ValueError("此故事没有对应的资产对象和用途。")
    refs = [r for r in entity["references"] if r["purpose"] == result["purpose"]]
    if action == "asset_inspect":
        match = next((r for r in refs if _identity(r) == _identity(original)), None)
        if match is None:
            raise ValueError("此故事没有对应的待核验媒体版本。")
        head = next(r for r in refs if r["asset_id"] == match["asset_id"])
        _check("可重新核验的当前候选", _identity(head), _identity(match))
        result["pin"] = _pin(match)
    else:
        refs = [r for r in refs if r["asset_id"] == body.get("asset_id", r["asset_id"])]
        if body.get("asset_id") and not refs:
            raise ValueError("此对象没有该参考资产。")
        if refs:
            latest = refs[0]
            if "parent_revision" in body:
                _check("候选父版本", body["parent_revision"], latest["revision_id"])
            result.update(asset_id=latest["asset_id"], parent_revision=latest["revision_id"])
    return result


def _store_unit(service, pid, row):
    event("studio.assets.unit.transition", {k: row.get(k) for k in
          ("subject_id", "purpose", "status", "candidate", "selected_pin", "basis", "job_id", "trace_id", "error")}, kind="state")
    with service.lock:
        work = service.state["asset_work"][pid]
        work["items"][_key(row["subject_id"], row["purpose"])] = copy.deepcopy(row)
        service._save()


def _pin(entry):
    return {k: entry[k] for k in ("asset_id", "revision_id", "sha256", "media_sha256", "subject_id", "purpose")}


def _one(service, pid, job, catalog, item, order, style):
    from pianchang.asset_review import review_studio_candidate, run_reference_loop
    item.update(status="generating", review=None, candidate=None, error_span_id=None, job_id=job["id"], trace_id=job["trace_id"],
                feedback=order.get("feedback", ""), error=None)
    _store_unit(service, pid, item)
    request = {k: order[k] for k in ("asset_id", "parent_revision", "feedback") if k in order}
    request.update(subject_id=item["subject_id"], purpose=item["purpose"], style=style, max_attempts=2)
    def checkpoint(attempt):
        if attempt.get("review_ref") and not attempt.get("review"):
            ref = attempt["review_ref"]
            saved = CollabStore(catalog["store_path"]).get_asset(ref["asset_id"], ref["revision_id"])
            attempt = dict(attempt, review={**saved["content"], "review_ref":ref})
        item["attempts"].append(copy.deepcopy(attempt))
        item["candidate"] = attempt.get("pin")
        if (attempt.get("review") or {}).get("status") == "PASS":
            item.update(status="awaiting_review", review=attempt["review"], trace_id=attempt["review"]["trace_id"])
        _store_unit(service, pid, item)
    try:
        if order.get("pin"):
            pin = order["pin"]
            review = review_studio_candidate(service, pid, pin, request)
            checked = {"pin":pin, "review":review, "verdict":review["status"], "review_ref":review["review_ref"],
                       "trace_id":review["trace_id"]}
            checkpoint(checked)
            result = {"candidate":pin, "review":review, "attempts":[checked]}
            if job["kind"] == "asset_production" and review["status"] != "PASS" and not review.get("observation_error"):
                issues = [c for c in review["checks"] if c.get("required",True) and c["status"] != "PASS" and
                          (pin["purpose"] == "voice" or "candidate" in c.get("frame_ids", []))]
                if issues:
                    request.update(max_attempts=1)
                    result = run_reference_loop(service, pid, request, on_attempt=checkpoint)
        else:
            result = run_reference_loop(service, pid, request, on_attempt=checkpoint)
        for attempt in result["attempts"]:
            if not any(a.get("review_ref") == attempt.get("review_ref") and a.get("pin") == attempt.get("pin")
                       for a in item["attempts"]):
                item["attempts"].append(attempt)
        review = result.get("review") or {}
        item.update(candidate=result.get("candidate") or result.get("pin"), review=review,
                    status="awaiting_review" if review.get("status") == "PASS" else "needs_attention",
                    trace_id=review.get("trace_id", job["trace_id"]))
        if item["status"] == "awaiting_review":
            _store_unit(service, pid, item)
            receipt = select_verified_asset(service, pid, {**_identity(item["candidate"]), "review_ref": review["review_ref"]})
            item.update(status="approved", selected_pin=receipt["pin"], approval=receipt)
    except TraceError:
        raise
    except Exception as exc:
        item.update(status="needs_attention", error=service.sanitize(str(exc)), error_span_id=current_context()["span_id"])
    _store_unit(service, pid, item)


@traced("studio.assets.work_execute", kind="task", capture_inputs=False)
def run_asset_order(service, job, order):
    with service._asset_write_lock(job["project_id"]):
        return _run_asset_order(service, job, order)


def _run_asset_order(service, job, order):
    pid = job["project_id"]
    catalog = studio_asset_catalog(service, pid)
    _check("资产计划的剧本和设定", order["basis"], _basis(catalog))
    if catalog.get("pending_script_ref"):
        raise ValueError("当前制作用的剧本版本已经落后，请更新制作规格。")
    default_style = _style(service, pid)
    with service.lock:
        work = service.state.setdefault("asset_work", {}).setdefault(pid, {"items": {}})
        requested_style = order.get("style", "").strip()
        # Retaining a project's artistic preferences does not transfer any review.
        compatible = _matches_basis(service, catalog, work.get("basis"), uncertain=True)
        retained_style = work.get("style") if work.get("style_explicit") or compatible else None
        style = requested_style or retained_style or default_style
        if job["kind"] != "asset_production" and work.get("style") and style != work["style"]:
            raise ValueError("修改全剧风格请使用整批资产制作委托。")
        work.update(style=style, basis=order["basis"], style_explicit=bool(requested_style or work.get("style_explicit")))
        if job["kind"] == "asset_production":
            work["production_started"] = True
        service._save()
    snapshot = asset_work_state(service, pid, catalog)
    if job["kind"] == "asset_production":
        targets = [u for u in snapshot["items"] if u["status"] == "planned"]
    else:
        targets = [u for u in snapshot["items"] if (u["subject_id"], u["purpose"]) == (order["subject_id"], order["purpose"])]
    for index, unit in enumerate(targets):
        service._update(job, message=f"资产 {index+1}/{len(targets)} · {unit['subject_id']} · {unit['purpose']}")
        task = dict(order)
        if task.get("pin"):
            ref = task["pin"]
            head = CollabStore(catalog["store_path"]).get_asset(ref["asset_id"])
            _check("重新核验前的候选版本", _identity(ref), _identity(head))
        if job["kind"] == "asset_production":
            entity = next(e for e in catalog["entities"] if e["id"] == unit["subject_id"])
            previous = next((r for r in entity["references"] if r["purpose"] == unit["purpose"]), None)
            if previous:
                task.update(asset_id=previous["asset_id"], parent_revision=previous["revision_id"], pin=_pin(previous))
        with service.tracer.span("studio.assets.produce_item", kind="task", inputs={"project_id": pid,
                "subject_id": unit["subject_id"], "purpose": unit["purpose"], "job_id": job["id"]}) as production:
            _one(service, pid, job, catalog, unit, task, style)
            production.set_output({"status": unit["status"], "candidate": unit.get("candidate"), "error": unit.get("error")})
    state = asset_work_state(service, pid)
    affected = {_key(i["subject_id"],i["purpose"]) for i in targets}
    incomplete = any(u["status"] == "needs_attention" and
                     (job["kind"] == "asset_production" or _key(u["subject_id"],u["purpose"]) in affected)
                     for u in state["items"])
    result = {"counts": state["counts"], "processed": len(targets), "trace_id": job["trace_id"], "needs_attention": incomplete,
            "summary": f"已处理 {len(targets)} 项资产，{state['counts'].get('approved',0)} 项已选用，"
                       f"{state['counts'].get('needs_attention',0)} 项待处理。"}
    if incomplete:
        from pianchang.asset_diagnostics import asset_diagnostic
        names = {e["id"]: e["name"] for e in catalog["entities"]}
        issues = [asset_diagnostic(u, names.get(u["subject_id"]), span_id=u.get("error_span_id"))
                  for u in state["items"] if u["status"] == "needs_attention"
                  and (job["kind"] == "asset_production" or _key(u["subject_id"], u["purpose"]) in affected)]
        result["asset_diagnostics"] = {"project_id": pid, "summary": result["summary"], "items": issues}
        event("studio.assets.unresolved", result["asset_diagnostics"], kind="state")
    if job["kind"] != "asset_production" and len(targets) == 1:
        result.update(pin=targets[0]["candidate"], review=targets[0]["review"], status=targets[0]["status"])
    return result


def _approval_id(story, pin):
    return story + "/asset-approval/" + digest_json(_identity(pin))[:32]


def _head_identity(store, asset_id):
    with store._db() as db:  # read from the same transaction as the spec/receipt publication
        row = db.execute("SELECT asset_id,revision_id,sha256 FROM assets WHERE asset_id=? ORDER BY seq DESC LIMIT 1",
                         (asset_id,)).fetchone()
    return dict(row) if row else None


def _selection_id(story, pin):
    return story + "/asset-selection/" + digest_json(_identity(pin))[:32]


def _approval_review(store, approval):
    review = approval["content"].get("review_ref")
    if review:
        return review
    # Early approvals pinned the review in the dependency graph instead of
    # duplicating that reference inside the receipt.
    edge = next((d for d in approval["dependencies"] if d["role"] == "verification"), None)
    _check("选用记录关联的核验版本", True, edge is not None)
    return _identity(store.get_asset(edge["asset_id"], edge["revision_id"]))


@traced("studio.assets.approved_pins", kind="tool", capture_output=False)
def approved_reference_pins(service, pid):
    work = asset_work_state(service, pid)
    return [u["selected_pin"] for u in work["items"] if u["selected_pin"]]


@traced("studio.assets.check_approval", kind="validation", capture_output=False)
def check_reference_approval(service, pid, pin, *, catalog=None):
    catalog = catalog if catalog is not None else studio_asset_catalog(service, pid)
    _check("参考目录所属项目", pid, catalog["project_id"])
    store = CollabStore(catalog["store_path"])
    asset = store.get_asset(pin["asset_id"], pin["revision_id"])
    tracked = any(a.get("pin") == pin for u in _record(service, pid).get("items", {}).values()
                  for a in u["attempts"])
    if not (asset["content"].get("origin") or {}).get("review_required") and not tracked:
        return
    row = next((i for i in asset_work_state(service, pid, catalog)["items"]
                if (i["subject_id"], i["purpose"]) == (pin["subject_id"], pin["purpose"])), None)
    _check("当前选用的参考版本", pin, (row or {}).get("selected_pin"))
    decisions = store.revisions(_selection_id(catalog["story_id"], pin)) or store.revisions(_approval_id(catalog["story_id"], pin))
    if not decisions:
        _check("参考资产的选用记录", "approved", "pending")
    decision = decisions[-1]["content"]
    _check("批准的参考版本", pin, decision["pin"])
    matched = _matches_basis(service, catalog, decision["basis"], pin)
    event("studio.assets.approval_basis", {"recorded": decision["basis"], "current": _basis(catalog),
          "candidate": pin, "status": "PASS" if matched else "FAIL"}, kind="validation")
    _check("批准时的资产设定", True, matched)
    _check("批准时的风格", digest_json(_record(service,pid).get("style","")), decision["style_sha256"])
    approved_review = _approval_review(store, decisions[-1])
    current = store.get_asset(approved_review["asset_id"])
    _check("批准所依据的核验版本", approved_review, _identity(current))


@traced("studio.assets.check_video_ready", kind="validation", capture_output=False)
def check_video_asset_readiness(service, pid, project, *, catalog=None):
    catalog = catalog if catalog is not None else studio_asset_catalog(service, pid)
    _check("参考目录所属项目", pid, catalog["project_id"])
    state = asset_work_state(service, pid, catalog)
    if not state["production_started"]:
        return
    units = {(u["subject_id"],u["purpose"]):u for u in state["items"]}
    missing = []
    for shot in project["shots"]:
        subjects = {shot["scene_id"], *shot["entity_ids"]}
        for (subject, purpose), unit in units.items():
            if subject not in subjects:
                continue
            pin = unit["selected_pin"]
            if not pin or pin not in shot.get("required_refs", []):
                missing.append(f"{shot['id']} · {subject} · {purpose}")
    event("asset.production.readiness", {"path":"shots.required_refs", "expected":"all required approved pins",
          "actual":missing, "status":"FAIL" if missing else "PASS"}, kind="validation")
    if missing:
        raise ValueError("以下镜头缺少已选用的参考资产：" + "、".join(missing))


@traced("studio.assets.approve", kind="tool")
def approve_asset(service, pid, body):
    """Compatibility entry point. Acceptance always uses the agent's recorded evidence."""
    return select_verified_asset(service, pid, body)


@traced("studio.assets.select_verified", kind="tool", capture_inputs=False)
def select_verified_asset(service, pid, body):
    return _select_asset(service, pid, body, human=False)


@traced("studio.assets.manual_approval", kind="tool", capture_inputs=False)
def manually_approve_asset(service, pid, body):
    """Select the reviewed media by human decision while retaining its original checks."""
    return _select_asset(service, pid, body, human=True)


def _select_asset(service, pid, body, *, human):
    from pianchang.asset_review import review_asset_id, _brief, _candidate
    authority = "human" if human else "asset_agent"
    event("studio.assets.selection.request", {"project_id": pid, "authority": authority, "request": body}, kind="tool")
    if set(body) - {"review_ref"} != {"asset_id", "revision_id", "sha256"}:
        raise ValueError("请提供要选用候选的 ID、版本与哈希。")
    if human and (not isinstance(body.get("review_ref"), dict) or
                  set(body["review_ref"]) != {"asset_id", "revision_id", "sha256"}):
        raise ValueError("人工通过需要指定当前自检记录的 ID、版本与哈希。")
    media_ref = _identity(body)
    with service._asset_write_lock(pid):
        view = studio_project_for_video(service, pid)
        catalog = studio_asset_catalog(service, pid)
        if catalog.get("pending_script_ref"):
            raise ValueError("剧本已更新，请先适配新的制作规格。")
        item = next((u for u in asset_work_state(service,pid,catalog)["items"] if u.get("candidate")
                     and _identity(u["candidate"]) == media_ref), None)
        if human:
            prior_human = item and (item.get("approval") or {}).get("selection_mode") == "human"
            if not item or (item["status"] != "needs_attention" and not (item["status"] == "approved" and prior_human)):
                raise ValueError("此版本不再是当前需处理的候选，请刷新资产页。")
        elif not item or item["status"] not in {"awaiting_review", "approved"}:
            raise ValueError("仅可选用当前资产已通过自检的精确候选版本。")
        pin = item["candidate"]
        store = CollabStore(view["store_path"])
        _check("当前候选版本", media_ref, _identity(store.get_asset(pin["asset_id"])))
        review_ref = (item.get("review") or {}).get("review_ref")
        if not review_ref:
            raise ValueError("此候选缺少对应的自检记录，请刷新后核对素材。")
        if "review_ref" in body or review_ref["revision_id"] != "r1":
            _check("请求关联的核验版本", review_ref, body.get("review_ref"))
        review_asset = store.get_asset(review_ref["asset_id"], review_ref["revision_id"])
        _check("最新核验版本",review_ref,_identity(store.get_asset(review_ref["asset_id"])))
        _check("自验记录", review_ref, _identity(review_asset))
        _check("自验记录的资产", review_asset_id(pin), review_ref["asset_id"])
        _check("核验所属故事", view["story_id"], review_asset["story_id"])
        _check("核验类型", "reference_review", review_asset["kind"])
        proof = review_asset["content"]
        _check("自验记录对应的媒体", pin, proof.get("candidate"))
        required = [c for c in proof.get("checks", []) if c.get("required", True)]
        if human:
            _check("人工审看所依据的自检结论", True, proof.get("verdict") in {"FAIL", "UNKNOWN", "PASS"})
            _check("人工审看所依据的必检记录", True, bool(required))
        else:
            _check("自验结论", "PASS", proof.get("verdict"))
            _check("必检项全部通过", True, bool(required) and all(c.get("status") == "PASS" for c in required))
        _check("自验时的风格", item["style_sha256"], digest_json(proof["brief"]["shared_style"]))
        current_brief = _brief(view, {"subject_id":pin["subject_id"], "purpose":pin["purpose"]})
        for field in ("subject_id", "purpose", "canonical", "source_refs", "entity_version", "script",
                      "setup", "shots", "script_direction"):
            _check("核验使用的制作依据." + field, current_brief[field], proof["brief"].get(field))
        _check("核验使用的指定参考", current_brief["declared_refs"], proof["brief"].get("declared_refs", []))
        _candidate(store, view["story_id"], pin)  # verify blob even for a subject that has no shots
        asset_id = _selection_id(view["story_id"], pin)
        approvals = store.revisions(asset_id)
        previous = approvals[-1]["content"] if approvals else {}
        subjects_shots = [s for s in catalog["shots"] if pin["subject_id"] in [s["scene_id"], *s["entity_ids"]]]
        shots = [s["id"] for s in subjects_shots]
        replaced = (item.get("approval") or {}).get("pin") or item.get("previous_pin")
        bound = all(pin in s["required_refs"] and (replaced == pin or replaced not in s["required_refs"])
                    for s in subjects_shots)
        reuse = (bound and previous.get("authority", "asset_agent") == authority
                 and previous.get("shot_ids") == shots and previous.get("style_sha256") == item["style_sha256"]
                 and _approval_review(store, approvals[-1]) == review_ref
                 and _matches_basis(service, catalog, previous.get("basis"), pin))
        spec = view["spec"]
        prepared = None
        if shots and not bound:
            prepared = _prepare_binding(store, view["project"], story_id=view["story_id"], shot_id=shots, pins=[pin],
                spec_asset_id=spec["asset_id"], parent_revision=spec["revision_id"], script_ref=view["script_ref"],
                replace_pin=replaced if replaced != pin else None)
        # Media probes run outside the shared database's write transaction. Check the
        # exact inputs again under its write lock before publishing spec and receipt together.
        with store.transaction():
            _check("选用时的候选", media_ref, _identity(store.get_asset(pin["asset_id"])))
            _check("选用时的核验记录", review_ref, _identity(store.get_asset(review_ref["asset_id"])))
            expected_spec = spec if spec["revision_id"] else None
            _check("选用时的制作规格", expected_spec, _head_identity(store, spec["asset_id"]))
            script_ref = view["script_ref"]
            script_id = script_ref["asset_id"] if script_ref else view["story_id"] + "/script"
            _check("选用时的剧本", script_ref, _head_identity(store, script_id))
            if reuse:
                _check("选用时的既有记录", _identity(approvals[-1]), _identity(store.get_asset(asset_id)))
                receipt = previous
            else:
                if prepared is not None:
                    updated, dependencies = prepared
                    spec = _identity(store.publish_asset(spec["asset_id"], updated, kind="production_spec",
                        producer="production", story_id=view["story_id"], parent_revision=spec["revision_id"],
                        dependencies=dependencies))
                receipt = {"pin":pin,"shot_ids":shots,"spec":spec,"trace_id":current_context()["trace_id"],
                           "basis":item["basis"],"style_sha256":item["style_sha256"],"review_ref":review_ref,
                           "authority":authority, "previous_pin":replaced, "review_trace_id":proof.get("trace_id")}
                if human:
                    receipt.update(selection_mode="human", review_verdict=proof["verdict"],
                                   review_span_id=proof.get("span_id"))
                deps = [{"asset_id":pin["asset_id"],"revision_id":pin["revision_id"],"role":"approved_media"},
                        {"asset_id":review_ref["asset_id"],"revision_id":review_ref["revision_id"],"role":"verification"}]
                if spec.get("revision_id"):
                    deps.append({"asset_id":spec["asset_id"],"revision_id":spec["revision_id"],"role":"bound_spec"})
                # The asset agent owns this revision stream; each receipt names the actual decision maker.
                store.publish_asset(asset_id,receipt,kind="reference_selection",producer="asset_agent",story_id=view["story_id"],
                                    parent_revision=approvals[-1]["revision_id"] if approvals else None,
                                    correlation_id=view["run_id"],dependencies=deps)
        item.update(status="approved", selected_pin=pin, approval=receipt, error=None)
        _store_unit(service,pid,item)
        if human:
            event("studio.assets.human_approved", {"project_id": pid, "pin": pin, "review_ref": review_ref,
                  "original_verdict": proof["verdict"], "review_trace_id": proof.get("trace_id"),
                  "receipt_trace_id": receipt["trace_id"], "shot_ids": shots, "spec": receipt["spec"], "reused": reuse},
                  kind="state")
        return receipt
