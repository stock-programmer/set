"""Offline screenplay decision/feedback tests with an explicitly simulated model."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from pianchang.collab_store import CollabStore
from pianchang.script_collab_agent import publish_script_revision, run_script_agent
from pianchang.story_spec import digest_json
from trace_system import Tracer, use_context


def action(tool, **arguments):
    return {"type": "tool", "tool": tool, "arguments": arguments, "summary": "offline test tool"}


FINISH = {"type": "finish", "outcome": "completed", "summary": "offline completion request"}


class OfflineModel:
    agent_backend="offline_legacy"
    def __init__(self, decisions):
        self.decisions = iter(decisions)
        self.requests = []

    def complete_json(self, messages):
        self.requests.append(copy.deepcopy(messages))
        return {"model": "offline-script-test", "data": next(self.decisions)}


class ScriptAgentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.tracer = Tracer(self.directory / "traces")
        self.binding = use_context(None, tracer=self.tracer)
        self.binding.__enter__()
        self.addCleanup(self.binding.__exit__, None, None, None)
        self.original = json.loads((Path(__file__).resolve().parents[1] / "fixtures/story_script.json").read_text(encoding="utf8"))
        self.published = []

    def run_case(self, decisions, **params):
        def publisher(script, report):
            self.published.append(copy.deepcopy(script))
            return {"revision": script["script_version"], "sha256": digest_json(script)}

        self.model = OfflineModel(decisions)
        return run_script_agent(self.model, self.directory / "run", story_id="offline-story",
                                goal="offline script intent", publish=params.pop("publish", publisher), **params)

    def feedback(self, index):
        values = [json.loads(m["content"]) for m in self.model.requests[index]
                  if m["role"] == "user" and m["content"].startswith("{")]
        return next(value for value in reversed(values) if value.get("type") == "tool_result")

    def test_import_rejects_early_publish_and_finish_then_preserves_exact_original(self):
        with self.tracer.trace("offline.script.parent", kind="test") as parent:
            result = self.run_case([FINISH, action("publish_script"), action("read_script"),
                                    action("check_script"), action("publish_script"), FINISH],
                                   mode="import", script=self.original)
        self.assertFalse(self.feedback(1)["ok"])
        self.assertFalse(self.feedback(2)["ok"])
        self.assertTrue(result["loop_result"]["task_complete"])
        self.assertEqual(self.published, [self.original])
        self.assertEqual(result["receipt"]["sha256"], digest_json(self.original))
        self.assertNotEqual(result["trace_id"], parent.trace_id)
        detail = self.tracer.get_trace(result["trace_id"])
        root = next(span for span in detail["spans"] if not span["parent_span_id"])
        self.assertEqual(root["links"][0]["trace_id"], parent.trace_id)
        self.assertTrue(self.tracer.verify(result["trace_id"])["ok"])

    def test_revise_only_requested_stable_beat_and_keep_unrelated_source(self):
        original_hash = digest_json(self.original)
        new_text = "陈默低头看向铜钥匙，停顿一秒，再扬起手。"
        result = self.run_case([action("read_script"), action("replace_beat", beat_id="B002", text="改错节拍"),
                                action("replace_beat", beat_id="B003", text=new_text), action("check_script"),
                                action("publish_script"), FINISH], mode="revise", script=self.original,
                               base_revision="r1", beat_id="B003", constraints={"required_terms": ["停顿一秒"]})
        self.assertFalse(self.feedback(2)["ok"])
        self.assertEqual(digest_json(self.original), original_hash)
        expected = copy.deepcopy(self.original)
        expected["script_version"] += 1
        expected["episodes"][0]["scenes"][0]["beats"][2]["text"] = new_text
        self.assertEqual(self.published, [expected])
        self.assertTrue(result["loop_result"]["task_complete"])

    def test_agent_authors_unequal_episode_scene_and_beat_lengths_from_idea(self):
        draft = {"title": "钥匙", "logline": "断线的老友重新联系。", "outline": "陌生人出现，两次试探后相认。",
                 "setup": ["十年前，老友留下了一把钥匙。"], "episodes": [
                     {"title": "来客", "scenes": [{"title": "门口", "beats": ["风铃响起。"]}]},
                     {"title": "相认", "scenes": [
                         {"title": "柜台", "beats": ["陌生人放下钥匙。", "店员认出了刻痕。"]},
                         {"title": "街角", "beats": ["店员追了出去。", "老友回头。", "两人叫出了对方的名字。"]}]}]}
        result = self.run_case([action("write_draft", **draft), action("check_script"),
                                action("publish_script"), FINISH], mode="create")
        self.assertTrue(result["loop_result"]["task_complete"])
        report = self.feedback(2)["result"]
        self.assertEqual(report["requested_constraints"], {})
        self.assertEqual([check["path"] for check in report["checks"]], ["script.schema"])
        structure = report["actual_structure"]
        self.assertEqual([structure[key] for key in ("episode_count", "scene_count", "beat_count")], [2, 3, 6])
        sizes = [[scene["beat_count"] for scene in episode["scenes"]] for episode in structure["episodes"]]
        self.assertEqual(sizes, [[1], [2, 3]])
        self.assertEqual(result["validation"], report)
        stored = json.loads((self.directory / "run" / "script-validation.json").read_text(encoding="utf8"))
        self.assertEqual(stored, report)

    def test_requested_beat_count_failure_returns_to_next_model_decision(self):
        draft = {"title": "午夜", "logline": "店员见到老友", "outline": "看见，认出，交谈",
                 "setup": ["深夜便利店只有二人。"], "episodes": [{"title": "相遇",
                 "scenes": [{"title": "收银台前", "beats": ["看见老友", "认出对方"]}]}]}
        fixed = copy.deepcopy(draft)
        fixed["episodes"][0]["scenes"][0]["beats"].append("互道问候")
        result = self.run_case([action("write_draft", **draft), action("check_script"),
                                action("write_draft", **fixed), action("check_script"), action("publish_script"),
                                FINISH], mode="create", constraints={"beats_per_scene": 3})
        report = self.feedback(2)["result"]
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["requested_constraints"], {"beats_per_scene": 3})
        self.assertEqual(report["actual_structure"]["beat_count"], 2)
        self.assertIn({"path": "SC001.beats", "expected": 3, "actual": 2, "status": "FAIL"}, report["checks"])
        self.assertEqual(len(self.published), 1)
        self.assertTrue(result["loop_result"]["task_complete"])

    def test_uncertain_publication_is_not_replayed_inside_loop(self):
        attempts = []
        def uncertain(script, report):
            attempts.append(report["script_sha256"])
            raise OSError("offline simulated unknown commit")
        result = self.run_case([action("check_script"), action("publish_script"), action("publish_script"),
                                {"type": "finish", "outcome": "needs_input", "summary": "offline unknown receipt"}],
                               mode="import", script=self.original, publish=uncertain)
        self.assertFalse(result["loop_result"]["task_complete"])
        self.assertEqual(len(attempts), 1)
        self.assertIsNone(result["receipt"])

    def test_store_publisher_links_checked_revision_to_actual_changed_ids(self):
        store = CollabStore(self.directory / "assets.sqlite")
        params = {"asset_id": "script:offline", "story_id": "offline", "correlation_id": "offline-batch"}
        def publish(script, base_revision=None):
            report = {"status": "PASS", "script_sha256": digest_json(script), "base_revision": base_revision}
            return publish_script_revision(store, script, report, **params, base_revision=base_revision,
                                           source_sha256=digest_json(self.original))
        first = publish(self.original)
        changed = copy.deepcopy(self.original)
        changed["script_version"] += 1
        changed["episodes"][0]["scenes"][0]["beats"][2]["text"] += " 他停顿一秒。"
        second = publish(changed, "r1")
        self.assertEqual(second["changed_source_ids"], ["B003"])
        self.assertEqual(second["revision_id"], "r2")
        self.assertEqual(store.get_asset(first["asset_id"], "r1")["content"], self.original)
