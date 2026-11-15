"""Screenplay snapshots, exact source references and declared beat coverage.

These checks establish provenance and declared structure, not semantic fidelity
or the quality of generated video. All required beats are assigned by code.
Requiring at least one declared fact is a minimum structural check, not proof
that all story state or all source meaning has been extracted.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

from pianchang.production import validate as validate_production
from trace_system import event, traced


# Capacity of one screenplay/1 document. These limits do not prescribe its shape.
MAX_EPISODES = 4
MAX_SCENES = 12
MAX_BEATS_PER_SCENE = 8
MAX_BEATS = 48


class StoryError(ValueError):
    """Invalid screenplay input or a value that cannot form a JSON snapshot."""


@traced("story.validate_structure_constraints", kind="validation")
def validate_structure_constraints(constraints):
    """Check only requested counts. Missing dimensions remain the writer's choice."""
    limits = {"episodes": MAX_EPISODES, "scenes_per_episode": MAX_SCENES, "beats_per_scene": MAX_BEATS_PER_SCENE}
    for key, value in constraints.items():
        maximum = limits.get(key)
        valid = maximum is not None and type(value) is int and 1 <= value <= maximum
        expected = {"min": 1, "max": maximum} if maximum else list(limits)
        event("story.structure.constraint", {"path": key, "expected": expected, "actual": value,
              "status": "PASS" if valid else "FAIL"}, kind="validation")
        if not valid:
            raise StoryError(f"{key} 须为 1～{maximum} 的整数。" if maximum else f"未知剧本数量约束：{key}")
    if constraints:
        # One nonempty child is the minimum for an unspecified dimension, never a target.
        min_scenes = constraints.get("episodes", 1) * constraints.get("scenes_per_episode", 1)
        min_beats = min_scenes * constraints.get("beats_per_scene", 1)
        for path, minimum, limit in (("total_scenes", min_scenes, MAX_SCENES), ("total_beats", min_beats, MAX_BEATS)):
            event("story.structure.capacity", {"path": path, "expected": {"maximum": limit},
                  "actual": {"minimum_required": minimum}, "status": "PASS" if minimum <= limit else "FAIL"}, kind="validation")
            if minimum > limit:
                raise StoryError(f"指定数量超过单次剧本容量（最多 {MAX_SCENES} 场、{MAX_BEATS} 个节拍）。")


def script_structure(script):
    """Count what is actually present in a screenplay or draft, including uneven episodes."""
    if not isinstance(script, dict) or not isinstance(script.get("episodes"), list):
        return None
    episodes = []
    scene_count = beat_count = 0
    for episode in script["episodes"]:
        if not isinstance(episode, dict) or not isinstance(episode.get("scenes"), list):
            return None
        scenes = []
        for scene in episode["scenes"]:
            if not isinstance(scene, dict) or not isinstance(scene.get("beats"), list):
                return None
            size = len(scene["beats"])
            scenes.append({"id": scene.get("id"), "title": scene.get("title"), "beat_count": size})
            beat_count += size
        episodes.append({"id": episode.get("id"), "title": episode.get("title"), "scene_count": len(scenes), "scenes": scenes})
        scene_count += len(scenes)
    return {"episode_count": len(episodes), "scene_count": scene_count, "beat_count": beat_count, "episodes": episodes}


def digest_json(value):
    """Hash JSON deterministically; non-JSON values and NaN are rejected."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
        raise StoryError("输入必须是有限、可编码的 JSON 数据。") from exc
    return hashlib.sha256(encoded).hexdigest()


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _issue(code, path, message, **evidence):
    return {"code": code, "path": path, "message": message,
            "severity": "error", "evidence": evidence}


@traced("story.validate_script", kind="validation")
def validate_script(script):
    """Return structural errors for a normalized screenplay without mutating it."""
    issues = []

    def issue(code, path, message, **evidence):
        issues.append(_issue(code, path, message, **evidence))

    if not isinstance(script, dict):
        return [_issue("SCRIPT_SCHEMA", "$", "剧本必须是 JSON 对象。")]
    try:
        digest_json(script)
    except StoryError as exc:
        return [_issue("SCRIPT_SCHEMA", "$", str(exc))]
    if script.get("schema_version") != "screenplay/1":
        issue("SCRIPT_SCHEMA", "schema_version", "支持 screenplay/1。")
    version = script.get("script_version")
    if type(version) is not int or version < 1:
        issue("SCRIPT_VERSION", "script_version", "剧本版本须为正整数。")
    for field in ("idea", "title", "logline", "outline"):
        if not _text(script.get(field)):
            issue("SCRIPT_TEXT", field, "须为非空文本。")
    seen = set()

    def identity(value, prefix, path):
        if not isinstance(value, str) or not re.fullmatch(prefix + r"\d{3}", value):
            issue("SCRIPT_ID", path, f"ID 须采用 {prefix}001 格式。")
        elif value in seen:
            issue("SCRIPT_DUPLICATE_ID", path, "剧本 ID 必须全局唯一。", source_id=value)
        else:
            seen.add(value)

    setup = script.get("setup")
    if not isinstance(setup, list) or not setup:
        issue("SCRIPT_SCHEMA", "setup", "setup 须为非空列表。")
        setup = []
    for index, item in enumerate(setup):
        path = f"setup[{index}]"
        if not isinstance(item, dict):
            issue("SCRIPT_SCHEMA", path, "设定须为含 id 与 text 的对象。")
            continue
        identity(item.get("id"), "SET", path + ".id")
        if not _text(item.get("text")):
            issue("SCRIPT_TEXT", path + ".text", "设定原文不能为空。")
    episodes = script.get("episodes")
    if not isinstance(episodes, list) or not 1 <= len(episodes) <= MAX_EPISODES:
        issue("SCRIPT_LIMIT", "episodes", "本阶段支持 1–4 集。")
        if not isinstance(episodes, list):
            episodes = []
    scene_count = beat_count = 0
    for ei, episode in enumerate(episodes):
        ep_path = f"episodes[{ei}]"
        if not isinstance(episode, dict):
            issue("SCRIPT_SCHEMA", ep_path, "分集须为对象。")
            continue
        identity(episode.get("id"), "EP", ep_path + ".id")
        if not _text(episode.get("title")):
            issue("SCRIPT_TEXT", ep_path + ".title", "分集标题不能为空。")
        scenes = episode.get("scenes")
        if not isinstance(scenes, list) or not scenes:
            issue("SCRIPT_SCHEMA", ep_path + ".scenes", "每集至少需要一个场次。")
            if not isinstance(scenes, list):
                scenes = []
        scene_count += len(scenes)
        for si, scene in enumerate(scenes):
            scene_path = f"{ep_path}.scenes[{si}]"
            if not isinstance(scene, dict):
                issue("SCRIPT_SCHEMA", scene_path, "场次须为对象。")
                continue
            identity(scene.get("id"), "SC", scene_path + ".id")
            if not _text(scene.get("title")):
                issue("SCRIPT_TEXT", scene_path + ".title", "场次标题不能为空。")
            beats = scene.get("beats")
            if not isinstance(beats, list) or not 1 <= len(beats) <= MAX_BEATS_PER_SCENE:
                issue("SCRIPT_LIMIT", scene_path + ".beats", "每场支持 1–8 个剧情节拍。")
                if not isinstance(beats, list):
                    beats = []
            beat_count += len(beats)
            for bi, beat in enumerate(beats):
                path = f"{scene_path}.beats[{bi}]"
                if not isinstance(beat, dict):
                    issue("SCRIPT_SCHEMA", path, "剧情节拍须为对象。")
                    continue
                identity(beat.get("id"), "B", path + ".id")
                if not _text(beat.get("text")):
                    issue("SCRIPT_TEXT", path + ".text", "剧情节拍原文不能为空。")
                if beat.get("required") is not True:
                    issue("SCRIPT_REQUIRED", path + ".required", "所有剧情节拍必须 required=true。")
    if scene_count > MAX_SCENES:
        issue("SCRIPT_LIMIT", "episodes", "本阶段总场次数不能超过 12。", actual=scene_count)
    if beat_count > MAX_BEATS:
        issue("SCRIPT_LIMIT", "episodes", "本阶段总剧情节拍不能超过 48。", actual=beat_count)
    return issues


@traced("story.normalize_script", kind="validation")
def normalize_script(draft, idea="", script_version=1):
    """Create a detached JSON snapshot with code-assigned stable source IDs.

    An existing screenplay/1 snapshot is validated and copied without replacing
    its IDs, version or source text. The caller persists the snapshot and hash.
    """
    if not isinstance(draft, dict):
        raise StoryError("剧本草稿必须是 JSON 对象。")
    if "schema_version" in draft:
        issues = validate_script(draft)
        if issues:
            raise StoryError("；".join(f"{i['path']}: {i['message']}" for i in issues[:10]))
        return copy.deepcopy(draft)
    if not isinstance(idea, str):
        raise StoryError("idea 须为文本。")
    if type(script_version) is not int or script_version < 1:
        raise StoryError("script_version 须为正整数。")
    for field in ("title", "logline", "outline"):
        if not _text(draft.get(field)):
            raise StoryError(f"{field} 须为非空文本。")
    setup = draft.get("setup")
    episodes = draft.get("episodes")
    if not isinstance(setup, list) or not setup or not all(_text(s) for s in setup):
        raise StoryError("setup 须为非空字符串列表。")
    if len(setup) > 999:
        raise StoryError("setup 数量超过 ID 格式范围。")
    if not isinstance(episodes, list) or not 1 <= len(episodes) <= MAX_EPISODES:
        raise StoryError("episodes 须为 1–4 集的列表。")
    script = {
        "schema_version": "screenplay/1", "script_version": script_version,
        "idea": idea.strip() or draft["logline"],
        "title": draft["title"], "logline": draft["logline"], "outline": draft["outline"],
        "setup": [{"id": f"SET{i:03}", "text": text} for i, text in enumerate(setup, 1)],
        "episodes": [],
    }
    scene_count = beat_count = 0
    for ei, episode in enumerate(episodes, 1):
        if not isinstance(episode, dict) or not _text(episode.get("title")):
            raise StoryError(f"第 {ei} 集需要非空 title。")
        scenes = episode.get("scenes")
        if not isinstance(scenes, list) or not scenes:
            raise StoryError(f"第 {ei} 集需要非空 scenes。")
        normalized_episode = {"id": f"EP{ei:03}", "title": episode["title"], "scenes": []}
        for scene in scenes:
            if not isinstance(scene, dict) or not _text(scene.get("title")):
                raise StoryError("每个场次需要非空 title。")
            beats = scene.get("beats")
            if not isinstance(beats, list) or not 1 <= len(beats) <= MAX_BEATS_PER_SCENE or not all(_text(b) for b in beats):
                raise StoryError("每场 beats 须为 1–8 个非空字符串；不接受模型指定 required。")
            scene_count += 1
            if scene_count > MAX_SCENES or beat_count + len(beats) > MAX_BEATS:
                raise StoryError("本阶段最多 12 个场次、48 个剧情节拍。")
            normalized_scene = {"id": f"SC{scene_count:03}", "title": scene["title"], "beats": []}
            for text in beats:
                beat_count += 1
                normalized_scene["beats"].append({"id": f"B{beat_count:03}", "text": text, "required": True})
            normalized_episode["scenes"].append(normalized_scene)
        script["episodes"].append(normalized_episode)
    issues = validate_script(script)
    if issues:
        raise StoryError("；".join(f"{i['path']}: {i['message']}" for i in issues[:10]))
    return script


def index_sources(script):
    """Index source context and screenplay text without treating context as beats.

    The brief, logline and outline can support entities and facts. They cannot
    substitute for required beat references on events or shots. Exact source
    matching still does not establish semantic support for a compiled claim.
    """
    issues = validate_script(script)
    if issues:
        raise StoryError("；".join(f"{i['path']}: {i['message']}" for i in issues[:10]))
    sources = {}
    for sid, field in (("SRC_IDEA", "idea"), ("SRC_LOGLINE", "logline"),
                       ("SRC_OUTLINE", "outline")):
        sources[sid] = {"id": sid, "text": script[field], "kind": field,
                        "episode_id": None, "scene_id": None,
                        "order": len(sources), "required": False}
    for item in script["setup"]:
        sources[item["id"]] = {"id": item["id"], "text": item["text"], "kind": "setup",
                               "episode_id": None, "scene_id": None,
                               "order": len(sources), "required": False}
    for episode in script["episodes"]:
        for scene in episode["scenes"]:
            for beat in scene["beats"]:
                sources[beat["id"]] = {"id": beat["id"], "text": beat["text"], "kind": "beat",
                                       "episode_id": episode["id"], "scene_id": scene["id"],
                                       "order": len(sources), "required": True}
    return sources


def _canonical_has_text(value):
    """Reject empty descriptive fields while allowing structured descriptions."""
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return bool(value) and all(_text(k) and _canonical_has_text(v) for k, v in value.items())
    if isinstance(value, list):
        return bool(value) and all(_canonical_has_text(v) for v in value)
    return False


@traced("story.validate_compilation", kind="validation")
def validate_compilation(project, script=None):
    """Check declared state, source integrity and required-beat shot coverage.

    Exact quotes prove that a source fragment exists. They do not prove that a
    generated fact, event or shot means the same thing as that source fragment.
    """
    try:
        spec = validate_production(project)
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError) as exc:
        spec = {"spec_pass": False, "state_trace": [], "issues": [
            _issue("SCHEMA", "$", "制作工程结构无效。", detail=type(exc).__name__)]}
    issues = list(spec["issues"])
    passes = {"spec_pass": spec["spec_pass"], "provenance_pass": True, "coverage_pass": True}
    coverage = []
    event_coverage = []

    def issue(category, code, path, message, **evidence):
        passes[category] = False
        issues.append(_issue(code, path, message, **evidence))

    def report():
        return {
            "scope": "declared_spec_and_exact_source_coverage; no semantic or media inspection",
            **passes, "ready_for_generation": all(passes.values()),
            "semantic_fidelity_pass": None, "visual_pass": None, "audio_pass": None,
            "media_generated": False, "issues": issues, "coverage": coverage,
            "event_coverage": event_coverage, "state_trace": spec.get("state_trace", []),
        }

    if not isinstance(project, dict):
        issue("provenance_pass", "SOURCE_SCHEMA", "$", "制作工程必须是对象。")
        passes["coverage_pass"] = False
        return report()
    if isinstance(project.get("facts"), dict) and not project["facts"]:
        issue("spec_pass", "EMPTY_FACTS", "facts", "制作工程至少须声明一个状态事实；这不证明状态抽取完整。")
    try:
        digest_json(project)
    except StoryError as exc:
        issue("provenance_pass", "SOURCE_SCHEMA", "$", str(exc))
        passes["coverage_pass"] = False
        return report()
    binding = project.get("script_source")
    if not isinstance(binding, dict) or not isinstance(binding.get("script"), dict):
        issue("provenance_pass", "MISSING_SCRIPT_SOURCE", "script_source", "需要嵌入原始剧本及 sha256。")
        passes["coverage_pass"] = False
        return report()
    embedded = binding["script"]
    selected = embedded if script is None else script
    script_issues = validate_script(selected)
    embedded_issues = validate_script(embedded) if script is not None else []
    if script_issues or embedded_issues:
        passes["provenance_pass"] = passes["coverage_pass"] = False
        issues.extend(script_issues + embedded_issues)
        return report()
    expected_hash = digest_json(selected)
    if binding.get("sha256") != expected_hash or digest_json(embedded) != expected_hash:
        issue("provenance_pass", "STALE_SCRIPT_SOURCE", "script_source.sha256",
              "剧本快照或外部剧本与记录的 SHA-256 不一致。", expected=expected_hash)
    if type(project.get("script_version")) is not int or project["script_version"] != selected["script_version"]:
        issue("provenance_pass", "SCRIPT_VERSION_MISMATCH", "script_version", "工程版本必须与来源剧本版本一致。")
    sources = index_sources(selected)
    coverage_map = {}
    for sid, source in sources.items():
        if source["required"]:
            entry = {"source_id": sid, "episode_id": source["episode_id"],
                     "scene_id": source["scene_id"], "shot_ids": []}
            coverage.append(entry)
            coverage_map[sid] = entry

    def source_refs(item, path):
        valid = set()
        if not isinstance(item, dict):
            issue("provenance_pass", "SOURCE_SCHEMA", path, "有来源的条目须为对象。")
            return valid
        refs = item.get("source_refs")
        if not isinstance(refs, list) or not refs:
            issue("provenance_pass", "MISSING_SOURCE_REFS", path + ".source_refs", "每个条目须有非空 source_refs。")
            return valid
        for ri, ref in enumerate(refs):
            ref_path = f"{path}.source_refs[{ri}]"
            if not isinstance(ref, dict):
                issue("provenance_pass", "SOURCE_SCHEMA", ref_path, "来源引用须含 source_id 与 quote。")
                continue
            sid, quote = ref.get("source_id"), ref.get("quote")
            if not isinstance(sid, str) or sid not in sources:
                issue("provenance_pass", "UNKNOWN_SOURCE", ref_path + ".source_id", "来源 ID 不存在。")
                continue
            if not _text(quote) or quote not in sources[sid]["text"]:
                issue("provenance_pass", "SOURCE_QUOTE", ref_path + ".quote", "引文必须是来源原文中的非空连续片段。", source_id=sid)
                continue
            valid.add(sid)
        return valid

    for group in ("entities", "facts"):
        items = project.get(group)
        if not isinstance(items, dict):
            issue("provenance_pass", "SOURCE_SCHEMA", group, "字段须为对象。")
            continue
        for item_id, item in items.items():
            path = f"{group}.{item_id}"
            source_refs(item, path)
            if group == "entities" and (not isinstance(item, dict)
                    or not isinstance(item.get("canonical"), dict)
                    or not _canonical_has_text(item["canonical"])):
                issue("spec_pass", "EMPTY_CANONICAL", path + ".canonical", "canonical 须有非空文字描述，所有描述字段均不能为空。")
    events = project.get("events")
    shots = project.get("shots")
    if not isinstance(events, list):
        issue("provenance_pass", "SOURCE_SCHEMA", "events", "events 须为列表。")
        events = []
    if not isinstance(shots, list):
        issue("provenance_pass", "SOURCE_SCHEMA", "shots", "shots 须为列表。")
        shots = []
    event_sources = []
    for ei, event in enumerate(events):
        refs = source_refs(event, f"events[{ei}]")
        event_sources.append({sid for sid in refs if sources[sid]["kind"] == "beat"})
        if not event_sources[-1]:
            issue("coverage_pass", "EVENT_WITHOUT_BEAT", f"events[{ei}].source_refs", "剧情事件必须引用至少一个有效剧情节拍。")
    scenes = {}
    for episode in selected["episodes"]:
        for scene in episode["scenes"]:
            scenes[scene["id"]] = (episode["id"], len(scenes))
    shot_sources = []
    scene_bindings = {}
    last_scene_order = last_beat_order = -1
    for si, shot in enumerate(shots):
        path = f"shots[{si}]"
        refs = source_refs(shot, path)
        beat_refs = {sid for sid in refs if sources[sid]["kind"] == "beat"}
        shot_sources.append(set())
        if not isinstance(shot, dict):
            continue
        if not _text(shot.get("description")):
            issue("spec_pass", "EMPTY_SHOT_DESCRIPTION", path + ".description", "镜头描述不能为空。")
        if type(shot.get("duration_s")) is not int or not 2 <= shot["duration_s"] <= 15:
            issue("spec_pass", "VIDEO_DURATION", path + ".duration_s", "本阶段镜头须为 2–15 秒整数。")
        scene_id, episode_id = shot.get("script_scene_id"), shot.get("episode_id")
        if not isinstance(scene_id, str) or scene_id not in scenes or scenes[scene_id][0] != episode_id:
            issue("provenance_pass", "SHOT_SOURCE_SCENE", path, "镜头必须标注有效且匹配的剧本分集和场次。")
        else:
            scene_order = scenes[scene_id][1]
            if scene_order < last_scene_order:
                issue("coverage_pass", "SHOT_SOURCE_ORDER", path, "镜头不能倒退到较早的剧本场次。")
            last_scene_order = max(last_scene_order, scene_order)
            entity_scene_id = shot.get("scene_id")
            if isinstance(entity_scene_id, str):
                bound_scene = scene_bindings.setdefault(scene_id, entity_scene_id)
                if entity_scene_id != bound_scene:
                    issue("provenance_pass", "SCENE_BINDING_CONFLICT", path + ".scene_id",
                          "同一剧本场次的所有镜头必须绑定同一个场景实体。",
                          script_scene_id=scene_id, expected_scene_id=bound_scene,
                          actual_scene_id=entity_scene_id)
        for sid in beat_refs:
            source = sources[sid]
            if source["scene_id"] != scene_id or source["episode_id"] != episode_id:
                issue("provenance_pass", "CROSS_SCENE_SOURCE", path + ".source_refs", "不得引用其他集或场次的剧情节拍充当本镜头来源。", source_id=sid)
            else:
                shot_sources[-1].add(sid)
                if _text(shot.get("id")) and shot["id"] not in coverage_map[sid]["shot_ids"]:
                    coverage_map[sid]["shot_ids"].append(shot["id"])
        if not shot_sources[-1]:
            issue("coverage_pass", "SHOT_WITHOUT_BEAT", path + ".source_refs", "镜头须引用所属场次的至少一个有效剧情节拍。")
        else:
            orders = [sources[sid]["order"] for sid in shot_sources[-1]]
            if min(orders) < last_beat_order:
                issue("coverage_pass", "SHOT_SOURCE_ORDER", path, "镜头须遵循原剧本剧情节拍顺序，不支持倒叙。")
            last_beat_order = max(last_beat_order, max(orders))
    for entry in coverage:
        if not entry["shot_ids"]:
            issue("coverage_pass", "UNCOVERED_BEAT", "coverage." + entry["source_id"], "必需剧情节拍没有对应镜头。")
    committed_shots = {t.get("shot_id") for t in spec.get("state_trace", [])
                       if isinstance(t, dict) and t.get("committed") is True and isinstance(t.get("shot_id"), str)}
    for ei, event in enumerate(events):
        if not isinstance(event, dict) or not _text(event.get("id")):
            issue("coverage_pass", "EVENT_COVERAGE", f"events[{ei}]", "事件须有有效 ID 才能追踪执行。")
            continue
        used_by = []
        for si, shot in enumerate(shots):
            if (not isinstance(shot, dict) or not isinstance(shot.get("event_ids"), list)
                    or event["id"] not in shot["event_ids"]):
                continue
            if not event_sources[ei] <= shot_sources[si]:
                issue("coverage_pass", "EVENT_SOURCE_NOT_IN_SHOT", f"shots[{si}].source_refs", "执行事件的镜头必须同时引用该事件的所有来源剧情节拍。", event_id=event["id"])
            if isinstance(shot.get("id"), str) and shot["id"] in committed_shots:
                used_by.append(shot["id"])
        event_coverage.append({"event_id": event["id"], "shot_ids": used_by})
        if not used_by:
            issue("coverage_pass", "UNEXECUTED_EVENT", f"events[{ei}]", "事件没有在通过状态检查的镜头中执行。", event_id=event["id"])
    return report()
