"""Independent screenplay agent. Publication hands a checked snapshot to a caller-owned sink."""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

from pianchang.agent_loop import AgentLoop, Tool
from pianchang.bailian_text import BailianTextClient, load_text_config
from pianchang.story_agent import _write_json, script_markdown
from pianchang.story_spec import (MAX_BEATS, MAX_BEATS_PER_SCENE, MAX_EPISODES, MAX_SCENES, StoryError,
                        digest_json, index_sources, normalize_script, script_structure, validate_script,
                        validate_structure_constraints)
from trace_system import current_context, trace, traced


TEXT = {"type": "string", "minLength": 1}
EMPTY = {"type": "object", "properties": {}, "required": []}


def _object(**properties):
    return {"type": "object", "properties": properties, "required": list(properties)}


def _array(items):
    return {"type": "array", "items": items, "minItems": 1}


DRAFT = _object(title=TEXT, logline=TEXT, outline=TEXT, setup=_array(TEXT),
                episodes=_array(_object(title=TEXT, scenes=_array(_object(title=TEXT, beats=_array(TEXT))))))


def _beat(script, beat_id):
    for episode in script["episodes"]:
        for scene in episode["scenes"]:
            for beat in scene["beats"]:
                if beat["id"] == beat_id:
                    return beat
    raise StoryError(f"找不到目标节拍 {beat_id}")


def _check(path, expected, actual):
    return {"path": path, "expected": expected, "actual": actual,
            "status": "PASS" if expected == actual else "FAIL"}


def _constraints(constraints):
    values = dict(constraints or {})
    if set(values) - {"episodes", "scenes_per_episode", "beats_per_scene", "required_terms"}:
        raise StoryError("含未支持的剧本约束。")
    validate_structure_constraints({key: value for key, value in values.items() if key != "required_terms"})
    terms = values.get("required_terms", [])
    if not isinstance(terms, list) or any(not isinstance(t, str) or not t.strip() for t in terms):
        raise StoryError("required_terms 须为非空文本组成的列表。")
    return values


class ScriptWorkspace:
    def __init__(self, directory, *, story_id, goal, publish, mode, script, base_revision, beat_id, constraints):
        if mode not in {"create", "import", "revise"} or not callable(publish):
            raise StoryError("剧本 Agent 需要有效的模式与发布接口。")
        if not isinstance(story_id, str) or not story_id.strip():
            raise StoryError("须指定长期有效的故事 ID。")
        if not isinstance(goal, str) or not 1 <= len(goal.strip()) <= 6000:
            raise StoryError("剧本任务目标须为 1～6000 字。")
        if mode == "create" and script is not None:
            raise StoryError("新创作任务不能指定原稿。")
        if mode != "revise" and (base_revision is not None or beat_id is not None):
            raise StoryError("基础版本和节拍 ID 只用于改稿。")
        if mode != "create":
            issues = validate_script(script)
            if issues:
                raise StoryError(f"输入须为可验证的冻结剧本：{issues[0]['path']} {issues[0]['message']}")
        if mode == "revise":
            if not base_revision:
                raise StoryError("改稿须指定已发布的基础版本。")
            _beat(script, beat_id)
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.story_id = story_id
        self.goal = goal
        self.mode = mode
        self.source = copy.deepcopy(script)
        self.constraints = _constraints(constraints)
        self.base_revision = base_revision
        self.beat_id = beat_id
        self.candidate = copy.deepcopy(script)
        self.report = None
        self.receipt = None
        self.publish_started = False
        self.publisher = publish

    def tools(self):
        tools = [
            Tool("read_script", "读取当前剧本、稳定对象 ID 与任务约束。",
                 EMPTY, self.read_script),
            Tool("check_script", "核验剧本结构、用户指定的约束及改稿范围，返回实际分集、场次和节拍分布。",
                 EMPTY, self.check_script),
            Tool("publish_script", "把最近一次检查通过的同一候选发布为不可变资产。只有拿到回执才能完成。",
                 EMPTY, self.publish_script, effect="write"),
        ]
        if self.mode == "create":
            tools.append(Tool("write_draft", "创作或修复完整剧本草稿。按创意、转折和节奏安排分集，"
                              "按时间、地点或行动的变化划分场次，将情节推进写成可拍摄的节拍。"
                              "遵循创意中的明确要求和 constraints 给出的数量，其余数量由你按剧情决定。"
                              "每集的场数、每场的节拍数可以不同。单份剧本格式的容量上限为"
                              f"{MAX_EPISODES} 集、总计 {MAX_SCENES} 场、每场最多 {MAX_BEATS_PER_SCENE} 个节拍、"
                              f"全剧总计 {MAX_BEATS} 个节拍。setup 与 beats 为文本数组。",
                              DRAFT, self.write_draft, effect="write"))
        if self.mode == "revise":
            tools.append(Tool("replace_beat", "根据改稿目标写入指定节拍的完整新文本，保留未要求修改的动作。",
                              _object(beat_id=TEXT, text=TEXT), self.replace_beat, effect="write"))
        return tools

    @traced("script_agent.snapshot", kind="tool")
    def snapshot(self):
        return {"role": "独立剧本 Agent，交付物是通过结构检查的已发布剧本", "mode": self.mode,
                "story_id": self.story_id, "base_revision": self.base_revision, "beat_id": self.beat_id,
                "constraints": self.constraints, "candidate_sha256": digest_json(self.candidate) if self.candidate else None,
                "validation": self.report and self.report["status"], "published": self.receipt is not None,
                "publication_attempted": self.publish_started}

    @traced("script_agent.read", kind="tool")
    def read_script(self):
        return {"script": copy.deepcopy(self.candidate), "constraints": self.constraints,
                "base_revision": self.base_revision, "target_beat_id": self.beat_id}

    def _save_candidate(self, candidate):
        if self.publish_started:
            raise StoryError("发布已开始，候选已冻结。")
        _write_json(self.directory / "candidate.json", candidate)
        self.candidate = candidate
        self.report = None
        return {"candidate_sha256": digest_json(candidate), "needs_check": True}

    @traced("script_agent.write_draft", kind="tool")
    def write_draft(self, title, logline, outline, setup, episodes):
        if self.mode != "create":
            raise StoryError("本任务不允许全文重写。")
        draft = dict(title=title, logline=logline, outline=outline, setup=setup, episodes=episodes)
        return self._save_candidate(normalize_script(draft, idea=self.goal))

    @traced("script_agent.replace_beat", kind="tool")
    def replace_beat(self, beat_id, text):
        if self.mode != "revise" or beat_id != self.beat_id:
            raise StoryError("只可修改任务指定的稳定节拍 ID。")
        if not isinstance(text, str) or not text.strip():
            raise StoryError("新节拍文本不能为空。")
        revised = copy.deepcopy(self.source)
        revised["script_version"] += 1
        _beat(revised, beat_id)["text"] = text.strip()
        return {**self._save_candidate(revised), "beat_id": beat_id, "new_text": text.strip()}

    def _evaluate(self):
        checks = []
        issues = validate_script(self.candidate)
        checks.append(_check("script.schema", [], issues))
        if issues:
            return checks
        script = self.candidate
        for path, actual, key in self._counts(script):
            if key in self.constraints:
                checks.append(_check(path, self.constraints[key], actual))
        required_text = json.dumps(script, ensure_ascii=False)
        if self.mode == "import":
            checks.append(_check("import.source_sha256", digest_json(self.source), digest_json(script)))
        if self.mode == "revise":
            before = _beat(self.source, self.beat_id)["text"]
            after = _beat(script, self.beat_id)["text"]
            expected = copy.deepcopy(self.source)
            expected["script_version"] += 1
            _beat(expected, self.beat_id)["text"] = after
            checks.append(_check(f"beats.{self.beat_id}.changed", True, before != after))
            checks.append(_check("revision.only_target_changed", digest_json(expected), digest_json(script)))
            required_text = after
        for term in self.constraints.get("required_terms", []):
            checks.append(_check(f"required_terms.{term}", True, term in required_text))
        return checks

    @staticmethod
    def _counts(script):
        yield "episodes", len(script["episodes"]), "episodes"
        for episode in script["episodes"]:
            yield episode["id"] + ".scenes", len(episode["scenes"]), "scenes_per_episode"
            for scene in episode["scenes"]:
                yield scene["id"] + ".beats", len(scene["beats"]), "beats_per_scene"

    @traced("script_agent.check", kind="validation")
    def check_script(self):
        checks = self._evaluate()
        self.report = {"status": "PASS" if all(x["status"] == "PASS" for x in checks) else "FAIL",
                       "checks": checks, "script_sha256": digest_json(self.candidate),
                       "semantic_fidelity": "UNREVIEWED", "base_revision": self.base_revision,
                       "target_beat_id": self.beat_id, "requested_constraints": self.constraints,
                       "actual_structure": script_structure(self.candidate) if checks[0]["status"] == "PASS" else None}
        _write_json(self.directory / "script-validation.json", self.report)
        return copy.deepcopy(self.report)

    @traced("script_agent.publish", kind="tool")
    def publish_script(self):
        if self.receipt is not None:
            return copy.deepcopy(self.receipt)
        if self.publish_started:
            raise StoryError("上一次发布结果未确认。请核对资产和消息记录，不在当前任务中重复提交。")
        if (not self.report or self.report["status"] != "PASS"
                or self.report["script_sha256"] != digest_json(self.candidate)):
            raise StoryError("当前候选需要先通过 check_script，再发布。")
        self.publish_started = True
        receipt = self.publisher(copy.deepcopy(self.candidate), copy.deepcopy(self.report))
        if not isinstance(receipt, dict) or not receipt:
            raise StoryError("发布接口没有返回可验证的资产回执。")
        self.receipt = copy.deepcopy(receipt)
        _write_json(self.directory / "published-script.json", self.candidate)
        (self.directory / "published-script.md").write_text(script_markdown(self.candidate), encoding="utf-8")
        _write_json(self.directory / "publication.json", self.receipt)
        return copy.deepcopy(self.receipt)

    @traced("script_agent.finish_check", kind="validation")
    def finish_check(self):
        checks = [_check("publication.receipt", True, bool(self.receipt)),
                  _check("validation.status", "PASS", (self.report or {}).get("status"))]
        if self.receipt is not None:
            package = json.loads((self.directory / "published-script.json").read_text(encoding="utf-8"))
            checks.append(_check("publication.script_sha256", digest_json(self.candidate), digest_json(package)))
            receipt = json.loads((self.directory / "publication.json").read_text(encoding="utf-8"))
            checks.append(_check("publication.receipt_sha256", digest_json(self.receipt), digest_json(receipt)))
        checks.extend(self._evaluate())
        completed = all(check["status"] == "PASS" for check in checks)
        return {"complete": completed, "checks": checks, "receipt": self.receipt,
                "summary": "剧本已发布，结构和任务约束通过检查。" if completed else "剧本尚未通过检查并发布。"}


def run_script_agent(client, directory, *, story_id, goal, publish, mode, script=None,
                     base_revision=None, beat_id=None, constraints=None, links=None, progress=None,
                     max_steps=12, trace_attributes=None):
    """Start an independent agent root and persist the private model/tool transcript in directory."""
    parent = current_context()
    causal = list(links or [])
    if parent:
        causal.append({**parent, "relation": "submitted_by"})
    inputs = dict(story_id=story_id, mode=mode, goal=goal, script=script, base_revision=base_revision,
                  beat_id=beat_id, constraints=constraints or {}, max_steps=max_steps)
    with trace("agent.task.script", kind="agent", inputs=inputs, links=causal,
               attributes={**(trace_attributes or {}), "agent": "script", "story_id": story_id}) as root:
        workspace = ScriptWorkspace(directory, story_id=story_id, goal=goal, publish=publish, mode=mode,
                                    script=script, base_revision=base_revision, beat_id=beat_id,
                                    constraints=constraints)
        from pi_bridge.runtime import make_loop
        loop = make_loop(client, workspace.tools(), directory, role="script", snapshot=workspace.snapshot,
                         finish_check=workspace.finish_check, progress=progress)
        loop_result = loop.run(goal, max_steps=max_steps)
        result = {"trace_id": root.context["trace_id"], "loop_result": loop_result,
                  "receipt": workspace.receipt, "script": workspace.candidate, "validation": workspace.report}
        root.set_output(result)
        if not loop_result["task_complete"]:
            root.set_status("ERROR" if loop_result["status"] == "error" else "UNKNOWN")
        return result


@traced("script_agent.publish_revision", kind="tool")
def publish_script_revision(store, candidate, report, *, asset_id, story_id, base_revision=None,
                            source_sha256=None, correlation_id=None, causation_id=None):
    """Publish a checked screenplay with its actual diff and a durable publication event."""
    sha = digest_json(candidate)
    if report.get("status") != "PASS" or report.get("script_sha256") != sha:
        raise StoryError("发布候选与通过检查的剧本哈希不一致。")
    if report.get("base_revision") != base_revision:
        raise StoryError("校验报告的基础版本与发布目标不符。")
    previous = {}
    if base_revision is not None:
        base = store.get_asset(asset_id, base_revision)
        if not source_sha256 or base["sha256"] != source_sha256:
            raise StoryError("输入剧本的哈希必须与已发布的基础版本相符。")
        previous = index_sources(base["content"])
    current = index_sources(candidate)
    changed = sorted(sid for sid in previous.keys() | current.keys()
                     if previous.get(sid) != current.get(sid))
    published = store.publish_asset(asset_id, candidate, kind="script", producer="script",
                                    story_id=story_id, parent_revision=base_revision,
                                    topic="script.revision_published", correlation_id=correlation_id,
                                    causation_id=causation_id, event_payload={"changed_source_ids": changed,
                                    "validation_sha256": digest_json(report), "validation_report": report})
    saved = store.get_asset(asset_id, published["revision_id"])
    if saved["sha256"] != sha or digest_json(saved["content"]) != sha:
        raise StoryError("发布后的资产内容未通过哈希复核。")
    return {**{key: saved[key] for key in ("asset_id", "revision_id", "sha256", "event_id", "trace_id")},
            "changed_source_ids": changed}


@traced("cli.script_collab_agent", kind="cli")
def main(argv=None):
    parser = argparse.ArgumentParser(description="独立剧本 Agent，输出可阅读剧本、结构化剧本及证据。")
    parser.add_argument("mode", choices=("create", "import", "revise"))
    parser.add_argument("--goal", required=True)
    parser.add_argument("--story-id", required=True)
    parser.add_argument("--output", required=True, type=Path, help="每次运行使用新的目录")
    parser.add_argument("--script", type=Path, help="import / revise 使用的冻结剧本 JSON")
    parser.add_argument("--base-revision", help="revise 使用的来源资产版本")
    parser.add_argument("--store", type=Path, help="可选的协作 SQLite 文件，供订阅方接收发布事件")
    parser.add_argument("--asset-id", help="协作库中的稳定剧本资产 ID，默认 script:<story-id>")
    parser.add_argument("--correlation-id", help="关联本次协作的任务组")
    parser.add_argument("--causation-id", help="触发本次改稿的消息 ID")
    parser.add_argument("--beat-id", help="revise 中唯一允许修改的稳定节拍 ID")
    parser.add_argument("--require-term", action="append", default=[], help="必须保留的文字，可重复")
    for field in ("episodes", "scenes-per-episode", "beats-per-scene"):
        parser.add_argument("--" + field, type=int, help="指定此项数量；省略时由剧本 Agent 按创意安排")
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args(argv)
    script = json.loads(args.script.read_text(encoding="utf-8-sig")) if args.script else None
    if not args.store and (args.asset_id or args.correlation_id or args.causation_id):
        parser.error("协作参数须配合 --store 使用。")
    asset_id = args.asset_id or "script:" + args.story_id
    store = None
    if args.store:
        from pianchang.collab_store import CollabStore
        store = CollabStore(args.store)
        if args.mode == "revise":
            if not args.base_revision:
                parser.error("改稿须指定 --base-revision")
            base = store.get_asset(asset_id, args.base_revision)
            if script is not None and digest_json(script) != base["sha256"]:
                parser.error("提供的剧本文件与指定基础版本不一致。")
            script = base["content"]
    constraints = {k: v for k, v in vars(args).items()
                   if k in {"episodes", "scenes_per_episode", "beats_per_scene"} and v is not None}
    if args.require_term:
        constraints["required_terms"] = args.require_term

    def deliver_local_package(candidate, report):
        if store is not None:
            return publish_script_revision(store, candidate, report, asset_id=asset_id, story_id=args.story_id,
                                           base_revision=args.base_revision, source_sha256=digest_json(script),
                                           correlation_id=args.correlation_id, causation_id=args.causation_id)
        return {"asset_id": args.story_id, "revision": candidate["script_version"],
                "kind": "script", "sha256": digest_json(candidate),
                "base_revision": args.base_revision, "validation_sha256": digest_json(report),
                "artifact_ref": str((args.output / "published-script.json").resolve())}

    result = run_script_agent(BailianTextClient(load_text_config(args.env_file)), args.output,
                              story_id=args.story_id, mode=args.mode, goal=args.goal,
                              script=script, base_revision=args.base_revision, beat_id=args.beat_id,
                              publish=deliver_local_package, constraints=constraints, max_steps=args.max_steps)
    print(json.dumps({"trace_id": result["trace_id"], "status": result["loop_result"]["status"],
                      "receipt": result["receipt"], "summary": result["loop_result"]["summary"]}, ensure_ascii=False))
    return 0 if result["loop_result"]["task_complete"] else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
