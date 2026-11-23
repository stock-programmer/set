"""Independent, revision-bound video production and evidence-based incremental builds."""
from __future__ import annotations

import argparse
import copy
import json
import os
import uuid
from collections import OrderedDict
from pathlib import Path

from pianchang.acceptance import AcceptanceError, create_contract, file_sha256, observe, regression_plan, run_checks, verify_inspection
from pianchang.agent_loop import AgentLoop, Tool
from pianchang.bailian_observer import load_observer_config
from pianchang.bailian_text import BailianTextClient, TextError, load_text_config, routed_cache_identity, text_client_for
from pianchang.bailian_video import REFERENCE_MODELS, SIZES_26, build_request, config_for_shooting_plan, load_config, validate_reference_capabilities
from pianchang.episode_export import export_preview
from pianchang.collab_store import CollabStore
from pianchang.reference_assets import resolve_references
from pianchang.media_tools import inspect_media
from pianchang.review_dispatch import ReviewOutOfScope, require_unscoped_call
from pianchang.story_agent import StoryRunner
from pianchang.story_spec import digest_json, index_sources, normalize_script, validate_compilation, validate_script
from trace_system import TraceError, current_context, event, get_tracer, register_secret, span, traced
from pianchang.video_agent import VideoRunner, compile_shot, compile_project_previews


_impact_cache = OrderedDict()


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep temporary basenames short: full candidate hashes plus another UUID
    # exceeded Windows' legacy path limit before any provider submission.
    temporary = path.with_name(".w-" + uuid.uuid4().hex[:16] + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(get_tracer().sanitize(data), stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        from pianchang.atomic_files import replace_with_retry
        replace_with_retry(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _one_model_call(path, request, call):
    # The task directory is exclusively claimed by AgentLoop. Persist the request before dispatch.
    path.parent.mkdir(parents=True, exist_ok=True)
    fingerprint = digest_json(request)
    saved = _read(path) if path.exists() else None
    if saved is not None and saved["input_sha256"] == fingerprint and saved["status"] == "completed":
        event("video.collab.cached_model_result", {"path": str(path), "input_sha256": fingerprint,
              "new_network_request": False}, kind="cache")
        return saved["response"]
    require_unscoped_call()
    if saved is not None:
        raise ValueError("已有未确认的模型调用或输入变更，已停止重复调用：" + str(path))
    _write(path, {"status": "submitted_or_unknown", "input_sha256": fingerprint, "request": request})
    response = call()
    _write(path, {"status": "completed", "input_sha256": fingerprint, "request": request, "response": response})
    return response


def _checked_project(project, script):
    report = validate_compilation(project, script)
    if not report["ready_for_generation"]:
        issues = [i for i in report["issues"] if i.get("severity") == "error"]
        raise ValueError("制作工程与目标剧本未通过校验：" + str(issues[:5]))
    return project


def _objects(project, group):
    collection = project[group]
    return collection if isinstance(collection, dict) else {obj["id"]: obj for obj in collection}


def _refs(item):
    return {r["source_id"] for r in item.get("source_refs", [])}


def _input_record(project, shot, compiled=None):
    """Only consumed inputs; the whole-script version counter is intentionally outside the reuse key."""
    refs = [shot["scene_id"], *shot["entity_ids"]]
    shot_fields = {k: v for k, v in shot.items() if k != "input_versions"}
    compiled = compiled if compiled is not None else compile_shot(project, shot["id"])
    facts = set(shot["requires"]) | set(shot["expected_after"])
    events = {eid: _objects(project, "events")[eid] for eid in shot["event_ids"]}
    for action in events.values():
        facts.update(action["requires"])
        facts.update(action["changes"])
    for name in project["facts"]:
        if name.rsplit(".", 1)[0] in refs:
            facts.add(name)
    items = [shot, *events.values(), *(project["entities"][eid] for eid in refs),
             *(project["facts"][key] for key in sorted(facts))]
    sources = index_sources(project["script_source"]["script"])
    source_ids = sorted(set().union(*(_refs(item) for item in items)))
    basis = {"shot": shot_fields, "entities": {eid: project["entities"][eid] for eid in refs},
             "events": events, "facts": {key: project["facts"][key] for key in sorted(facts)},
             "sources": {key: {f: sources[key][f] for f in ("text", "episode_id", "scene_id")} for key in source_ids},
             "compiled_prompt": compiled["prompt"], "duration": compiled["duration"]}
    # Keep the historical fingerprint for shots without locked media.
    pins = shot_fields.pop("required_refs", [])
    if pins:
        basis["required_refs"] = pins
    return {"sha256": digest_json(basis), "components": {k: digest_json(v) for k, v in basis.items()},
            "source_ids": source_ids}


@traced("video.collab.impact", kind="validation")
def analyze_impact(project, base_project=None):
    cache_key = (get_tracer().store_id, digest_json(project), digest_json(base_project))
    if cache_key in _impact_cache:
        saved, source = _impact_cache[cache_key]
        event("video.collab.impact_cache_reused", {"project_sha256": cache_key[1], "base_sha256": cache_key[2],
              "source": source, "result_sha256": digest_json(saved), "new_model_call": False}, kind="cache")
        _impact_cache.move_to_end(cache_key)
        return copy.deepcopy(saved)
    current = _checked_project(project, project["script_source"]["script"])
    _, current_previews = compile_project_previews(current)
    previous_previews = {}
    previous_shots = {}
    old_sources = {}
    if base_project is not None:
        _checked_project(base_project, base_project["script_source"]["script"])
        previous_shots = _objects(base_project, "shots")
        old_sources = index_sources(base_project["script_source"]["script"])
        _, previous_previews = compile_project_previews(base_project)
    sources = index_sources(current["script_source"]["script"])
    changes = sorted(k for k in sources.keys() | old_sources.keys() if sources.get(k) != old_sources.get(k))
    rows, regenerate, reuse, edges = {}, [], [], set()
    for shot in current["shots"]:
        sid = shot["id"]
        target = _input_record(current, shot, current_previews[sid])
        base = _input_record(base_project, previous_shots[sid], previous_previews[sid]) if sid in previous_shots else None
        same = base is not None and target["sha256"] == base["sha256"]
        (reuse if same else regenerate).append(sid)
        previous_components = (base or {}).get("components", {})
        reasons = sorted(key for key in previous_components.keys() | target["components"].keys()
                         if previous_components.get(key) != target["components"].get(key))
        rows[sid] = {"action": "reuse" if same else "generate", "target": target, "base": base,
                     "changed_components": reasons,
                     "changed_sources": sorted(set(changes) & (set(target["source_ids"]) | set((base or {}).get("source_ids", [])))),
                     "check": {"path": f"shots.{sid}.dependency_sha256", "expected": (base or {}).get("sha256"),
                               "actual": target["sha256"], "status": "PASS" if same else "CHANGED"}}
        if not same:
            edges.update(tuple(edge) for edge in regression_plan(current, sid)["continuity_edges"])
    # Changes in ordering also introduce new adjacency obligations for unchanged shots.
    old_order = list(previous_shots)
    new_order = [s["id"] for s in current["shots"]]
    old_edges = set(zip(old_order, old_order[1:]))
    for left, right in zip(new_order, new_order[1:]):
        if (left, right) not in old_edges or left in regenerate or right in regenerate:
            edges.add((left, right))
    result = {"changed_sources": changes, "regenerate": regenerate, "reuse": reuse, "shots": rows,
            "removed_shots": sorted(previous_shots.keys() - set(new_order)),
            "recheck_edges": [list(edge) for edge in sorted(edges)],
            "recheck_shots": sorted(set(regenerate) | {s for edge in edges for s in edge})}
    _impact_cache[cache_key] = (copy.deepcopy(result), current_context())
    if len(_impact_cache) > 16:
        _impact_cache.popitem(last=False)
    return result


_EDITABLE = {"entities": {"canonical", "references"}, "facts": {"initial", "allowed", "mutable"},
             "events": {"reason", "requires", "changes"},
             "shots": {"description", "entity_ids", "scene_id", "requires", "expected_after", "event_ids", "duration_s"}}


@traced("video.collab.adapt_project", kind="tool", capture_inputs=False)
def adapt_project(base, script, client, *, call_path=None):
    """Incrementally adapt existing stable objects; structural edits use the full compiler."""
    previous_script = base["script_source"]["script"]
    old, new = index_sources(previous_script), index_sources(script)
    changed = sorted(k for k in old if old[k] != new.get(k))
    if old.keys() != new.keys() or any(new[k]["kind"] != "beat" for k in changed):
        return None
    project = copy.deepcopy(base)
    project.update(title=script["title"], script_version=script["script_version"],
                   script_source={"script": copy.deepcopy(script), "sha256": digest_json(script)})
    changed_objects = {}
    for group in _EDITABLE:
        for key, item in _objects(project, group).items():
            if _refs(item) & set(changed):
                changed_objects.setdefault(group, {})[key] = copy.deepcopy(item)
            for ref in item["source_refs"]:
                sid = ref["source_id"]
                if sid in changed and ref["quote"] not in new[sid]["text"]:
                    ref["quote"] = new[sid]["text"]
    if changed:
        messages = [{"role": "system", "content": "你是视频制作规格编辑。依据实际修订对已有稳定对象作必要调整；剧情、人物、镜头 ID 不可改。"
                     "若已有对象/状态模型不能表达新剧情，返回 {\"full_compile\":true}。否则返回 JSON {\"edits\":[{\"group\":\"shots\",\"id\":\"S003\",\"values\":{\"description\":\"完整的新镜头描述\"}}]}。"
                     "每个直接承载修改节拍的镜头必须更新 description。仅在剧情确实改变对象属性/状态时修改其他已有字段。正确保留固定事实、事件前置条件、连续性。不要写来源、ID、版本字段。"},
                    {"role": "user", "content": json.dumps({"source_changes": {k: {"before": old[k], "after": new[k]} for k in changed},
                        "affected_objects": changed_objects, "editable_fields": {g: sorted(v) for g, v in _EDITABLE.items()}}, ensure_ascii=False)}]
        client = text_client_for(client, "story.compile")
        route_identity = routed_cache_identity(client)
        request = {"model_route": route_identity, "messages": messages} if route_identity else messages
        response = (client.complete_json(messages) if call_path is None else
                    _one_model_call(call_path, request, lambda: client.complete_json(messages)))["data"]
        if not isinstance(response, dict) or response.get("full_compile") or not isinstance(response.get("edits"), list):
            return None
        edited = set()
        for patch in response["edits"]:
            if not isinstance(patch, dict):
                return None
            group, key, values = patch.get("group"), patch.get("id"), patch.get("values")
            if (not isinstance(group, str) or not isinstance(key, str) or key not in changed_objects.get(group, {}) or not isinstance(values, dict)
                    or not values or not values.keys() <= _EDITABLE[group]):
                return None
            item = _objects(project, group)[key]
            item.update(values)
            if group == "entities" and any(values[k] != changed_objects[group][key].get(k) for k in values):
                item["version"] = changed_objects[group][key]["version"] + 1
            if group == "shots" and values.get("description") != changed_objects[group][key]["description"] and "description" in values:
                edited.add(key)
        if set(changed_objects.get("shots", {})) - edited:
            return None
    for shot in project["shots"]:
        shot["input_versions"] = {"script": script["script_version"], **{key: project["entities"][key]["version"]
                                 for key in [shot["scene_id"], *shot["entity_ids"]] if key in project["entities"]}}
    report = validate_compilation(project, script)
    return project if report["ready_for_generation"] else None


def _schema(shot=False):
    return {"type": "object", "properties": {"shot_id": {"type": "string", "minLength": 1, "maxLength": 128}} if shot else {},
            "required": ["shot_id"] if shot else [], "additionalProperties": False}


def _evidence_match(path, expected):
    return Path(path).is_file() and file_sha256(path) == expected


def _model_result(result):
    """Keep the exact result on disk and in Trace. Send its fingerprint and decision-relevant fields to the model."""
    summary = {key: result[key] for key in ("shot_id", "status", "operation", "media_sha256", "task_id",
               "reason", "regenerate", "reuse", "recheck_edges", "preview", "stage", "next_action") if key in result}
    if "acceptance" in result:
        summary["acceptance"] = {**result["acceptance"], "issues":[{k:r[k] for k in ("shot_id","criterion_id","status")}
                                             for r in result["acceptance"].get("issues",[])]}
    if "reports" in result:
        summary["checks"] = {sid: {"status": row["status"], "unresolved": row["unresolved"]}
                             for sid, row in result["reports"].items()}
    return {**summary, "result_sha256": digest_json(result)}


def _matches_tool_output(receipts, tool, result):
    return any(entry["tool"] == tool and entry["result"] in (result, _model_result(result)) for entry in receipts)


@traced("video.collab.shooting_plan", kind="validation", capture_inputs=False, capture_output=False)
def _production_impact(project, previous, plan, partial=None):
    impact = analyze_impact(project, previous["project"] if previous else None)
    if plan is None:
        return impact
    if "project_sha256" in plan:
        actual = digest_json(project)
        event("video.collab.approved_spec", {"path": "project_sha256", "expected": plan["project_sha256"],
              "actual": actual, "status": "PASS" if plan["project_sha256"] == actual else "FAIL"}, kind="validation")
        if plan["project_sha256"] != actual:
            raise ValueError("已批准的制作规格与当前输入版本不一致。")
    ids = [shot["id"] for shot in project["shots"]]
    if partial is not None and plan.get("continuation"):
        if partial["project"] != project or set(partial["shots"]) | set(partial["missing_shots"]) != set(ids):
            raise ValueError("恢复素材和待制作镜头与当前规格不符。")
        fresh, retakes, reused = ([] if plan.get("repair_existing_only") else partial["missing_shots"]), set(), set(partial["shots"])
    elif partial is not None:
        if partial["project"] != project or set(partial["shots"]) | set(partial["failed_shots"]) != set(ids):
            raise ValueError("部分制作记录与当前制作规格不符。")
        fresh, retakes, reused = [], set(partial["failed_shots"]), set(partial["shots"])
    else:
        normal = set(impact["regenerate"])
        fresh = [sid for sid in ids if sid in normal and impact["shots"][sid]["base"] is None]
        retakes = normal - set(fresh)
        requested = set(plan["retake_shots"]) - retakes
        proposal = plan.get("approval_proposal")
        if requested and (not proposal or set(proposal.get("shot_ids", [])) != requested):
            raise ValueError("额外重拍的镜头不属于本次批准的提案。")
        if requested and any((previous or {}).get("reports", {}).get(sid, {}).get("status") != "FAIL" for sid in requested):
            raise ValueError("额外重拍需要前一版对应镜头的失败验收记录。")
        retakes.update(requested)
        reused = set(ids) - retakes - set(fresh)
    expected = {"new_shots": list(fresh), "retake_shots": [sid for sid in ids if sid in retakes],
                "reuse_shots": [sid for sid in ids if sid in reused]}
    checks = [{"path": key, "expected": value, "actual": plan.get(key),
               "status": "PASS" if value == plan.get(key) else "FAIL"} for key, value in expected.items()]
    event("video.collab.shooting_plan_check", {"plan_id": plan["plan_id"], "checks": checks}, kind="validation")
    if any(check["status"] != "PASS" for check in checks):
        raise ValueError("当前镜头依赖与已批准的拍摄计划不一致，请重新查看计划。")
    impact["regenerate"] = [sid for sid in ids if sid not in reused and sid not in plan.get("deferred_shots", [])]
    impact["reuse"] = expected["reuse_shots"]
    edges = {tuple(edge) for edge in impact["recheck_edges"] if partial is None}
    for sid in impact["regenerate"]:
        edges.update(tuple(edge) for edge in regression_plan(project, sid)["continuity_edges"])
    impact["recheck_edges"] = [list(edge) for edge in sorted(edges)]
    impact["recheck_shots"] = sorted(set(impact["regenerate"]) | {s for edge in edges for s in edge})
    for sid, row in impact["shots"].items():
        row["action"] = "reuse" if sid in reused else "generate"
    return impact


def _validated_media(clip, *, require_origin=False):
    if clip.get("media_lineage"):
        from pianchang.video_repair import validate_lineage
        job = validate_lineage(clip["media_lineage"], output_path=clip["media_path"], output_sha256=clip["media_sha256"])
        if (digest_json(job) != clip["generation_job_sha256"] or job["task_id"] != clip["task_id"]
                or job["metadata"]["shot_id"] != clip["shot_id"]
                or job["metadata"].get("dependency_sha256") != clip["dependency_sha256"]):
            raise ValueError("编辑素材与原始生成任务不符。")
        return job
    job = _read(clip["manifest_path"])
    signature = {key: job[key] for key in ("base_url", "request", "metadata")}
    pairs = {"job.task_id": (clip["task_id"], job.get("task_id")), "job.status": ("SUCCEEDED", job.get("status")),
             "job.download_sha256": (clip["media_sha256"], job.get("download_sha256")),
             "job.metadata.shot_id": (clip["shot_id"], job["metadata"].get("shot_id")),
             "job.sha256": (clip["generation_job_sha256"], digest_json(job)),
             "job.request_sha256": (digest_json(signature), job.get("request_sha256")),
             "file.sha256": (clip["media_sha256"], file_sha256(clip["media_path"]) if Path(clip["media_path"]).is_file() else None)}
    if require_origin:
        pairs["job.metadata.dependency_sha256"] = (clip["dependency_sha256"], job["metadata"].get("dependency_sha256"))
        pairs["job.metadata.script_asset_ref"] = (clip["origin_script_ref"], job["metadata"].get("script_asset_ref"))
    if clip.get("required_refs") or job["metadata"].get("required_refs") or clip.get("reference_receipt"):
        from pianchang.reference_usage import verify_reference_exchange
        from pianchang.video_agent import verify_reference_receipt
        verified = verify_reference_receipt(job)
        pairs["candidate.required_refs"] = (clip.get("required_refs"), job["metadata"].get("required_refs"))
        pairs["candidate.reference_receipt"] = (job.get("reference_receipt"), clip.get("reference_receipt"))
        receipt = job.get("reference_receipt") or {}
        pairs["candidate.submitted_refs"] = (receipt.get("submitted_refs"), clip.get("submitted_refs"))
        pairs["candidate.provider_status"] = ("ACCEPTED", receipt.get("status"))
        pairs["candidate.reference_evidence_valid"] = (True, verified["status"] == "PASS")
        if verified["status"] == "PASS":
            exchange = verify_reference_exchange(job, receipt, receipt.get("trace_id"))
            pairs["candidate.confirmed_reference_request"] = ("MATCHED_RESPONSE", exchange["status"])
    checks = [{"path": path, "expected": expected, "actual": actual, "status": "PASS" if expected == actual else "FAIL"}
              for path, (expected, actual) in pairs.items()]
    event("video.collab.media_binding", {"shot_id": clip["shot_id"], "checks": checks}, kind="validation")
    if any(check["status"] != "PASS" for check in checks):
        raise ValueError("源视频、依赖或生成任务与选用记录不一致：" + clip["shot_id"])
    return job


def _manual_approvals(workspace):
    if hasattr(workspace, "manual_approvals"):
        return workspace.manual_approvals
    request_id = getattr(workspace, "request_id", None)
    if not request_id or workspace.directory.name != "video-spec-" + request_id:
        return {}
    from pianchang.shot_approval import read_approvals
    return read_approvals(workspace.directory, workspace.project, workspace.shots, workspace.reports,
                          project_id=(workspace.shooting_plan or {}).get("project_id"))


class VideoWorkspace:
    def __init__(self, *, script, script_ref, directory, text_config, video_config, template_project, previous,
                 decision_client, vision_config, progress, inbound_message=None, reference_store=None, story_id=None,
                 register_specs=None, shooting_plan=None, partial=None):
        self.script, self.ref = script, dict(script_ref)
        self.directory = Path(directory).resolve()
        self.text_config, self.video_config, self.vision_config = text_config, video_config, vision_config
        self.template, self.previous = template_project, previous
        self.decision_client, self.progress = decision_client, progress
        self.inbound = copy.deepcopy(inbound_message)
        self.request_id = (inbound_message or {}).get("message_id")
        self.reference_store, self.story_id, self.register_specs = reference_store, story_id, register_specs
        self.shooting_plan, self.partial = shooting_plan, partial
        self.defer_content_review = bool((shooting_plan or {}).get("repair_existing_only"))
        self.failed_shots = {}
        self.reference_specs = {}
        self.project = self.impact = self.preview = None
        self.shots, self.reports = {}, {}
        from pianchang.video_repair import VideoRepair
        self.repair = VideoRepair(self)
        context_result = _model_result if shooting_plan is not None else None
        self.tools = [Tool("plan_production", "读取固定剧本，准备或修订分镜，分析实际来源与状态依赖，返回需生成、可复用和需复查的镜头。先调用。", _schema(), self.plan, context_result=context_result),
                      Tool("generate_shot", "按冻结计划提交一个生成任务并等待视频。有 creative_shots 时，同一任务在一段视频中依序拍摄所有创作分镜，只占一次请求；首拍和重拍共享委托总额度。", _schema(True), self.generate, effect="video", preflight=self.require_reference_inputs, context_result=context_result),
                      Tool("reuse_shot", "对复用计划中的镜头核对源任务、文件哈希与新旧依赖，绑定旧媒体至当前版本。", _schema(True), self.reuse, effect="write", context_result=context_result),
                      Tool("review_and_export", "所有镜头就绪后自动验收；有必过失败或未知项则返回修复问题，仅全部通过后导出人工预览。", _schema(), self.review_and_export, effect="write", context_result=context_result),
                      *self.repair.tools]
        if self.reference_store and (self.story_id or (self.template or {}).get("reference_story_id")):
            self.tools.extend([
                Tool("read_reference_inputs","读取每镜头兼容输入编排、审核状态及可选策略。",_schema(True),lambda shot_id:self.reference_inputs(shot_id).read(shot_id)),
                Tool("prepare_reference_inputs","原参考超限时构建有精确来源映射的参考板并独立核验；失败换strategy。不能更改剧本或删除必需参考。",
                    {"type":"object","properties":{"shot_id":{"type":"string"},"strategy":{"type":"string","enum":["identity_first","balanced_boards"]}},"required":["shot_id"]},
                    lambda shot_id,strategy="identity_first":self.reference_inputs(shot_id).prepare(shot_id,strategy),effect="write")])

    def reference_inputs(self, shot_id):
        from pianchang.reference_inputs import ReferenceInputs
        project=self.project or self.template
        if project is None:raise ValueError("先plan_production，固定制作规格后再编排参考。")
        return ReferenceInputs(store=self.reference_store,story_id=project.get("reference_story_id") or self.story_id,
            project=project,directory=self.directory/"reference-inputs",config=self.video_config_for(shot_id),
            vision_config=self.vision_config,reserve_review=getattr(self,"reserve_reference_review",None))

    @traced("video.collab.require_reference_inputs",kind="validation",capture_inputs=False)
    def require_reference_inputs(self,shot_id):
        if getattr(self, "autonomy", None):
            blocker = self.autonomy.blocked.get(shot_id, {})
            if blocker.get("kind") == "submission_rejected":
                raise ValueError(blocker["reason"])
        if not self._required(shot_id):return
        state=self.reference_inputs(shot_id).read(shot_id)
        if not state["ready"]:raise ValueError("镜头 "+shot_id+" 的参考输入未核验，请先prepare_reference_inputs；当前未提交视频、未预留生成次数。"+json.dumps(state,ensure_ascii=False))

    def _progress(self, summary):
        if self.progress:
            self.progress({"phase": "production", "summary": summary})

    @property
    def reuses_entire_continuation(self):
        from pianchang.video_autonomy import all_reused_continuation
        return bool(self.template and self.partial and all_reused_continuation(self.shooting_plan)
                    and set(self.shooting_plan["reuse_shots"]) == {shot["id"] for shot in self.template["shots"]})

    @traced("video.collab.prepare_reused_clips", kind="task", capture_inputs=False)
    def prepare_reused_clips(self):
        """Bind approved local inputs even when the first Pi decision cannot run."""
        if not self.reuses_entire_continuation:
            raise ValueError("此准备步骤需要可核对全部已有镜头的恢复委托。")
        if (self.directory / "project.json").is_file() and _read(self.directory / "project.json") != self.template:
            raise ValueError("恢复工程与冻结委托不符。")
        self.plan()
        clips, reports = {}, {}
        for sid in self.shooting_plan["reuse_shots"]:
            clip = self._reused_clip(sid)
            clips[sid] = clip
            record = self._continued_review(sid, clip)
            if record:
                self._verify_cached_review(sid, clip, record)
                reports[sid] = record
        outputs = (("clips.json", clips), ("reports.json", reports))
        for name, values in outputs:
            path = self.directory / name
            if path.exists():
                if any(values.get(sid) != saved for sid, saved in _read(path).items()):
                    raise ValueError("已有候选或报告与恢复来源不一致：" + name)
        for name, values in outputs:
            if values:
                _write(self.directory / name, values)
        self.shots, self.reports = clips, reports
        autonomy = getattr(self, "autonomy", None)
        if autonomy is not None:
            autonomy.defer_new_clarifications_once = True
        return {"restored_shots": list(clips), "report_sha256": {s: r["report_sha256"] for s, r in reports.items()},
                "new_video_posts": 0}

    @traced("video.collab.prepare_existing", kind="tool", capture_inputs=False)
    def prepare_existing(self):
        """Restore verified inputs after the loop's exclusive claim, before model decisions."""
        if not (self.shooting_plan or {}).get("repair_existing_only") or self.partial is None:
            raise ValueError("此启动恢复只适用于明确的已有视频修复委托。")
        self.plan()
        for sid in self.shooting_plan["reuse_shots"]:
            self._progress("正在载入已有视频与原验收证据 " + sid)
            self.reuse(sid)
        return {"scope":"existing_video_repair", "restored_shots":list(self.shots),
                "report_sha256":{sid:r["report_sha256"] for sid,r in self.reports.items()},
                "deferred_shots":self.shooting_plan.get("deferred_shots", []),
                "video_submissions":0,"next_action":"读取未通过项的摘要，直接选择定向修复工具。"}

    @traced("video.collab.plan", kind="tool")
    def plan(self):
        if self.project is not None:
            return self.impact
        base = self.previous["project"] if self.previous else None
        if self.previous and self.previous["script_ref"]["asset_id"] != self.ref["asset_id"]:
            raise ValueError("前版制作结果属于另一个剧本资产。")
        project = None
        if self.template is not None:
            # The explicit target specification can change reference versions even when the script is identical.
            project = copy.deepcopy(self.template)
        elif base is not None:
            _checked_project(base, base["script_source"]["script"])
            project = adapt_project(base, self.script, self.decision_client, call_path=self.directory / "adapt-call.json")
        if project is None:
            summary = StoryRunner(self.text_config, client=self.decision_client).compile(self.script, self.directory / "compiler", max_repairs=2)
            if not summary.get("ready_for_generation"):
                raise ValueError("分镜编译失败，检查报告：" + summary["report_path"])
            project = _read(summary["project_path"])
            if base is not None:
                locked = {sid: shot["required_refs"] for sid, shot in _objects(base, "shots").items() if shot.get("required_refs")}
                if locked.keys() - _objects(project, "shots").keys():
                    raise ValueError("重新编译改变了已锁定参考的镜头 ID，需要明确绑定新的制作规格。")
                for shot in project["shots"]:
                    if shot["id"] in locked:
                        shot["required_refs"] = copy.deepcopy(locked[shot["id"]])
                if locked and "reference_story_id" in base:
                    project["reference_story_id"] = base["reference_story_id"]
        checked = _checked_project(project, self.script)
        impact = _production_impact(checked, self.previous, self.shooting_plan, self.partial)
        impact_path = self.directory / "impact.json"
        if self.reuses_entire_continuation and impact_path.is_file() and _read(impact_path) != impact:
            raise ValueError("现有镜头依赖与冻结委托不符。")
        if self.register_specs:
            self.reference_specs = self.register_specs(checked)
        for shot in checked["shots"]:
            if shot["id"] in (self.shooting_plan or {}).get("deferred_shots", []):
                continue
            pins = shot.get("required_refs", [])
            if pins:
                if self.reference_store is None:
                    raise ValueError("镜头要求已固定的参考素材，请指定 --reference-store。")
                from pianchang.reference_inputs import plan_reference_inputs
                plan_reference_inputs(checked,shot["id"],config_for_shooting_plan(self.video_config,pins,self.shooting_plan))
                resolve_references(self.reference_store, pins, story_id=checked.get("reference_story_id") or self.story_id)
        _write(self.directory / "project.json", checked)
        _write(self.directory / "impact.json", impact)
        self.project, self.impact = checked, impact
        self._progress(f"生成 {impact['regenerate']}，复用 {impact['reuse']}，复查衔接 {impact['recheck_edges']}")
        return impact

    @traced("video.collab.snapshot", kind="tool")
    def snapshot(self):
        acceptance = self.acceptance_gate(validate=False) if self.project else None
        if acceptance:
            acceptance = {**acceptance, "issues": [{k: r[k] for k in ("shot_id", "criterion_id", "status")} for r in acceptance["issues"]]}
        consumed = {h["repair_id"] for h in self.repair.history if h.get("status") != "local_failure_before_post"}
        return {"agent": "video", "script_ref": self.ref, "script_sha256": digest_json(self.script),
                "received_production_request": (self.inbound if not self.shooting_plan or self.project is None
                                                else {"message_id": (self.inbound or {}).get("message_id")}),
                "project_ready": self.project is not None, "generated_or_reused": sorted(self.shots),
                "generate_needed": [s for s in (self.impact or {}).get("regenerate", []) if s not in self.shots and s not in self.failed_shots],
                "confirmed_failed_shots": sorted(self.failed_shots),
                "reuse_needed": [s for s in (self.impact or {}).get("reuse", []) if s not in self.shots],
                "review_and_export_done": self.preview is not None,
                "acceptance": acceptance,
                "repair_plans": [{k: p[k] for k in ("repair_id", "shot_id", "criterion_ids", "candidate_sha256")}
                                 for p in self.repair.plans.values() if p["repair_id"] not in consumed],
                "repair_history": [{k: h[k] for k in ("repair_id", "shot_id", "strategy", "status")} for h in self.repair.history[-1:]],
                "completion_scope": "自动修复并复验，所有必过条件PASS后才交付人工预览。FAIL或UNKNOWN均未完成。"}

    def _action(self, shot_id, expected):
        if self.impact is None or shot_id not in self.impact[expected]:
            raise ValueError(f"{shot_id} 不属于已验证计划 {expected}。")

    def operation_key(self, shot_id, repair=None):
        return digest_json({"order":str(self.directory),"shot_id":shot_id,
                            "repair_id":(repair or {}).get("repair_id")})

    def video_config_for(self, shot_id):
        pins = _objects(self.project or self.template, "shots")[shot_id].get("required_refs", [])
        return config_for_shooting_plan(self.video_config, pins, self.shooting_plan)

    @traced("video.collab.generate", kind="tool")
    def generate(self, shot_id, *, repair=None):
        if repair is None:
            self._action(shot_id, "regenerate")
        if shot_id in self.shots and repair is None:
            return self.shots[shot_id]
        compiled = compile_shot(self.project, shot_id)
        if repair is not None:
            compiled["prompt"] += ("\n本次定向修复（原剧本、角色参考、动作和起止条件全部保留）：\n" + repair["instruction"])
        metadata = compiled["metadata"]
        dependency = self.impact["shots"][shot_id]["target"]["sha256"]
        metadata.update(script_asset_ref=self.ref, dependency_sha256=dependency)
        metadata["execution_key"] = self.operation_key(shot_id,repair)
        if repair is not None:
            metadata["acceptance_repair"] = {k: repair[k] for k in
                ("repair_id", "candidate_sha256", "report_sha256", "criterion_ids", "instruction")}
            if repair.get("strategy_design"):
                metadata["acceptance_repair"]["strategy_design"] = repair["strategy_design"]
        if self.shooting_plan is not None:
            # A separately approved retake needs a distinct candidate. Retries of the same order keep the same key.
            metadata["shooting_plan_id"] = self.shooting_plan["plan_id"]
        pins = self._required(shot_id)
        config = self.video_config_for(shot_id)
        resolved = []
        if pins:
            from pianchang.reference_transport import prepare_shot_request
            scope = self.project.get("reference_story_id") or self.story_id
            metadata.update(story_id=self.story_id or scope, reference_story_id=scope, required_refs=pins)
            if shot_id in self.reference_specs:
                metadata["reference_spec"] = self.reference_specs[shot_id]
            prepared=self.reference_inputs(shot_id).read(shot_id)
            if not prepared["ready"]:raise ValueError("先prepare_reference_inputs核验模型输入。")
            if prepared["selected"].get("bundle"):metadata["reference_bundle"]=prepared["selected"]["bundle"]
            request, resolved = prepare_shot_request(config, compiled, store=self.reference_store, story_id=scope)
        else:
            request = build_request(config, compiled["prompt"], duration=compiled["duration"])
        self._progress(f"正在生成 {shot_id}")
        runner = VideoRunner(config)
        submitted = runner.submit(request, metadata, **({"resolved_refs": resolved} if pins else {}))
        self._progress(f"{shot_id} 视频任务 {submitted['task_id']}，等待视频")
        try:
            generated = runner.resume(submitted["manifest_path"], wait=True)
        except TraceError:
            raise
        except Exception:
            if self.shooting_plan is None:
                raise
            job = _read(submitted["manifest_path"])
            response = (job.get("provider_response") or {}).get("output") or {}
            status = job.get("status")
            if (status not in {"FAILED", "CANCELED", "CANCELLED"} or response.get("task_status") != status
                    or not job.get("task_id") or job["task_id"] != submitted["task_id"]):
                raise
            failure = {"shot_id": shot_id, "task_id": job["task_id"], "status": status,
                       "reason": str(response.get("message") or response.get("code") or status),
                       "manifest_path": submitted["manifest_path"], "manifest_sha256": digest_json(job)}
            event("video.collab.confirmed_generation_failure", failure, kind="state")
            self.failed_shots[shot_id] = failure
            return failure
        return self.adopt_generated(shot_id,generated,repair=repair)

    @traced("video.collab.adopt_generated", kind="tool", capture_inputs=False)
    def adopt_generated(self, shot_id, generated, *, repair=None):
        dependency = self.impact["shots"][shot_id]["target"]["sha256"]
        pins = self._required(shot_id)
        if generated.get("status") != "SUCCEEDED" or not generated.get("local_video_path"):
            raise ValueError("生成任务仍未下载：" + generated["manifest_path"])
        clip = {"shot_id": shot_id, "operation": "generated", "origin_script_ref": self.ref, "script_ref": self.ref,
                "task_id": generated["task_id"], "manifest_path": generated["manifest_path"],
                "generation_job_sha256": digest_json(_read(generated["manifest_path"])), "media_path": generated["local_video_path"],
                "media_sha256": generated["download_sha256"], "dependency_sha256": dependency}
        if repair is not None:
            clip["repair_id"] = repair["repair_id"]
        if pins:
            job = _read(generated["manifest_path"])
            clip.update(required_refs=copy.deepcopy(pins), submitted_refs=job["reference_receipt"]["submitted_refs"],
                        reference_receipt=job["reference_receipt"])
        _validated_media(clip, require_origin=True)
        self.shots[shot_id] = clip
        self.failed_shots.pop(shot_id, None)
        self.preview = None
        if repair is not None:
            self.reports.pop(shot_id, None)
        _write(self.directory / "clips.json", self.shots)
        self.review_shot(shot_id)
        return clip

    def _required(self, shot_id):
        return _objects(self.project, "shots")[shot_id].get("required_refs", [])

    def retake_pause(self, record=None):
        if record and not getattr(self, "autonomy", None):
            failures = {h["shot_id"]: h["retake_failure"]["summary"] for h in self.repair.history if h.get("retake_failure")}
            if failures and self.stop_check(record)["allowed"]:
                unresolved = {r["shot_id"] for r in self.acceptance_gate(validate=False)["issues"]}
                messages = [message for sid, message in failures.items() if sid in unresolved]
                if messages:
                    return {"reason": "acceptance_retake_limit", "summary": "\n".join(messages)}
        if (self.shooting_plan or {}).get("acceptance_loop"):
            return None
        if self.failed_shots and set(self.shots) | set(self.failed_shots) == _objects(self.project, "shots").keys():
            return "以下镜头的云端任务已经失败。额外重拍需要批准：" + "、".join(self.failed_shots)

    def _reused_clip(self, shot_id):
        self._action(shot_id, "reuse")
        source = self.partial or self.previous
        base = source["shots"][shot_id]
        current = self.impact["shots"][shot_id]["target"]["sha256"]
        if base["shot_id"] != shot_id or base["dependency_sha256"] != current or base["script_ref"] != source["script_ref"]:
            raise ValueError("旧素材输入版本或依赖指纹不一致。")
        _validated_media(base, require_origin=True)
        if base.get("required_refs", []) != self._required(shot_id):
            raise ValueError("旧素材未提供当前镜头要求的参考版本，不能复用。")
        clip = {**base, "operation": "reused", "reused_from": source["script_ref"], "script_ref": self.ref}
        clip.pop("partial_source", None)
        clip.pop("continuation_source", None)
        if self.partial is not None:
            key = "continuation" if self.shooting_plan.get("continuation") else "partial"
            clip[key + "_source"] = self.shooting_plan[key]
        return clip

    def _continued_review(self, shot_id, clip):
        if self.partial is not None and self.shooting_plan.get("continuation"):
            source_dir = self.directory.parent / ("video-spec-" + self.shooting_plan["continuation"]["request_id"])
            source_dirs = [source_dir]
            ancestor = self.partial.get("source", {}).get("prior_continuation")
            while ancestor:
                ancestor_dir = self.directory.parent / ("video-spec-" + uuid.UUID(ancestor["request_id"]).hex)
                if ancestor_dir in source_dirs or len(source_dirs) >= 12:
                    raise ValueError("历史恢复链重复或过长。")
                sha = ancestor["sha256"]
                if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
                    raise ValueError("历史恢复记录指纹无效。")
                checkpoint = _read(ancestor_dir / ("resume-" + sha[:16] + ".json"))
                actual = digest_json(checkpoint["source"])
                event("video.collab.review_ancestor_checked", {"path":str(ancestor_dir), "expected":sha,
                      "actual":actual, "status":"PASS" if actual == sha else "FAIL"}, kind="validation")
                if actual != sha or checkpoint["project"] != self.project:
                    raise ValueError("历史恢复记录与当前冻结工程不符。")
                source_dirs.append(ancestor_dir)
                ancestor = checkpoint["source"].get("prior_continuation")
            for source_dir in source_dirs:
                report_index = source_dir / "reports.json"
                if not report_index.is_file():
                    continue
                record = _read(report_index).get(shot_id)
                if record:
                    report = _read(record["report_path"])
                    if digest_json(report) != record["report_sha256"]:
                        raise ValueError("历史自动验收报告哈希不符。")
                    if (report.get("project_sha256") == digest_json(self.project)
                            and report.get("media_sha256") == clip["media_sha256"]
                            and report.get("generation_job_sha256") in (None, clip["generation_job_sha256"])):
                        event("video.collab.continued_review", {"shot_id": shot_id,
                              "report_sha256": record["report_sha256"], "new_model_call": False}, kind="cache")
                        return record
        return None

    @traced("video.collab.reuse", kind="tool")
    def reuse(self, shot_id):
        clip = self._reused_clip(shot_id)
        self.shots[shot_id] = clip
        _write(self.directory / "clips.json", self.shots)
        record = self._continued_review(shot_id, clip)
        if record:
            self.reports[shot_id] = record
        self.review_shot(shot_id)
        if (self.defer_content_review and (self.shooting_plan or {}).get("repair_existing_only")
                and set(self.shooting_plan["reuse_shots"]) <= self.shots.keys()):
            self.defer_content_review = False
            if self.vision_config is not None and self.reference_store is not None:
                from pianchang.content_review import run_content_reviews
                run_content_reviews(self)
        return clip

    def _verify_cached_review(self, shot_id, clip, record):
        report = _read(record["report_path"])
        inspection = _read(record["inspection_path"])
        verify_inspection(inspection)
        valid = (digest_json(report) == record["report_sha256"]
                 and report["inspection_sha256"] == digest_json(inspection)
                 and report["project_sha256"] == digest_json(self.project)
                 and report["media_sha256"] == clip["media_sha256"]
                 and report["shot_id"] == shot_id)
        event("video.collab.review_cache_check", {"path": shot_id, "expected": True,
              "actual": valid, "status": "PASS" if valid else "FAIL"}, kind="validation")
        if not valid:
            raise ValueError("已有自动验收与当前素材不符：" + shot_id)

    @traced("video.collab.review_shot", kind="validation", capture_inputs=False)
    def review_shot(self, shot_id):
        """Persist each conclusion before moving on to another generation."""
        if shot_id not in self.shots:
            raise ValueError("镜头素材尚未就绪：" + shot_id)
        saved = _read(self.directory / "reports.json") if (self.directory / "reports.json").is_file() else {}
        record = self.reports.get(shot_id) or saved.get(shot_id)
        if record and self.shots[shot_id].get("repair_id"):
            old_report = _read(record["report_path"])
            if digest_json(old_report) != record["report_sha256"]:
                raise ValueError("原验收报告哈希不符。")
            if (old_report["media_sha256"] != self.shots[shot_id]["media_sha256"]
                    or old_report.get("generation_job_sha256") not in (None, self.shots[shot_id]["generation_job_sha256"])):
                record = None
        if record:
            clip = self.shots[shot_id]
            _validated_media(clip, require_origin=True)
            self._verify_cached_review(shot_id, clip, record)
        else:
            self._progress("正在自动验收 " + shot_id)
            record = {**self._review(shot_id), "trace_id": current_context()["trace_id"]}
        self.reports[shot_id] = record
        _write(self.directory / "reports.json", {**saved, **self.reports})
        if self.vision_config is not None and self.reference_store is not None and not getattr(self, "defer_content_review", False):
            from pianchang.content_review import run_content_reviews
            run_content_reviews(self)
        return self.reports[shot_id]

    @traced("video.collab.review_media", kind="validation", capture_inputs=False)
    def _review(self, shot_id, *, historical_folder=None, prior_job_sha256=None):
        clip = self.shots[shot_id]
        job = _validated_media(clip, require_origin=True)
        parameters = job["request"]["parameters"]
        size = parameters.get("size")
        if (job.get("metadata", {}).get("model_route") or {}).get("adapter") == "litellm":
            from pianchang.sdk_video import sdk_expected_size
            size = sdk_expected_size(job)
        if size is None:
            resolution, ratio = parameters["resolution"], parameters["ratio"]
            custom = {("720P", "4:3"): "1104*832", ("720P", "3:4"): "832*1104",
                      ("1080P", "4:3"): "1648*1248", ("1080P", "3:4"): "1248*1648"}
            size = custom.get((resolution, ratio), SIZES_26[resolution][ratio])
        width, height = map(int, size.split("*"))
        folder = self.directory / "reviews" / digest_json(shot_id)[:16]
        if clip.get("repair_id"):
            folder = folder / ("candidate-" + clip["media_sha256"][:16] + "-" + clip["generation_job_sha256"][:8])
        contract = create_contract(self.project, shot_id, width=width, height=height)
        historical = historical_folder is not None
        inspection_path = historical_folder / "inspection" / "inspection.json" if historical else None
        if inspection_path and inspection_path.is_file():
            inspected = _read(inspection_path)
            if (Path(inspected["manifest_path"]).resolve() != inspection_path.resolve()
                    or Path(inspected["media_path"]).resolve() != Path(clip["media_path"]).resolve()
                    or inspected["media_sha256"] != clip["media_sha256"]
                    or any(not Path(frame["path"]).resolve().is_relative_to(inspection_path.parent.resolve())
                           for frame in inspected.get("frames", []))):
                raise ValueError("历史抽帧证据与当前选用镜头不符：" + shot_id)
            verify_inspection(inspected)
        else:
            inspected = inspect_media(clip["media_path"], folder / "inspection", interval_s=1, max_frames=16)
        obs = None
        vision = {"status": "disabled" if not self.vision_config else "no_applicable_sampled_criteria"}
        if historical:
            vision = {"status": "UNKNOWN", "reason": "本次本地恢复未调用视觉模型，历史目录中无可用观察。"}
            if (historical_folder / "contract.json").is_file() and _read(historical_folder / "contract.json") != contract:
                raise ValueError("历史契约与当前规格不符：" + shot_id)
            if (historical_folder / "observations.json").is_file():
                obs = _read(historical_folder / "observations.json")
                vision = {"status": "historical_sampled_frames", "request_id": obs["response"].get("request_id"), "new_call": False}
        elif self.vision_config and any(r["kind"] == "sampled_visual" for r in contract["requirements"]):
            try:
                obs = _one_model_call(folder / "vision-call.json", {"inspection_sha256": digest_json(inspected),
                                  "contract_sha256": digest_json(contract), "model": self.vision_config.model},
                                  lambda: observe(inspected, contract, config=self.vision_config))
                vision = {"status": "sampled_frames_observed", "request_id": obs["response"].get("request_id")}
            except ReviewOutOfScope:
                raise
            except (TextError, AcceptanceError, ValueError) as exc:
                # A preview has a separate completion condition from content approval. Keep the actual failure.
                vision = {"status": "UNKNOWN", "reason": str(exc), "replayed": False}
                event("video.collab.observation_unavailable", {"shot_id": shot_id, **vision}, kind="error")
        report = run_checks(self.project, contract, inspected, obs,
                            job=job if clip["operation"] in {"generated", "edited"} or
                            job["metadata"].get("project_sha256") == contract["project_sha256"] else None,
                            media_lineage=clip.get("media_lineage"),
                            **({"reference_job": job} if clip.get("required_refs") else {}))
        # Preserve the original generation journal. A reused job still belongs to its original project.
        binding = {"kind": clip["operation"], "origin_script_ref": clip["origin_script_ref"], "target": self.ref,
                   "origin_task_id": job["task_id"], "check": {"path": "generation.metadata.dependency_sha256",
                   "expected": self.impact["shots"][shot_id]["target"]["sha256"], "actual": job["metadata"]["dependency_sha256"], "status": "PASS"}}
        report_path = folder / "report.json"
        previous_report_path = None
        if historical and (historical_folder / "report.json").is_file():
            previous_report_path = historical_folder / "report.json"
            original = _read(previous_report_path)
            expected_prior = {**report, "generation_job_sha256": prior_job_sha256} if clip["operation"] == "generated" else report
            if original != expected_prior:
                raise ValueError("历史验收报告与当前契约、观察、视频或任务不符：" + shot_id)
            if original == report:
                report_path = previous_report_path
        if report_path != previous_report_path:
            _write(folder / "contract.json", contract)
            _write(report_path, report)
            if obs is not None:
                _write(folder / "observations.json", obs)
            _write(folder / "binding.json", binding)
        evidence_integrity = next(r for r in report["results"] if r["criterion_id"] == "evidence_integrity")
        if historical and evidence_integrity["status"] != "PASS":
            raise ValueError("验收证据不完整：" + shot_id + " " + evidence_integrity["reason"])
        return {"shot_id": shot_id, "status": report["status"], "media_accepted": report["task_complete"],
                "report_path": str(report_path), "report_sha256": digest_json(_read(report_path)),
                "original_report_path": str(previous_report_path) if previous_report_path else None,
                "original_report_sha256": digest_json(_read(previous_report_path)) if previous_report_path else None,
                "inspection_path": inspected["manifest_path"], "binding": binding, "vision_observation": vision,
                "technical_checks": {r["criterion_id"]: r["status"] for r in report["results"] if r["method"] == "technical"},
                "unresolved": [r["criterion_id"] for r in report["results"] if r["required"] and r["status"] != "PASS"]}

    @traced("video.collab.review_and_export", kind="tool")
    def review_and_export(self):
        if not self.project or self.shots.keys() != _objects(self.project, "shots").keys():
            raise ValueError("必须先生成或显式复用当前工程的全部镜头。")
        for shot in self.project["shots"]:
            sid = shot["id"]
            self.review_shot(sid)
        gate = self.acceptance_gate()
        if not gate["passed"]:
            self.preview = None
            return {"stage": "repairing", "preview": None, "acceptance": gate,
                    "next_action": "read_acceptance，依据证据准备修复并执行；缺少证据须具体说明并补齐。"}
        self._export()
        return {"preview": self.preview, "reports": self.reports, "edge_reviews": self.edge_reviews()}

    def _export(self, existing_preview=None):
        if not self.acceptance_gate()["passed"]:
            raise ValueError("所有镜头及衔接必过项尚未通过，不能进入人工预览。")
        clips = [{"shot_id": s["id"], "path": self.shots[s["id"]]["media_path"], "sha256": self.shots[s["id"]]["media_sha256"]}
                 for s in self.project["shots"]]
        preview = export_preview(clips, existing_preview or self.directory / "preview")
        self.preview = {k: preview[k] for k in ("preview_path", "preview_sha256", "manifest_path", "total_duration_s", "status")}
        self.preview.update(script_ref=self.ref, export_manifest_sha256=digest_json(_read(preview["manifest_path"])))

    @traced("video.collab.edge_reviews", kind="validation")
    def edge_reviews(self):
        states = {s["shot_id"]: s for s in validate_compilation(self.project)["state_trace"]}
        ids = [s["id"] for s in self.project["shots"]]
        rows = []
        for left, right in self.impact["recheck_edges"]:
            if left not in self.shots or right not in self.shots:
                continue
            adjacent = ids.index(right) == ids.index(left) + 1
            expected, actual = states[left]["after"], states[right]["before"]
            status = ("PASS" if expected == actual else "FAIL") if adjacent else "NOT_ADJACENT"
            cid = "continuity:" + left + "->" + right
            reports = [_read(self.reports[s]["report_path"]) for s in (left, right) if s in self.reports]
            observed = [r["status"] for report in reports for r in report["results"] if r["criterion_id"] == cid]
            continuity = "FAIL" if "FAIL" in observed else "PASS" if observed and all(x == "PASS" for x in observed) else "UNKNOWN"
            rows.append({"from": left, "to": right, "media": [self.shots[s]["media_sha256"] for s in (left, right)],
                         "script_ref": self.ref, "state_checks": {"path": f"{left}.after/{right}.before",
                         "expected": expected, "actual": actual, "status": status}, "audiovisual_continuity": continuity,
                         "preview": self.preview["preview_path"] if self.preview else None})
        return rows

    @traced("video.collab.acceptance_gate", kind="validation", capture_inputs=False, capture_output=False)
    def acceptance_gate(self, *, validate=True):
        issues, checks = [], []
        approvals = _manual_approvals(self)
        for shot in (self.project or {}).get("shots", []):
            sid = shot["id"]
            clip, summary = self.shots.get(sid), self.reports.get(sid)
            if not clip or not summary:
                issues.append({"shot_id": sid, "criterion_id": "media_or_report", "status": "UNKNOWN",
                               "reason": "镜头或自动验收报告尚未就绪。"})
                continue
            report = _read(summary["report_path"])
            valid = (digest_json(report) == summary["report_sha256"] and report["media_sha256"] == clip["media_sha256"]
                     and report["project_sha256"] == digest_json(self.project) and report["shot_id"] == sid)
            if validate and valid:
                job = _validated_media(clip, require_origin=True)
                if report.get("generation_job_sha256") is not None:
                    valid = report["generation_job_sha256"] == digest_json(job)
                if report.get("reference_job_sha256") is not None:
                    valid = valid and report["reference_job_sha256"] == digest_json(job)
                inspection = _read(summary["inspection_path"])
                verify_inspection(inspection)
                valid = valid and report["inspection_sha256"] == digest_json(inspection)
                if report.get("content_review_version"):
                    from pianchang.content_review import validate_content_evidence
                    folder = Path(summary["report_path"]).parent
                    evidence = _read(folder / "content.json")
                    valid = valid and report.get("content_evidence_sha256") == digest_json(evidence)
                    if valid:
                        validate_content_evidence(self.project, _read(folder / "contract.json"), inspection, evidence)
                        valid = all(s in self.shots and a["sha256"] == self.shots[s]["media_sha256"]
                                    for s, a in evidence["selected_media"].items())
            checks.append({"path": sid + ".acceptance_binding", "expected": True, "actual": valid,
                           "status": "PASS" if valid else "FAIL"})
            if not valid:
                issues.append({"shot_id": sid, "criterion_id": "acceptance_binding", "status": "FAIL", "reason": "报告与当前候选组合或证据不符，须重新检查。"})
            from pianchang.creative_sound import effective_results
            required = [r for r in effective_results(shot, report) if r.get("required")]
            contract = _read(Path(summary["report_path"]).parent / "contract.json")
            declared = {r["criterion_id"] for r in contract["requirements"] if r.get("required")}
            from pianchang.acceptance import required_subtitle
            subtitle=required_subtitle(shot)
            if subtitle:declared.add(subtitle["criterion_id"])
            reviewable = set()
            if approvals:
                from pianchang.manual_delivery import adjacent_cut_criteria, human_reviewable_criteria
                from pianchang.shot_approval import approval_matches
                declared.update(adjacent_cut_criteria(self.project, sid))
                decision = approvals.get(sid)
                if decision and valid and approval_matches(decision, self.request_id, self.project, sid, clip, summary):
                    if digest_json(contract) == report.get("contract_sha256"):
                        reviewable = human_reviewable_criteria(contract)
                    else:
                        issues.append({"shot_id":sid, "criterion_id":"acceptance_binding", "status":"FAIL",
                                       "reason":"人工审看的报告与当前验收契约不符。"})
            observed = {r["criterion_id"] for r in required}
            checks.append({"path": sid+".required_coverage", "expected": sorted(declared), "actual":sorted(observed),
                           "status": "PASS" if declared <= observed else "FAIL"})
            if not declared <= observed:
                issues.append({"shot_id": sid, "criterion_id": "required_coverage", "status": "FAIL", "reason": "报告缺少原契约中的必过项。"})
            accepted_by_person = False
            for row in required:
                if row["status"] in {"FAIL", "UNKNOWN"} and row["criterion_id"] in reviewable:
                    checks.append({"path": sid+"."+row["criterion_id"], "expected":"PASS", "actual":row["status"],
                                   "status":"PASS", "decision":"human_approved", "trace_id":decision["trace_id"]})
                    accepted_by_person = True
                    continue
                checks.append({"path": sid+"."+row["criterion_id"], "expected":"PASS", "actual":row["status"],
                               "status":row["status"]})
                if row["status"] != "PASS":
                    issues.append({"shot_id": sid, **{k: row[k] for k in ("criterion_id", "status", "reason", "needs_creative_audio") if k in row}})
            if not required or (not accepted_by_person and (report.get("task_complete") is not True or report["status"] != "PASS")):
                if not any(r["shot_id"] == sid for r in issues):
                    issues.append({"shot_id": sid, "criterion_id": "task_complete", "status": "UNKNOWN", "reason": "报告未确认全部必过项通过。"})
        passed = bool(self.project and self.project["shots"]) and not issues
        event("video.collab.acceptance_gate.result", {"passed": passed, "checks": checks, "issues": issues}, kind="validation")
        return {"passed": passed, "stage": "ready_for_preview" if passed else "repairing", "issues": issues}

    @traced("video.collab.finish_check", kind="validation")
    def finish_check(self):
        checks = []
        for shot in (self.project or {}).get("shots", []):
            sid = shot["id"]
            record = self.reports.get(sid)
            clip = self.shots.get(sid)
            valid = False
            if record and clip:
                _validated_media(clip, require_origin=True)
                report = _read(record["report_path"])
                inspection = _read(record["inspection_path"])
                verify_inspection(inspection)
                valid = (digest_json(report) == record["report_sha256"] and report["inspection_sha256"] == digest_json(inspection)
                         and report["media_sha256"] == clip["media_sha256"] and report["project_sha256"] == digest_json(self.project)
                         and report["shot_id"] == sid and inspection["media_sha256"] == clip["media_sha256"])
            checks.append({"path": f"shots.{sid}.review", "expected": True, "actual": valid, "status": "PASS" if valid else "FAIL"})
        valid_preview = bool(self.preview and _evidence_match(self.preview["preview_path"], self.preview["preview_sha256"]))
        if valid_preview:
            manifest = _read(self.preview["manifest_path"])
            selected = [(s["id"], self.shots[s["id"]]["media_sha256"]) for s in self.project["shots"]]
            valid_preview = (self.preview["export_manifest_sha256"] == digest_json(manifest)
                             and selected == [(clip["shot_id"], clip["sha256"]) for clip in manifest["clips"]])
        acceptance = self.acceptance_gate()
        complete = bool(checks) and all(c["status"] == "PASS" for c in checks) and valid_preview and acceptance["passed"]
        return {"complete": complete, "checks": checks, "preview_valid": valid_preview,
                "media_accepted": complete, "acceptance": acceptance,
                "summary": "所有镜头及相邻衔接必过项已通过，已交付绑定剧本版本的人工预览。" if complete else "自动验收尚未全部通过或预览证据不完整，视频制作未完成。"}

    @traced("video.collab.stop_check", kind="validation", capture_inputs=False, capture_output=False)
    def stop_check(self, record):
        remaining = record["limits"]["max_video_generations"] - record["video_generations"]
        if getattr(self,"autonomy",None) and self.project:
            queue = self.autonomy.queue(record)
            if queue["actionable"]:
                return {"allowed":False,"reason":"仍有可修复的明确失败项；未知项不会中断其他工作。",
                        "actionable":queue["actionable"],"next_action":"design_repair_strategy"}
            if queue["all_required_passed"]:
                return {"allowed":False,"reason":"须先导出验收通过的预览并完成独立检查。"}
            return {"allowed":True,"reason":"可执行修复已处理完或到达授权上限；以下受阻项仍未通过。",
                    "blocked":queue["blocked"],"exhausted":queue["exhausted"],"task_complete":False,
                    "terminal_summary":queue.get("terminal_summary")}
        if any(h["status"] == "started_or_unknown" for h in self.repair.history):
            return {"allowed":True,"reason":"已有修复执行结果未确认，不能重复提交。"}
        if self.project is None:
            errors = [m for m in record["messages"] if m["role"] == "user" and '"tool_result"' in m["content"]
                      and '"error"' in m["content"] and '"ok":false' in m["content"]]
            return {"allowed":bool(errors),"reason":"须先执行制作规划；规划报错时保留实际阻碍。"}
        deferred = (self.shooting_plan or {}).get("deferred_shots", [])
        missing = [s["id"] for s in self.project["shots"] if s["id"] not in self.shots and s["id"] not in deferred]
        if missing and remaining > 0:
            return {"allowed":False,"reason":"仍有未完成镜头和可用生成/修复额度。","shot_ids":missing}
        gate = self.acceptance_gate(validate=False)
        invalid = [r for r in gate["issues"] if r["criterion_id"] in
                   {"evidence_integrity", "candidate_binding", "reference_submission", "acceptance_binding"}]
        if invalid:
            return {"allowed":True,"reason":"素材绑定或验收证据校验未通过，须先恢复真实证据，不能用重拍掩盖。","issues":invalid}
        inconclusive = [r for r in gate["issues"] if r["status"] == "UNKNOWN" and r["criterion_id"] != "media_or_report"]
        policy = (self.shooting_plan or {}).get("acceptance_loop")
        repairable = {r["shot_id"] for r in gate["issues"] if r["shot_id"] not in deferred and r["status"]=="FAIL"} | set(self.failed_shots)
        from pianchang.video_repair import retake_limit
        available = [sid for sid in repairable if not self.repair.retake_spent(sid)
                     and record.get("repair_reservations", {}).get(sid, 0) < retake_limit(policy or {}, sid)]
        if remaining > 0 and available:
            return {"allowed":False,"reason":"有未通过项及可用自动修复额度，请依据证据选择工具继续。","shot_ids":available}
        if inconclusive:
            return {"allowed":True,"reason":"其他可修项已处理；剩余证据不足项保留具体原因，制作未完成。","issues":inconclusive}
        if gate["passed"]:
            return {"allowed":False,"reason":"验收已通过，须导出预览并按独立完成检查结束。"}
        return {"allowed":True,"reason":"自动修复额度已用完或缺少可执行的补证动作，制作仍未完成。"}


@traced("video.collab.run", kind="agent", capture_inputs=False)
def run_video_agent(*, script, script_ref, directory, text_config, video_config, template_project=None,
                    previous=None, client=None, vision_config=None, max_video_generations=3, max_steps=None,
                    progress=None, inbound_message=None, negotiation=None, reference_store=None, story_id=None,
                    register_specs=None, shooting_plan=None, partial=None, resume=False):
    for config in (text_config, video_config, vision_config):
        if config is not None and getattr(config, "api_key", None):
            register_secret(config.api_key)
    if validate_script(script) or not script_ref.get("asset_id") or not script_ref.get("revision_id"):
        raise ValueError("需要已验证剧本及其稳定资产 ID 与固定版本。")
    if (script_ref.get("sha256") or script_ref.get("content_sha256") or digest_json(script)) != digest_json(script):
        raise ValueError("输入剧本与指定资产版本的内容哈希不符。")
    known_spec = template_project or (previous or {}).get("project", {})
    for shot in known_spec.get("shots", []):
        if (shooting_plan or {}).get("repair_existing_only"):
            # prepare_existing performs the same frozen-input checks once, under
            # the loop's exclusive claim, before any model decision is submitted.
            break
        if shot["id"] in (shooting_plan or {}).get("deferred_shots", []):
            continue
        pins = shot.get("required_refs", [])
        if pins:
            if reference_store is None or not (known_spec.get("reference_story_id") or story_id):
                raise ValueError("此镜头要求固定媒体，请提供可解析且属于该故事的参考素材库。")
            from pianchang.reference_inputs import plan_reference_inputs
            reference_config = config_for_shooting_plan(video_config, pins, shooting_plan)
            if not reference_config.sdk_video and reference_config.model not in REFERENCE_MODELS:
                validate_reference_capabilities(reference_config, pins)
            plan_reference_inputs(known_spec, shot["id"], reference_config)
            resolve_references(reference_store, pins, story_id=known_spec.get("reference_story_id") or story_id)
    directory = Path(directory).resolve()
    client = client if client is not None else BailianTextClient(text_config)
    event("video.collab.input", {"script_ref": script_ref, "script": script, "previous_ref": (previous or {}).get("script_ref"),
                                "inbound_message": inbound_message, "max_video_generations": max_video_generations,
                                "vision_enabled": vision_config is not None})
    workspace = VideoWorkspace(script=script, script_ref=script_ref, directory=directory, text_config=text_config,
                               video_config=video_config, template_project=template_project, previous=previous,
                               decision_client=client, vision_config=vision_config, progress=progress, inbound_message=inbound_message,
                               reference_store=reference_store, story_id=story_id, register_specs=register_specs,
                               shooting_plan=shooting_plan, partial=partial)
    autonomous = bool((shooting_plan or {}).get("acceptance_loop",{}).get("autonomy_version"))
    if autonomous:
        from pianchang.video_autonomy import VideoAutonomy
        workspace.autonomy = VideoAutonomy(workspace,negotiation)
    catalog = [t for t in workspace.tools if t.name != "edit_shot_asset" or not shooting_plan]
    catalog += (negotiation.tools() if negotiation else []) + (workspace.autonomy.tools if autonomous else [])
    from pi_bridge.runtime import make_loop
    from pianchang.reference_inputs import external_review_usage
    from pi_bridge.recovery import strategy_external_usage
    extra={"external_model_usage":lambda:len(external_review_usage(directory/"reference-inputs")["reservations"])+strategy_external_usage(directory)} if getattr(client,"agent_backend",None)!="offline_legacy" else {}
    prepare = None
    if (shooting_plan or {}).get("repair_existing_only"):
        prepare = workspace.prepare_existing
    elif workspace.reuses_entire_continuation:
        prepare = workspace.prepare_reused_clips
    loop = make_loop(client, catalog, directory / "agent", role="video", snapshot=workspace.snapshot,
                     finish_check=workspace.finish_check, progress=progress, pause_check=lambda:workspace.retake_pause(loop.record),
                     stop_check=workspace.stop_check,
                     prepare=prepare,
                     maintain=workspace.autonomy.tick if autonomous else None,
                     recover=workspace.autonomy.recover if autonomous else None,**extra)
    workspace.reserve_reference_review=lambda:loop.before_model({"auxiliary_model":"reference_board_review"}) if hasattr(loop,"before_model") else (_ for _ in ()).throw(ValueError("离线旧协议没有视觉核验模型额度，不能提交付费观察。"))
    if hasattr(loop, "before_model"):
        workspace.reserve_strategy_review = lambda: loop.before_model({"auxiliary_model": "repair_strategy"})
        workspace.reserve_content_review = lambda purpose: loop.before_model({"auxiliary_model": purpose})
        workspace.reserve_content_review.commission_id = workspace.request_id
    repair_direction = ("每镜最多一次定向重拍；重拍验收仍不通过时保留报告供人工审看，继续处理尚可执行的其他镜头。"
                        if (shooting_plan or {}).get("acceptance_loop", {}).get("max_local_edits_per_shot") == 0
                        else "或edit_shot_asset修改视频资产（例如切黑及剧本原文字幕）。没有表现出来的动作必须重拍，不能用字幕冒充。")
    goal = ("你是独立视频制作 Agent。完成条件是准确表现固定剧本，所有镜头及相邻衔接的必过验收项全部PASS，之后才能导出人工预览。"
            "先plan_production，再按计划generate_shot/reuse_shot。镜头自动验收失败时read_acceptance读取实际证据，"
            "用prepare_shot_repair准备有针对性的修复。根据问题自主选择regenerate_shot修改提示词并使用固定参考重拍，"
            + repair_direction +
            "每次修改自动复验当前候选和相邻衔接。失败项不得删除、标准不得放宽、参考身份不得替换。"
            "也要比较剧本与规格：画外对视、心理认知缺少可核对证据或规格自相矛盾时，具体说明并通过已有协商工具请求澄清。"
            "不要对同一个无效提示盲目重复重拍。缺失邻镜头先补齐，再审衔接。"
            "全部素材就绪后review_and_export，有FAIL/UNKNOWN则继续修复，只有独立完成检查通过才能finish(completed)。"
            "实际无法补证、结果未确认或额度用完时返回needs_input并说明阻碍；不能称制作完成。")
    goal += ("原始参考数量超限时先read_reference_inputs，再prepare_reference_inputs。identity_first优先保留独立人物图；"
             "真实核验失败可改balanced_boards调整分组和清晰度。两种策略的源引用与审核均会落盘，不能重放未确认观察，"
             "不能把参考板核验通过当作成片通过。不要因一镜头参考受阻就放弃其他可准备或生成的镜头。")
    if negotiation is not None:
        goal += "若发现真实剧情矛盾且有证据，可定向提出改稿请求。项目执行器会自动处理请求并回传真实回应；等待时继续其他可修工作。"
    if shooting_plan is not None:
        goal += "使用拍摄计划中的自动修复额度，无需逐轮等待人工批准；上限是停止条件，不是通过条件。"
    if (shooting_plan or {}).get("repair_existing_only"):
        goal += ("本轮只修复计划reuse_shots中的已有候选。启动工具已经完成plan_production及所有reuse_shot，真实结果见preparation_result，"
                 "不必重复规划或逐个复用。先调用read_acceptance空参数读取全部失败摘要，再直接准备修复；只在必要时补读单镜细节，避免反复载入完整状态字段。"
                 "未生成的deferred_shots不在本轮生成范围，不能为它们调用生成；所有原必过条件和缺失衔接仍保留。"
                 "优先修复可观察的动作和服化道问题，再如实列出认知状态、规格矛盾或缺失相邻视频等真实阻碍。"
                 "read_acceptance的prior_attempts保存当前候选已经实际尝试的修复指令；下一轮必须结合失败证据改变策略，不能原样重复。"
                 "衔接报告若越界引用另一镜头动作、把剧本明确的新角色视为多余或强制跨场站位不变，应报告验收器错误，不能据此改坏剧情。"
                 "不能因为某一项缺证据就放弃其他仍能修复的明确失败项。全项目验收没有通过时不得导出预览。")
    if autonomous:
        goal += ("每轮autonomous_work_queue提供独立划分的actionable、blocked和exhausted。"
                 "actionable非空时不得停止。优先对action_sequence失败调用design_repair_strategy，"
                 "它会对照历史实际失败方案设计不同拍法并返回新repair_id，然后regenerate_shot执行。"
                 "队列已提供当前摘要，design_repair_strategy会自行载入详细证据，不要反复空参数read_acceptance灌入全量报告。"
                 "策略重复、候选变化或旧repair_id失效时，必须重新design_repair_strategy，不能重试旧计划。"
                 "不要将预检拒绝说成额度耗尽。完成一个镜头后根据新队列处理其他仍可修项。"
                 "缺证据、认知状态、规格矛盾、缺少范围外镜头仅阻塞相应项，不阻塞其他镜头。"
                 "start_end_state的明确失败字段若已由剧本 Agent 结合本轮绑定报告核对为规格矛盾，"
                 "可逐字段用record_specification_blocker登记证据；原FAIL/UNKNOWN保持，其他未登记的可修字段仍须处理。"
                 "最终仍有失败时列出各项证据和已尝试策略及确切次数；不得删除或放宽必过项。")
    if max_steps is None:
        policy = (shooting_plan or {}).get("acceptance_loop")
        max_steps = (max(30, len(policy["shot_ids"]) * (6 + 3 * policy["max_repairs_per_shot"]) + 20)
                     if policy else max(18, 2 * len(known_spec.get("shots", [])) + 8) if shooting_plan is not None else 18)
    result = loop.run(goal, max_steps=max_steps, max_video_generations=max_video_generations, shooting_plan=shooting_plan,resume=resume)
    checkpoint = None
    if result.get("reason") == "retake_approval_required" and shooting_plan:
        checkpoint = {"schema_version": "video-partial/1", "request_id": inbound_message["message_id"],
                      "script_ref": script_ref, "project": workspace.project, "shooting_plan": shooting_plan,
                      "shots": workspace.shots, "failed_shots": workspace.failed_shots}
        event("video.collab.partial_checkpoint", {"request_id": checkpoint["request_id"], "sha256": digest_json(checkpoint),
              "finished_shots": list(workspace.shots), "failed_shots": workspace.failed_shots}, kind="artifact")
        _write(directory / "partial.json", checkpoint)
    production = None
    if result["task_complete"]:
        production = {"schema_version": "video-production/1", "script_ref": script_ref, "project": workspace.project,
                      "impact": workspace.impact, "shots": workspace.shots, "reports": workspace.reports,
                      "preview": workspace.preview, "media_accepted": result["completion_check"]["media_accepted"],
                      "repair_history": workspace.repair.history,
                      "edge_reviews": workspace.edge_reviews(), "creative_review": "UNKNOWN"}
        if workspace.reference_specs:
            production["reference_specs"] = workspace.reference_specs
        approvals = _manual_approvals(workspace)
        if approvals:
            production.update(manual_approvals=approvals, request_id=workspace.request_id)
        _write(directory / "production.json", production)
    return {"loop": result, "production": production, "impact": workspace.impact, "partial": checkpoint}


def _recover_clip(clip, project, script_ref, dependency):
    """Validate prior evidence, commit a signed-URL redaction receipt, and preserve the original clip record."""
    clip = copy.deepcopy(clip)
    sid = clip["shot_id"]
    manifest = Path(clip["manifest_path"]).resolve()
    media = Path(clip["media_path"]).resolve()
    if manifest.name != "job.json" or media != manifest.parent / "video.mp4" or clip["script_ref"] != script_ref:
        raise ValueError("历史素材位置或目标版本不符：" + sid)
    if clip["dependency_sha256"] != dependency:
        raise ValueError("目标镜头依赖版本不符：" + sid)
    job = _read(manifest)
    raw_sha = digest_json(job)
    old_sha = clip["generation_job_sha256"]
    receipt_path = manifest.parent / ("redaction-" + old_sha + ".json")
    if clip["operation"] == "generated" and (job["metadata"].get("project_sha256") != digest_json(project)
            or job["metadata"].get("script_sha256") != project["script_source"]["sha256"]
            or job["metadata"].get("input_versions") != _objects(project, "shots")[sid]["input_versions"]):
        raise ValueError("生成任务与源工程、剧本或镜头输入不符：" + sid)
    if Path(job["local_video_path"]).resolve() != media:
        raise ValueError("生成任务记录了另一个下载位置：" + sid)
    if raw_sha == old_sha:
        _validated_media(clip, require_origin=True)
    safe = get_tracer().sanitize(job)
    new_sha = digest_json(safe)
    if any(job.get(k) != safe.get(k) for k in job.keys() - {"provider_response", "video_url"}):
        raise ValueError("任务中含超出下载 URL 范围的敏感字段，已停止原地迁移。")
    receipt = {"schema_version": "video-manifest-redaction/1", "manifest_path": str(manifest),
               "old_generation_job_sha256": old_sha, "new_generation_job_sha256": new_sha,
               "task_id": clip["task_id"], "shot_id": sid, "media_sha256": clip["media_sha256"],
               "request_sha256": job["request_sha256"], "origin_script_ref": clip["origin_script_ref"]}
    if old_sha != new_sha:
        if receipt_path.exists():
            if _read(receipt_path) != receipt or raw_sha not in {old_sha, new_sha}:
                raise ValueError("原任务脱敏迁移收据不符：" + sid)
        else:
            if raw_sha != old_sha:
                raise ValueError("旧任务哈希不符，且无对应脱敏收据：" + sid)
            event("video.collab.manifest_redaction_prepared", receipt, kind="artifact")
            _write(receipt_path, receipt)
        if raw_sha == old_sha:
            _write(manifest, safe)
        clip["generation_job_sha256"] = new_sha
        event("video.collab.manifest_redaction_confirmed", receipt, kind="artifact")
    elif raw_sha != old_sha:
        raise ValueError("源任务清单由未知写入改变：" + sid)
    _validated_media(clip, require_origin=True)
    return clip, receipt if old_sha != new_sha else None


@traced("video.collab.recover_preview", kind="task")
def recover_preview(*, source_directory, directory, script, script_ref, template_project=None, previous=None):
    """Complete a local preview from the finished run's actual tool receipts. No model or cloud calls."""
    source, destination = Path(source_directory).resolve(), Path(directory).resolve()
    if source == destination or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError("恢复结果必须写入原运行之外的独立目录。")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError("恢复目录已有内容，须选择新目录。")
    snapshots = {n: _read(source / n) for n in ("agent/run.json", "project.json", "impact.json", "clips.json")}
    source_hashes = {n: digest_json(v) for n, v in snapshots.items()}
    journal_sha = file_sha256(source / "agent" / "events.jsonl")
    run, project, saved_impact, saved_clips = snapshots.values()
    if run.get("status") not in {"error", "completed", "needs_input", "budget_exhausted", "cancelled"}:
        raise ValueError("源模型循环仍在运行或状态不明确。")
    if script_ref.get("sha256", digest_json(script)) != digest_json(script):
        raise ValueError("剧本资产哈希与输入不符。")
    _checked_project(project, script)
    if template_project is not None and template_project != project:
        raise ValueError("恢复工程与指定制作合同中的工程不符。")
    messages = [json.loads(m["content"]) for m in run["messages"] if m["role"] == "user"]
    states = [m["state"] for m in messages if m.get("type") == "state_snapshot"]
    if not states or any(s.get("script_ref") != script_ref or s.get("script_sha256") != digest_json(script) for s in states):
        raise ValueError("源视频 Agent 上下文绑定了别的剧本版本。")
    receipts = [m for m in messages if m.get("type") == "tool_result" and m.get("ok")]
    shooting_plan = run.get("shooting_plan")
    partial = None
    if shooting_plan and shooting_plan.get("partial"):
        pointer = shooting_plan["partial"]
        partial = _read(source.parent / ("video-spec-" + uuid.UUID(pointer["request_id"]).hex) / "partial.json")
        if digest_json(partial) != pointer["sha256"] or partial["request_id"] != pointer["request_id"]:
            raise ValueError("部分制作记录与源任务所用版本不符。")
    impact = _production_impact(project, previous, shooting_plan, partial)
    if saved_impact != impact or not _matches_tool_output(receipts, "plan_production", saved_impact):
        raise ValueError("历史影响计划与 Agent 实际工具结果或重算依赖不符。")
    order = [s["id"] for s in project["shots"]]
    if set(saved_clips) != set(order):
        raise ValueError("原任务未完成全部镜头下载，不能离线交付预览。")
    for sid in order:
        expected_tool = "generate_shot" if sid in impact["regenerate"] else "reuse_shot"
        if not _matches_tool_output(receipts, expected_tool, saved_clips[sid]):
            raise ValueError("缺少与选用镜头相符的历史工具结果：" + sid)
    destination.mkdir(parents=True, exist_ok=True)
    with (destination / ".recovery-claimed").open("x", encoding="utf-8") as claim:
        claim.write(str(source))
    workspace = VideoWorkspace(script=script, script_ref=script_ref, directory=destination, text_config=None,
                               video_config=None, template_project=None, previous=previous, decision_client=None,
                               vision_config=None, progress=None)
    workspace.project, workspace.impact = project, impact
    migrations, historical_reports, technical_only = [], [], []
    for sid in order:
        row, migration = _recover_clip(saved_clips[sid], project, script_ref, impact["shots"][sid]["target"]["sha256"])
        workspace.shots[sid] = row
        if migration:
            migrations.append(migration)
        original = source / "reviews" / digest_json(sid)[:16]
        workspace.reports[sid] = workspace._review(sid, historical_folder=original,
                             prior_job_sha256=saved_clips[sid]["generation_job_sha256"])
        (historical_reports if (original / "report.json").is_file() else technical_only).append(sid)
    original_preview = source / "preview"
    reuse_preview = (original_preview / "export.json").is_file()
    workspace._export(original_preview if reuse_preview else None)
    check = workspace.finish_check()
    if not check["complete"]:
        raise ValueError("历史素材、报告或导出顺序没有通过恢复验收。")
    if journal_sha != file_sha256(source / "agent" / "events.jsonl") or any(digest_json(_read(source / n)) != sha for n, sha in source_hashes.items()):
        raise ValueError("恢复期间原任务输入、历史消息或选用素材发生变化。")
    recovery = {"mode": "local_media_recovery", "source_directory": str(source), "source_files": source_hashes,
                "source_agent_journal_sha256": journal_sha,
                "source_run_trace_id": run["trace_context"]["trace_id"], "source_loop_status": run["status"],
                "source_loop_result": (run.get("result") or {}).get("summary"),
                "source_tool_errors": [{"tool": m.get("tool"), "error": m.get("error")} for m in messages
                                       if m.get("type") == "tool_result" and not m.get("ok")],
                "recovery_trace_id": current_context()["trace_id"], "model_calls": 0, "video_submissions": 0,
                "verified_prior_reports": historical_reports, "new_reports": technical_only,
                "reused_preview": reuse_preview, "manifest_redactions": migrations, "selection_order": order}
    result = {"schema_version": "video-production/1", "script_ref": script_ref, "project": project, "impact": impact,
              "shots": workspace.shots, "reports": workspace.reports, "preview": workspace.preview,
              "media_accepted": check["media_accepted"], "edge_reviews": workspace.edge_reviews(),
              "creative_review": "UNKNOWN", "recovery": recovery}
    _write(destination / "production.json", result)
    event("video.collab.local_recovery_finished", recovery, kind="artifact")
    return result


@traced("cli.video_collab", kind="cli")
def main(argv=None):
    parser = argparse.ArgumentParser(description="独立制作一个已交付的 JSON 剧本包，无需剧本 Agent 或 Web 服务。")
    parser.add_argument("script", type=Path, help="screenplay/1 剧本，或含 title/logline/outline/setup/episodes 的创作草稿")
    parser.add_argument("--asset-id", required=True, help="剧本稳定资产 ID，改稿时继续使用此 ID")
    parser.add_argument("--revision-id", required=True, help="本次固定的不可变输入版本，如 r1")
    parser.add_argument("--output", type=Path, required=True, help="本次独立 Agent 运行目录")
    parser.add_argument("--project", type=Path, help="与初版来源严格匹配的制作规格；缺省则由视频 Agent 编译")
    parser.add_argument("--previous", type=Path, help="同一剧本上次完成的 production.json")
    parser.add_argument("--reference-store", type=Path, help="镜头所用参考素材的 SQLite 资产库")
    parser.add_argument("--reference-story-id", help="参考素材所属故事（默认读取绑定工程的 reference_story_id）")
    parser.add_argument("--max-video-generations", type=int, required=True, help="本次允许的视频提交上限")
    parser.add_argument("--vision", action="store_true", help="调用已配置的视觉验收模型观察抽取的证据帧")
    parser.add_argument("--env-file", type=Path, default=Path(__file__).resolve().parents[1] / ".env")
    args = parser.parse_args(argv)
    original = _read(args.script)
    script = normalize_script(original, idea=original.get("idea", ""))
    source_ref = {"asset_id": args.asset_id, "revision_id": args.revision_id, "content_sha256": digest_json(script)}
    with span("video.collab.standalone", kind="task", inputs={"source_path": str(args.script.resolve()),
                "source_file_sha256": file_sha256(args.script), "script_ref": source_ref}):
        result = run_video_agent(script=script, script_ref=source_ref, directory=args.output,
                     text_config=load_text_config(args.env_file), video_config=load_config(args.env_file),
                     vision_config=load_observer_config(args.env_file) if args.vision else None,
                     template_project=_read(args.project) if args.project else None,
                     previous=_read(args.previous) if args.previous else None,
                     reference_store=CollabStore(args.reference_store) if args.reference_store else None,
                     story_id=args.reference_story_id,
                     max_video_generations=args.max_video_generations)
        print(json.dumps({"loop": result["loop"], "output": str(args.output)}, ensure_ascii=False))
        return 0 if result["loop"]["task_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
