"""Offline behavior tests for screenplay drafting, compilation and recovery."""
from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from pianchang.bailian_text import TextConfig, TextError
from pianchang.bailian_video import VideoError
from pianchang.story_agent import PREVIOUS_PROMPT_VERSION, StoryRunner, _parser, query_context
from trace_system import TraceError
from pianchang.story_spec import StoryError, digest_json, validate_compilation
from pianchang.video_agent import compile_shot


ROOT = Path(__file__).resolve().parents[1]


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def draft_from_script(script):
    """A model draft has prose lists; source IDs belong to the compiler."""
    return {
        "title": script["title"], "logline": script["logline"], "outline": script["outline"],
        "setup": [item["text"] for item in script["setup"]],
        "episodes": [{
            "title": episode["title"],
            "scenes": [{"title": scene["title"], "beats": [b["text"] for b in scene["beats"]]}
                       for scene in episode["scenes"]],
        } for episode in script["episodes"]],
    }


class FakeClient:
    """Replay explicit provider results, failing immediately on unexpected calls."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def complete_json(self, messages):
        self.calls.append(copy.deepcopy(messages))
        if not self.responses:
            raise AssertionError("Unexpected model call: tests must never contact the network")
        data = self.responses.pop(0)
        if isinstance(data, BaseException):
            raise data
        return {
            "data": copy.deepcopy(data), "request_id": f"offline-{len(self.calls)}",
            "model": "qwen-plus", "finish_reason": "stop",
            "usage": {"prompt_tokens": 200, "completion_tokens": 500, "total_tokens": 700},
        }


class StoryWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "run"
        self.config = TextConfig(api_key="sk-offline-story-test")
        self.script = read_json(ROOT / "fixtures" / "story_script.json")
        self.project = read_json(ROOT / "fixtures" / "story_project.json")
        self.proposal = copy.deepcopy(self.project)
        self.proposal.pop("script_source")
        self.draft = draft_from_script(self.script)
        self.idea = self.script["idea"]
        silence = redirect_stderr(io.StringIO())
        silence.__enter__()
        self.addCleanup(silence.__exit__, None, None, None)

    def test_build_freezes_sources_and_saves_valid_project_without_media_claims(self):
        client = FakeClient(self.draft, self.proposal)
        result = StoryRunner(self.config, client).build(self.idea, self.directory)
        self.assertEqual(result["stage"], "PROJECT_READY")
        self.assertTrue(result["ready_for_generation"])
        self.assertFalse(result["media_generated"])
        for field in ("semantic_fidelity_pass", "visual_pass", "audio_pass", "cost"):
            self.assertIsNone(result[field])
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(result["recorded_model_calls"], 2)
        self.assertEqual(result["model_usage"][0]["total_tokens"], 700)
        frozen = read_json(result["script_path"])
        project = read_json(result["project_path"])
        self.assertEqual(frozen, self.script)
        self.assertEqual(project["script_source"]["script"], frozen)
        self.assertEqual(project["script_source"]["sha256"], digest_json(frozen))
        self.assertTrue(validate_compilation(project, frozen)["ready_for_generation"])
        self.assertIn("[B002]", (self.directory / "script.md").read_text(encoding="utf-8"))
        self.assertTrue((self.directory / "compile-01-candidate.json").is_file())
        self.assertTrue((self.directory / "compile-01-report.json").is_file())
        for path in self.directory.glob("*.json"):
            self.assertNotIn(self.config.api_key, path.read_text(encoding="utf-8"))

    def test_repeated_build_reuses_completed_project_without_more_model_calls(self):
        client = FakeClient(self.draft, self.proposal)
        first = StoryRunner(self.config, client).build(self.idea, self.directory)
        original_project = Path(first["project_path"]).read_bytes()
        second = StoryRunner(self.config, client).build(self.idea, self.directory)
        self.assertEqual(second["project_path"], first["project_path"])
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(Path(second["project_path"]).read_bytes(), original_project)

    def test_repair_keeps_same_frozen_script_and_preserves_failed_candidate(self):
        invalid = copy.deepcopy(self.proposal)
        invalid["shots"][2]["requires"]["prop:key.holder"] = "char:lin"
        client = FakeClient(self.draft, invalid, self.proposal)
        result = StoryRunner(self.config, client).build(self.idea, self.directory, max_repairs=1)
        self.assertTrue(result["ready_for_generation"])
        self.assertEqual(result["compile_attempts"], 2)
        self.assertEqual(len(client.calls), 3)
        failed = read_json(self.directory / "compile-01-candidate.json")
        repaired = read_json(self.directory / "compile-02-candidate.json")
        self.assertEqual(failed["shots"][2]["requires"]["prop:key.holder"], "char:lin")
        self.assertEqual(repaired["shots"][2]["requires"]["prop:key.holder"], "char:chen")
        self.assertEqual(failed["script_source"], repaired["script_source"])
        self.assertEqual(read_json(self.directory / "script.json"), self.script)
        self.assertEqual(client.calls[1][:2], client.calls[2][:2])
        self.assertIn("CONTINUITY", client.calls[2][-1]["content"])
        self.assertFalse(read_json(self.directory / "compile-01-report.json")["ready_for_generation"])
        self.assertTrue(read_json(self.directory / "compile-02-report.json")["ready_for_generation"])

    def test_zero_repair_budget_preserves_failure_and_never_publishes_project(self):
        invalid = copy.deepcopy(self.proposal)
        invalid["shots"].pop()
        client = FakeClient(self.draft, invalid)
        result = StoryRunner(self.config, client).build(self.idea, self.directory, max_repairs=0)
        self.assertEqual(result["stage"], "NEEDS_REVISION")
        self.assertFalse(result["ready_for_generation"])
        self.assertIsNone(result["project_path"])
        self.assertFalse((self.directory / "project.json").exists())
        self.assertEqual(len(client.calls), 2)
        self.assertTrue((self.directory / "compile-01-candidate.json").is_file())
        report = read_json(result["report_path"])
        self.assertIn("UNCOVERED_BEAT", {i["code"] for i in report["issues"]})

    def test_repair_cache_survives_validator_issue_order_changes(self):
        first_bad = copy.deepcopy(self.proposal)
        first_bad["shots"][2]["requires"]["prop:key.holder"] = "char:lin"
        first_bad["shots"][0]["source_refs"][0]["quote"] = "不在来源中的引语"
        second_bad = copy.deepcopy(self.proposal)
        second_bad["shots"].pop()
        second_bad["shots"][0]["source_refs"][0]["quote"] = "仍不在来源中的引语"
        client = FakeClient(first_bad, second_bad, self.proposal)
        first = StoryRunner(self.config, client).compile(self.script, self.directory, max_repairs=1)
        self.assertFalse(first["ready_for_generation"])
        self.assertEqual(len(client.calls), 2)
        self.assertFalse((self.directory / "project.json").exists())
        frozen_before = (self.directory / "script.json").read_bytes()
        first_report = read_json(self.directory / "compile-01-report.json")
        self.assertGreaterEqual(len(first_report["issues"]), 2)

        def reversed_issues(project, script=None):
            report = validate_compilation(project, script)
            report["issues"] = list(reversed(report["issues"]))
            return report

        # A fresh process may traverse sets in a different order. Cached model
        # requests must still match when validation findings are unchanged.
        with patch("pianchang.story_agent.validate_compilation", side_effect=reversed_issues):
            second = StoryRunner(self.config, client).compile(self.script, self.directory, max_repairs=2)
        self.assertTrue(second["ready_for_generation"])
        self.assertEqual(second["compile_attempts"], 3)
        self.assertEqual(second["recorded_model_calls"], 3)
        self.assertEqual(len(client.calls), 3)
        self.assertEqual((self.directory / "script.json").read_bytes(), frozen_before)
        self.assertTrue((self.directory / "compile-01-candidate.json").is_file())
        self.assertTrue((self.directory / "compile-02-candidate.json").is_file())
        self.assertTrue(validate_compilation(read_json(second["project_path"]), self.script)["ready_for_generation"])

    def test_compile_existing_script_needs_only_one_call_and_is_repeatable(self):
        client = FakeClient(self.proposal)
        runner = StoryRunner(self.config, client)
        first = runner.compile(self.script, self.directory)
        second = runner.compile(self.script, self.directory)
        self.assertTrue(first["ready_for_generation"])
        self.assertTrue(second["ready_for_generation"])
        self.assertEqual(len(client.calls), 1)
        self.assertFalse((self.directory / "draft-call.json").exists())

    def test_nonempty_unowned_output_directory_is_not_overwritten(self):
        self.directory.mkdir()
        sentinel = self.directory / "notes.txt"
        sentinel.write_text("已有人工笔记", encoding="utf-8")
        client = FakeClient()
        with self.assertRaises(StoryError):
            StoryRunner(self.config, client).build(self.idea, self.directory)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "已有人工笔记")
        self.assertEqual(client.calls, [])
        self.assertEqual({p.name for p in self.directory.iterdir()}, {"notes.txt"})

    def test_changed_idea_cannot_reuse_previous_output_directory(self):
        client = FakeClient(self.draft)
        runner = StoryRunner(self.config, client)
        runner.build(self.idea, self.directory, draft_only=True)
        frozen_before = (self.directory / "script.json").read_bytes()
        with self.assertRaises(StoryError):
            runner.build("另一部故事：陌生人在车站重逢。", self.directory)
        self.assertEqual((self.directory / "script.json").read_bytes(), frozen_before)
        self.assertEqual(len(client.calls), 1)

    def test_draft_only_can_later_continue_build_without_repeating_draft(self):
        client = FakeClient(self.draft, self.proposal)
        runner = StoryRunner(self.config, client)
        drafted = runner.build(self.idea, self.directory, draft_only=True)
        self.assertEqual(drafted["stage"], "SCRIPT_READY")
        self.assertFalse(drafted["ready_for_generation"])
        self.assertEqual(len(client.calls), 1)
        self.assertFalse((self.directory / "project.json").exists())
        self.assertFalse(list(self.directory.glob("compile-*-call.json")))
        finished = runner.build(self.idea, self.directory)
        self.assertTrue(finished["ready_for_generation"])
        self.assertEqual(len(client.calls), 2)

    def test_edited_frozen_script_is_preserved_and_refused_on_resume(self):
        client = FakeClient(self.draft)
        runner = StoryRunner(self.config, client)
        runner.build(self.idea, self.directory, draft_only=True)
        changed = read_json(self.directory / "script.json")
        changed["outline"] = "人工修改：故事发生了变化。"
        path = self.directory / "script.json"
        path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(StoryError, "冻结剧本"):
            runner.build(self.idea, self.directory)
        self.assertEqual(read_json(path), changed)
        self.assertEqual(len(client.calls), 1)

    def test_failed_or_interrupted_call_is_recorded_and_not_replayed(self):
        for index, failure in enumerate((TextError("temporary network failure"), KeyboardInterrupt())):
            with self.subTest(exception=type(failure).__name__):
                directory = self.directory / str(index)
                client = FakeClient(failure)
                runner = StoryRunner(self.config, client)
                with self.assertRaises(type(failure)):
                    runner.build(self.idea, directory)
                call = read_json(directory / "draft-call.json")
                self.assertEqual(call["status"], "FAILED_OR_INTERRUPTED")
                self.assertFalse((directory / ".story.lock").exists())
                with self.assertRaises(StoryError):
                    StoryRunner(self.config, client).build(self.idea, directory)
                self.assertEqual(len(client.calls), 1)
                self.assertFalse((directory / "project.json").exists())

    def test_interrupted_compile_preserves_draft_without_replaying_either_call(self):
        client = FakeClient(self.draft, TextError("compile connection lost"))
        runner = StoryRunner(self.config, client)
        with self.assertRaises(TextError):
            runner.build(self.idea, self.directory)
        self.assertEqual(read_json(self.directory / "script.json"), self.script)
        self.assertEqual(read_json(self.directory / "draft-call.json")["status"], "COMPLETED")
        self.assertEqual(read_json(self.directory / "compile-01-call.json")["status"], "FAILED_OR_INTERRUPTED")
        with self.assertRaises(StoryError):
            runner.build(self.idea, self.directory)
        self.assertEqual(len(client.calls), 2)

    def test_provider_failure_trace_and_usage_are_preserved_without_credentials(self):
        failure = TextError("provider failed " + self.config.api_key,
                            code="OutputTruncated", request_id="offline-failed-request",
                            usage={"total_tokens": 1024})
        client = FakeClient(failure)
        with self.assertRaises(TextError):
            StoryRunner(self.config, client).build(self.idea, self.directory)
        path = self.directory / "draft-call.json"
        stored = read_json(path)
        self.assertEqual(stored["request_id"], "offline-failed-request")
        self.assertEqual(stored["code"], "OutputTruncated")
        self.assertEqual(stored["usage"]["total_tokens"], 1024)
        self.assertNotIn(self.config.api_key, path.read_text(encoding="utf-8"))

    def test_existing_invalid_project_is_not_overwritten_or_regenerated(self):
        client = FakeClient(self.proposal)
        runner = StoryRunner(self.config, client)
        result = runner.compile(self.script, self.directory)
        project_path = Path(result["project_path"])
        changed = read_json(project_path)
        changed["shots"][2]["requires"]["prop:key.holder"] = "char:lin"
        project_path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(StoryError, "project.json"):
            runner.compile(self.script, self.directory)
        self.assertEqual(read_json(project_path), changed)
        self.assertEqual(len(client.calls), 1)

    def test_human_edit_to_valid_project_survives_repeat_compile(self):
        client = FakeClient(self.proposal)
        runner = StoryRunner(self.config, client)
        result = runner.compile(self.script, self.directory)
        project_path = Path(result["project_path"])
        changed = read_json(project_path)
        changed["title"] = "人工调整的片名"
        project_path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
        again = runner.compile(self.script, self.directory)
        self.assertTrue(again["ready_for_generation"])
        self.assertEqual(read_json(project_path)["title"], "人工调整的片名")
        self.assertEqual(len(client.calls), 1)

    def test_incorrect_draft_shape_never_reaches_compile(self):
        draft = copy.deepcopy(self.draft)
        draft["episodes"][0]["scenes"][0]["beats"].pop()
        client = FakeClient(draft)
        with self.assertRaisesRegex(StoryError, "数量"):
            StoryRunner(self.config, client).build(self.idea, self.directory, beats=3, max_repairs=0)
        self.assertEqual(len(client.calls), 1)
        self.assertTrue((self.directory / "draft-call.json").exists())
        self.assertFalse((self.directory / "project.json").exists())

    def test_auto_structure_freezes_variable_episode_scene_and_beat_lengths(self):
        draft = copy.deepcopy(self.draft)
        draft["episodes"].append({"title": "重逢", "scenes": [
            {"title": "站台", "beats": ["她认出了站台上的旧友。"]},
            {"title": "出口", "beats": ["朋友转身。", "两人再次握手。"]}]})
        client = FakeClient(draft)
        result = StoryRunner(self.config, client).build(self.idea, self.directory, draft_only=True)
        report = read_json(self.directory / "draft-validation.json")
        self.assertEqual(result["draft_repair_count"], 0)
        self.assertEqual(report["requested_constraints"], {})
        actual = report["actual_structure"]["episodes"]
        self.assertEqual([[s["beat_count"] for s in e["scenes"]] for e in actual], [[3], [1, 2]])
        self.assertEqual(json.loads(client.calls[0][1]["content"]), {"mode":"idea", "idea":self.idea})
        self.assertIn("自行决定分集", client.calls[0][0]["content"])
        args = _parser().parse_args(["draft", "--idea", self.idea, "--output", str(self.directory)])
        self.assertEqual((args.episodes, args.scenes_per_episode, args.beats_per_scene), (None, None, None))

    def test_partial_constraint_leaves_other_dimensions_to_agent(self):
        draft = copy.deepcopy(self.draft)
        draft["episodes"][0]["scenes"].append({"title":"出口", "beats":["她关上门。"]})
        client = FakeClient(draft)
        StoryRunner(self.config, client).build(self.idea, self.directory, episodes=1, draft_only=True)
        report = read_json(self.directory / "draft-validation.json")
        self.assertEqual(report["requested_constraints"], {"episodes":1})
        self.assertEqual([c["path"] for c in report["checks"]], ["episodes", "$"])
        self.assertEqual(report["actual_structure"]["scene_count"], 2)

    def test_preexisting_fixed_workspace_preserves_its_original_contract_and_cache(self):
        client = FakeClient(self.draft)
        runner = StoryRunner(self.config, client)
        with patch("pianchang.story_agent.PROMPT_VERSION", PREVIOUS_PROMPT_VERSION):
            runner.build(self.idea, self.directory, episodes=1, scenes=1, beats=3, draft_only=True)
        prior = (self.directory / "draft-00-report.json").read_bytes()
        runner.build(self.idea, self.directory, draft_only=True)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual((self.directory / "draft-00-report.json").read_bytes(), prior)
        with self.assertRaisesRegex(StoryError, "不同创意"):
            runner.build(self.idea, self.directory, beats=2, draft_only=True)

    def test_draft_model_cannot_supply_frozen_ids_idea_or_version(self):
        forged = copy.deepcopy(self.script)
        forged["idea"] = "模型擅自替换的创作目标"
        forged["script_version"] = 999
        forged["episodes"][0]["scenes"][0]["beats"][0]["id"] = "B999"
        client = FakeClient(forged)
        with self.assertRaises(StoryError):
            StoryRunner(self.config, client).build(self.idea, self.directory, draft_only=True, max_repairs=0)
        self.assertEqual(len(client.calls), 1)
        self.assertTrue((self.directory / "draft-call.json").is_file())
        self.assertFalse((self.directory / "script.json").exists())
        self.assertFalse((self.directory / "project.json").exists())

    def test_forged_model_script_binding_cannot_replace_frozen_source(self):
        proposal = copy.deepcopy(self.proposal)
        proposal["script_source"] = {"sha256": "invented", "script": {"idea": "替换原文"}}
        client = FakeClient(proposal)
        result = StoryRunner(self.config, client).compile(self.script, self.directory)
        compiled = read_json(result["project_path"])
        self.assertEqual(compiled["script_source"]["script"], self.script)
        self.assertEqual(compiled["script_source"]["sha256"], digest_json(self.script))

    def test_draft_count_error_is_fed_back_and_repaired_before_compile(self):
        bad = copy.deepcopy(self.draft)
        bad["episodes"][0]["scenes"][0]["beats"].pop()
        client = FakeClient(bad, self.draft, self.proposal)
        progress = []
        result = StoryRunner(self.config, client, progress=progress.append).build(self.idea, self.directory, beats=3)
        self.assertTrue(result["ready_for_generation"])
        self.assertEqual(result["draft_attempts"], 2)
        self.assertEqual(result["draft_repair_count"], 1)
        self.assertEqual(result["recorded_model_calls"], 3)
        feedback = client.calls[1]
        self.assertEqual(json.loads(feedback[-2]["content"]), bad)
        self.assertIn('"expected": 3', feedback[-1]["content"])
        self.assertIn('"actual": 2', feedback[-1]["content"])
        self.assertIn('episodes[0].scenes[0].beats', feedback[-1]["content"])
        self.assertEqual(feedback[:2], client.calls[0][:2])
        self.assertEqual(read_json(self.directory / "draft-00-candidate.json"), bad)
        self.assertEqual(read_json(self.directory / "draft-candidate.json"), self.draft)
        self.assertEqual(read_json(self.directory / "draft-00-report.json")["status"], "FAIL")
        self.assertEqual(read_json(self.directory / "draft-validation.json")["status"], "PASS")
        self.assertTrue((self.directory / "draft-repair-01-call.json").is_file())
        self.assertTrue(any(p["stage"] == "draft-repair-01" for p in progress))

    def test_second_draft_repair_keeps_all_previous_outputs_and_feedback(self):
        bad_count = copy.deepcopy(self.draft)
        bad_count["episodes"][0]["scenes"][0]["beats"].pop()
        bad_shape = copy.deepcopy(self.draft)
        bad_shape["title"] = ""
        client = FakeClient(bad_count, bad_shape, self.draft)
        result = StoryRunner(self.config, client).build(self.idea, self.directory, beats=3, max_repairs=2, draft_only=True)
        self.assertEqual(result["draft_repair_count"], 2)
        self.assertEqual(client.calls[2][:len(client.calls[1])], client.calls[1])
        assistant_outputs = [json.loads(m["content"]) for m in client.calls[2] if m["role"] == "assistant"]
        self.assertEqual(assistant_outputs, [bad_count, bad_shape])
        self.assertIn("DRAFT_SCHEMA", client.calls[2][-1]["content"])
        self.assertIn(self.idea, client.calls[2][1]["content"])
        self.assertEqual(read_json(self.directory / "draft-01-report.json")["status"], "FAIL")
        self.assertEqual(read_json(self.directory / "draft-02-report.json")["status"], "PASS")

    def test_draft_repair_budget_exhaustion_never_freezes_or_compiles(self):
        bad = copy.deepcopy(self.draft)
        bad["episodes"][0]["scenes"][0]["beats"].pop()
        client = FakeClient(bad, bad, bad)
        with self.assertRaisesRegex(StoryError, "已用完 2 次"):
            StoryRunner(self.config, client).build(self.idea, self.directory, beats=3, max_repairs=2)
        self.assertEqual(len(client.calls), 3)
        self.assertFalse((self.directory / "script.json").exists())
        self.assertFalse((self.directory / "project.json").exists())
        self.assertFalse(list(self.directory.glob("compile-*-call.json")))
        report = read_json(self.directory / "draft-validation.json")
        self.assertEqual(report["draft_attempts"], 3)
        self.assertEqual(report["draft_repair_count"], 2)
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(len(list(self.directory.glob("draft-??-candidate.json"))), 3)

    def test_historical_seed_repairs_without_fabricating_or_repeating_initial_call(self):
        seed = copy.deepcopy(self.draft)
        seed["episodes"][0]["scenes"][0]["beats"].pop()
        before = copy.deepcopy(seed)
        source = {"job_id": "old-offline-task", "trace_id": "old-offline-trace", "draft_sha256": digest_json(seed)}
        client = FakeClient(self.draft, self.proposal)
        runner = StoryRunner(self.config, client)
        result = runner.build(self.idea, self.directory, beats=3, initial_draft=seed, repair_source=source)
        self.assertTrue(result["ready_for_generation"])
        self.assertEqual(seed, before)
        self.assertFalse((self.directory / "draft-call.json").exists())
        saved = read_json(self.directory / "draft-seed.json")
        self.assertEqual(saved["kind"], "historical_draft_observation")
        self.assertFalse(saved["network_request_performed"])
        self.assertEqual(saved["source"], source)
        self.assertEqual(result["recorded_model_calls"], 2)
        self.assertEqual(json.loads(client.calls[0][-2]["content"]), seed)
        again = runner.build(self.idea, self.directory, beats=3, initial_draft=seed, repair_source=source)
        self.assertEqual(again["project_path"], result["project_path"])
        self.assertEqual(len(client.calls), 2)
        with self.assertRaisesRegex(StoryError, "哈希"):
            runner.build(self.idea, self.directory, initial_draft=self.draft, repair_source=source)
        self.assertEqual(len(client.calls), 2)

    def test_repaired_draft_calls_are_cached_on_repeat(self):
        bad = copy.deepcopy(self.draft)
        bad["episodes"][0]["scenes"][0]["beats"].pop()
        client = FakeClient(bad, self.draft)
        runner = StoryRunner(self.config, client)
        runner.build(self.idea, self.directory, beats=3, draft_only=True)
        old_initial = (self.directory / "draft-call.json").read_bytes()
        old_candidate = (self.directory / "draft-00-candidate.json").read_bytes()
        runner.build(self.idea, self.directory, beats=3, draft_only=True)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual((self.directory / "draft-call.json").read_bytes(), old_initial)
        self.assertEqual((self.directory / "draft-00-candidate.json").read_bytes(), old_candidate)

    def test_failed_repair_request_stops_and_is_not_replayed(self):
        bad = copy.deepcopy(self.draft)
        bad["episodes"][0]["scenes"][0]["beats"].pop()
        client = FakeClient(bad, TextError("repair response unknown"))
        runner = StoryRunner(self.config, client)
        with self.assertRaises(TextError):
            runner.build(self.idea, self.directory, beats=3, max_repairs=2)
        call = read_json(self.directory / "draft-repair-01-call.json")
        self.assertEqual(call["status"], "FAILED_OR_INTERRUPTED")
        with self.assertRaisesRegex(StoryError, "未完整完成"):
            runner.build(self.idea, self.directory, beats=3, max_repairs=2)
        self.assertEqual(len(client.calls), 2)
        self.assertFalse((self.directory / "draft-repair-02-call.json").exists())

    def test_trace_failure_does_not_trigger_failure_write_or_retry(self):
        client = FakeClient(TraceError("offline trace storage failure"))
        with self.assertRaises(TraceError):
            StoryRunner(self.config, client).build(self.idea, self.directory, max_repairs=2)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(read_json(self.directory / "draft-call.json")["status"], "REQUESTING")
        self.assertFalse((self.directory / "draft-repair-01-call.json").exists())

    def test_trace_failure_before_repair_prevents_another_model_call(self):
        bad = copy.deepcopy(self.draft)
        bad["episodes"][0]["scenes"][0]["beats"].pop()
        client = FakeClient(bad, self.draft)
        from pianchang.story_agent import event as actual_event
        def failing_decision(name, data, **kwargs):
            if name == "agent.decision" and data.get("action") == "repair_draft":
                raise TraceError("offline decision cannot be persisted")
            return actual_event(name, data, **kwargs)
        with patch("pianchang.story_agent.event", side_effect=failing_decision):
            with self.assertRaises(TraceError):
                StoryRunner(self.config, client).build(self.idea, self.directory, beats=3)
        self.assertEqual(len(client.calls), 1)

    def test_prompt_shape_matches_requested_beat_count(self):
        draft = copy.deepcopy(self.draft)
        draft["episodes"][0]["scenes"][0]["beats"].append("陈默将钥匙放入口袋。")
        client = FakeClient(draft)
        StoryRunner(self.config, client).build(self.idea, self.directory, episodes=1, scenes=1, beats=4, draft_only=True)
        system = client.calls[0][0]["content"]
        example = json.loads(system.split("严格采用此结构：\n", 1)[1].split("\nsetup 每条", 1)[0])
        self.assertEqual(len(example["episodes"][0]["scenes"][0]["beats"]), 4)
        self.assertNotIn("__DRAFT_SHAPE__", system)

    def test_existing_lock_prevents_a_second_worker_without_removing_lock(self):
        self.directory.mkdir()
        lock = self.directory / ".story.lock"
        lock.write_text("99999", encoding="utf-8")
        client = FakeClient()
        with self.assertRaises(StoryError):
            StoryRunner(self.config, client).build(self.idea, self.directory)
        self.assertEqual(lock.read_text(encoding="utf-8"), "99999")
        self.assertEqual(client.calls, [])


class StoryContextTests(unittest.TestCase):
    def setUp(self):
        self.project = read_json(ROOT / "fixtures" / "story_project.json")

    def test_context_uses_cumulative_state_and_keeps_quality_unknown(self):
        context = query_context(self.project, "S002")
        self.assertEqual(context["state_before"]["prop:key.holder"], "char:lin")
        self.assertEqual(context["state_after"]["prop:key.holder"], "char:chen")
        self.assertFalse(context["state_after"]["char:chen.knows_key_origin"])
        self.assertEqual(context["events"][0]["id"], "event:handover")
        self.assertIn("B002", context["sources"])
        self.assertEqual(context["script_sha256"], self.project["script_source"]["sha256"])
        for field in ("semantic_fidelity_pass", "visual_pass", "audio_pass"):
            self.assertIsNone(context[field])

    def test_single_shot_context_omits_unrelated_character_facts_and_beats(self):
        context = query_context(self.project, "S003")
        self.assertNotIn("char:lin", context["entities"])
        self.assertNotIn("costume:coat", context["entities"])
        self.assertNotIn("char:lin.costume", context["facts"])
        self.assertNotIn("char:lin.location", context["state_before"])
        self.assertIn("char:chen", context["entities"])
        self.assertEqual(context["state_before"]["prop:key.holder"], "char:chen")
        self.assertEqual(context["events"], [])
        self.assertIn("B003", context["sources"])
        self.assertNotIn("B001", context["sources"])
        self.assertIn("B002", context["sources"])
        self.assertNotIn("SET001", context["sources"])
        self.assertNotIn("SET005", context["sources"])
        origin = context["state_before_origins"]["prop:key.holder"]
        self.assertEqual(origin["kind"], "event")
        self.assertEqual(origin["event_id"], "event:handover")
        self.assertEqual(origin["shot_id"], "S002")
        self.assertEqual(origin["source_refs"], self.project["events"][0]["source_refs"])
        self.assertEqual(origin["reason"], self.project["events"][0]["reason"])

    def test_context_refuses_unknown_shot(self):
        with self.assertRaises(StoryError):
            query_context(self.project, "S404")

    def test_context_refuses_invalid_provenance(self):
        self.project["shots"][0]["source_refs"][0]["quote"] = "不存在的原文引语"
        with self.assertRaises(StoryError):
            query_context(self.project, "S001")

    def test_video_shot_keeps_script_and_episode_provenance(self):
        compiled = compile_shot(self.project, "S002")
        metadata = compiled["metadata"]
        self.assertEqual(metadata["script_sha256"], self.project["script_source"]["sha256"])
        self.assertEqual(metadata["episode_id"], "EP001")
        self.assertEqual(metadata["script_scene_id"], "SC001")
        self.assertEqual(metadata["source_refs"], self.project["shots"][1]["source_refs"])
        self.assertIsNone(metadata["semantic_fidelity_pass"])

    def test_video_gate_rejects_forged_quote_even_with_prompt_override(self):
        self.project["shots"][0]["source_refs"][0]["quote"] = "不存在的原文引语"
        with self.assertRaisesRegex(VideoError, "SOURCE_QUOTE"):
            compile_shot(self.project, "S001", prompt_override="自定义镜头提示词")

    def test_video_gate_rejects_omitted_required_beat(self):
        self.project["shots"].pop()
        with self.assertRaisesRegex(VideoError, "UNCOVERED_BEAT"):
            compile_shot(self.project, "S001")


if __name__ == "__main__":
    unittest.main()
