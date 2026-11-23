"""Compile ordered creative shots into scene-length Seedance generation tasks."""
from copy import deepcopy
from itertools import groupby

from pianchang.reference_assets import pin_issues


def group_generation_tasks(project, request_limit):
    if type(request_limit) is not int or request_limit < 1:
        raise ValueError("生成请求上限必须为正整数。")
    result = deepcopy(project)
    shots = result["shots"]
    if result.get("generation_request_limit") == request_limit and all(s.get("creative_shots") for s in shots):
        return result
    groups = [[s] for s in shots]
    if len(groups) > request_limit:
        groups = [list(members) for _, members in groupby(shots, key=lambda s: (s["episode_id"], s["script_scene_id"]))]
    if len(groups) > request_limit:
        raise ValueError("当前场次数超过视频请求上限，须重新安排制作规格。")
    tasks = []
    owners = {s["id"]: f"G{i:03d}" for i, group in enumerate(groups, 1) for s in group}
    for group in groups:
        first = group[0]
        task = deepcopy(first)
        task.update(id=owners[first["id"]], creative_shots=group,
                    duration_s=max(4, min(15, sum(s["duration_s"] for s in group))),
                    description="\n".join(f"镜头{i}（{s['id']}）：{s['description']}" for i,s in enumerate(group,1)))
        for field in ("entity_ids", "event_ids", "source_refs", "required_refs"):
            merged = []
            for s in group:
                for value in s.get(field, []):
                    if value not in merged:
                        merged.append(value)
            task[field] = merged
        if pin_issues(task["required_refs"]) or any(s["scene_id"] != first["scene_id"] for s in group):
            raise ValueError("同一生成任务的参考版本或场景冲突，请调整分组。")
        task["input_versions"] = {k:v for s in group for k,v in s["input_versions"].items()}
        task["continuity_from"] = list(dict.fromkeys(owners[k] for s in group for k in s["continuity_from"]
                                                     if owners[k] != task["id"]))
        # Internal state transitions stay with their creative shot. The task
        # contract exposes only conditions that hold at its actual boundaries.
        events = {e["id"]:e for e in project["events"]}
        changed = set()
        task["requires"] = {}
        task["expected_after"] = {}
        for shot in group:
            for k,v in shot["requires"].items():
                if k not in changed:
                    task["requires"][k] = v
            for eid in shot["event_ids"]:
                changed.update(events[eid]["changes"])
                task["expected_after"].update(events[eid]["changes"])
            task["expected_after"].update(shot["expected_after"])
        tasks.append(task)
    result.update(shots=tasks, generation_request_limit=request_limit)
    return result
