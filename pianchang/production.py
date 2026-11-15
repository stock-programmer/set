"""Step 1: validate declared story state. This does NOT inspect video or call AI."""
from __future__ import annotations

import argparse
import copy
import json
import math
from pathlib import Path
from pianchang.reference_assets import pin_issues
from trace_system import traced


def same(left, right):
    """JSON booleans must not compare equal to numbers."""
    return type(left) is type(right) and left == right


@traced("production.validate", kind="validation")
def validate(project):
    issues = []

    def issue(code, path, message, severity="error", **evidence):
        issues.append(dict(code=code, path=path, severity=severity,
                           message=message, evidence=evidence))

    def report(state=None, trace=None):
        return {
            "scope": "declared_spec_only; no media inspection or AI generation",
            "spec_pass": not any(i["severity"] == "error" for i in issues),
            "visual_pass": None,
            "audio_pass": None,
            "media_generated": False,
            "issues": issues,
            "state_trace": trace or [],
            "final_declared_state": state or {},
        }

    if not isinstance(project, dict):
        issue("SCHEMA", "$", "Project must be an object")
        return report()
    for field, kind in (("entities", dict), ("facts", dict), ("events", list), ("shots", list)):
        if not isinstance(project.get(field), kind):
            issue("SCHEMA", field, f"Expected {kind.__name__}")
    if project.get("schema_version") != "0.1":
        issue("SCHEMA_VERSION", "schema_version", "Supported version is 0.1")
    if type(project.get("script_version")) is not int or project["script_version"] < 1:
        issue("SCHEMA", "script_version", "Expected positive integer")
    if project.get("timeline_mode") != "chronological":
        issue("UNSUPPORTED_TIMELINE", "timeline_mode", "v0.1 supports chronological shots only")
    if "reference_story_id" in project:
        scope = project["reference_story_id"]
        if not isinstance(scope, str) or not scope.strip() or scope != scope.strip() or len(scope) > 256:
            issue("REFERENCE_SCOPE", "reference_story_id", "参考资产必须归属于有效的故事 ID。")
    if issues:
        return report()

    entities, facts = project["entities"], project["facts"]
    versions = {"script": project["script_version"]}
    for entity_id, entity in entities.items():
        path = f"entities.{entity_id}"
        if not isinstance(entity, dict):
            issue("SCHEMA", path, "Entity must be an object")
            continue
        if entity.get("kind") not in ("character", "scene", "prop", "costume"):
            issue("SCHEMA", path, "Unsupported entity kind")
        version = entity.get("version")
        if type(version) is not int or version < 1:
            issue("SCHEMA", path + ".version", "Expected positive integer")
        else:
            versions[entity_id] = version
        if not isinstance(entity.get("canonical"), dict):
            issue("SCHEMA", path + ".canonical", "Expected canonical description object")
        if not isinstance(entity.get("references"), list):
            issue("SCHEMA", path + ".references", "Expected list")

    state = {}
    for name, fact in facts.items():
        path = f"facts.{name}"
        if not isinstance(fact, dict) or not all(k in fact for k in ("initial", "mutable", "allowed")):
            issue("SCHEMA", path, "Fact needs initial, mutable, allowed")
            continue
        if name.split(".", 1)[0] not in entities:
            issue("UNKNOWN_ENTITY", path, "Fact owner is not registered")
        if type(fact["mutable"]) is not bool or not isinstance(fact["allowed"], list) or not fact["allowed"]:
            issue("SCHEMA", path, "mutable must be boolean; allowed must be nonempty list")
            continue
        if not any(same(fact["initial"], item) for item in fact["allowed"]):
            issue("VALUE_DOMAIN", path, "Initial value outside allowed values")
        state[name] = copy.deepcopy(fact["initial"])

    def check_assignments(assignments, path, allow_changes=False):
        if not isinstance(assignments, dict):
            issue("SCHEMA", path, "Expected fact/value object")
            return
        for key, value in assignments.items():
            if key not in facts:
                issue("UNKNOWN_FACT", path + "." + key, "Fact is not registered")
            elif isinstance(facts[key], dict) and isinstance(facts[key].get("allowed"), list):
                if not any(same(value, item) for item in facts[key]["allowed"]):
                    issue("VALUE_DOMAIN", path + "." + key, "Value outside allowed values")
                if allow_changes and facts[key].get("mutable") is not True:
                    issue("IMMUTABLE_FACT", path + "." + key, "Story events cannot change canonical facts")

    events = {}
    for index, event in enumerate(project["events"]):
        path = f"events[{index}]"
        if not isinstance(event, dict) or not isinstance(event.get("id"), str):
            issue("SCHEMA", path, "Event needs string id")
            continue
        if event["id"] in events:
            issue("DUPLICATE_ID", path, "Duplicate event id")
        events[event["id"]] = event
        if not isinstance(event.get("reason"), str) or not event["reason"].strip():
            issue("MISSING_REASON", path, "State changes need a narrative reason")
        check_assignments(event.get("requires"), path + ".requires")
        check_assignments(event.get("changes"), path + ".changes", allow_changes=True)

    seen_shots = set()
    referenced_entities = set()
    for index, shot in enumerate(project["shots"]):
        path = f"shots[{index}]"
        if not isinstance(shot, dict) or not isinstance(shot.get("id"), str):
            issue("SCHEMA", path, "Shot needs string id")
            continue
        if shot["id"] in seen_shots:
            issue("DUPLICATE_ID", path, "Duplicate shot id")
        if not isinstance(shot.get("continuity_from"), list):
            issue("SCHEMA", path, "continuity_from must be a list")
        else:
            for previous in shot["continuity_from"]:
                if not isinstance(previous, str) or previous not in seen_shots:
                    issue("INVALID_DEPENDENCY", path, "Dependency must point to an earlier shot")
        seen_shots.add(shot["id"])
        duration = shot.get("duration_s")
        if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
            issue("SCHEMA", path + ".duration_s", "Expected finite positive duration")
        scene_id = shot.get("scene_id")
        if not isinstance(scene_id, str) or not isinstance(entities.get(scene_id), dict) or entities[scene_id].get("kind") != "scene":
            issue("UNKNOWN_SCENE", path, "Shot must reference a registered scene")
        ids = shot.get("entity_ids")
        if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
            issue("SCHEMA", path, "entity_ids must be string list")
            ids = []
        if len(ids) != len(set(ids)):
            issue("DUPLICATE_REFERENCE", path, "entity_ids contains duplicates")
        for entity_id in ids:
            if entity_id not in entities:
                issue("UNKNOWN_ENTITY", path, "Shot references unknown entity", entity_id=entity_id)
        if "required_refs" in shot:
            scope = {key: entities[key].get("kind") for key in [*ids, scene_id]
                     if isinstance(key, str) and isinstance(entities.get(key), dict)}
            checks = pin_issues(shot["required_refs"], path=path + ".required_refs", subjects=scope)
            for check in checks:
                issue("INVALID_REQUIRED_REFERENCE", check["path"], "镜头参考的版本、对象或用途无效。",
                      expected=check["expected"], actual=check["actual"], status=check["status"])
            if not checks:
                referenced_entities.update(pin["subject_id"] for pin in shot["required_refs"])
        required_versions = set(ids) | {"script"}
        if isinstance(scene_id, str):
            required_versions.add(scene_id)
        pins = shot.get("input_versions")
        if not isinstance(pins, dict):
            issue("SCHEMA", path, "input_versions must be object")
        else:
            for item in required_versions - pins.keys():
                issue("UNPINNED_INPUT", path, "Referenced input must have pinned version", input=item)
            for item, version in pins.items():
                if item not in versions:
                    issue("UNKNOWN_INPUT", path, "Unknown pinned input", input=item)
                elif not same(version, versions[item]):
                    issue("STALE_INPUT", path, "Pinned version differs from project version",
                          input=item, expected=versions[item], actual=version)
        check_assignments(shot.get("requires"), path + ".requires")
        check_assignments(shot.get("expected_after"), path + ".expected_after")
        event_ids = shot.get("event_ids")
        if not isinstance(event_ids, list) or not all(isinstance(x, str) for x in event_ids):
            issue("SCHEMA", path, "event_ids must be string list")
        else:
            for event_id in event_ids:
                if event_id not in events:
                    issue("UNKNOWN_EVENT", path, "Shot references unknown event", event_id=event_id)
    if not project["shots"]:
        issue("NO_SHOTS", "shots", "At least one shot is required")
    if any(i["severity"] == "error" for i in issues):
        return report(state)

    used_events = set()
    trace = []

    def matches(requirements, current, path, code):
        ok = True
        for key, expected in requirements.items():
            if not same(current[key], expected):
                issue(code, path + "." + key, "Declared state contradicts accumulated story state",
                      expected=expected, actual=current[key])
                ok = False
        return ok

    for shot in project["shots"]:
        path = f"shot:{shot['id']}"
        before = copy.deepcopy(state)
        candidate = copy.deepcopy(state)
        errors_before = len(issues)
        if matches(shot["requires"], candidate, path + ".requires", "CONTINUITY"):
            local_events = set()
            for event_id in shot["event_ids"]:
                event = events[event_id]
                if event_id in used_events or event_id in local_events:
                    issue("EVENT_REPLAY", path, "Event already applied", event_id=event_id)
                    break
                if not matches(event["requires"], candidate, f"event:{event_id}", "EVENT_PRECONDITION"):
                    break
                candidate.update(copy.deepcopy(event["changes"]))
                local_events.add(event_id)
            matches(shot["expected_after"], candidate, path + ".expected_after", "UNEXPLAINED_CHANGE")
            if len(issues) == errors_before:
                state = candidate
                used_events.update(local_events)
        trace.append({"shot_id": shot["id"], "committed": len(issues) == errors_before,
                      "before": before, "after": copy.deepcopy(state)})

    for event_id in events.keys() - used_events:
        issue("UNAPPLIED_EVENT", f"event:{event_id}", "Script event has no successful shot execution")
    for entity_id, entity in entities.items():
        if not entity["references"] and entity_id not in referenced_entities:
            issue("MISSING_REFERENCE", f"entities.{entity_id}",
                  "No approved media reference; text identity is not visual identity", "warning")
        elif entity["references"] and entity_id not in referenced_entities:
            issue("UNBOUND_REFERENCE", f"entities.{entity_id}.references",
                  "只有设定中的参考声明，尚无镜头通过 required_refs 锁定媒体版本。", "warning")
    return report(state, trace)


@traced("production.impact", kind="tool")
def impact(project, changed_input):
    """Conservative recheck candidates; never automatic rerender instructions."""
    known = set(project["entities"]) | {"script"}
    if changed_input not in known:
        raise ValueError(f"Unknown input: {changed_input}")
    direct = {s["id"] for s in project["shots"] if changed_input in s["input_versions"]}
    affected = set(direct)
    while True:
        addition = {s["id"] for s in project["shots"] if set(s["continuity_from"]) & affected}
        if addition <= affected:
            break
        affected |= addition
    order = [s["id"] for s in project["shots"]]
    return {"changed_input": changed_input,
            "direct_recheck": [s for s in order if s in direct],
            "downstream_recheck": [s for s in order if s in affected - direct],
            "policy": "Recheck first. This report does not request or perform regeneration."}


@traced("cli.production", kind="cli")
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "impact"))
    parser.add_argument("project", type=Path)
    parser.add_argument("--changed")
    args = parser.parse_args()
    try:
        project = json.loads(args.project.read_text(encoding="utf-8"))
        result = validate(project)
        if args.command == "impact":
            if not result["spec_pass"]:
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 1
            if not args.changed:
                parser.error("impact requires --changed")
            result = impact(project, args.changed)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("spec_pass", True) else 1
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
