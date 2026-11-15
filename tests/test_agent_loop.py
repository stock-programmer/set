"""Offline behavior tests for model-driven tool feedback; no paid/network calls."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pianchang.agent_loop import AgentLoop, AgentLoopError, Tool, _replace_record
from trace_system import TraceError, Tracer, use_context


EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}


def action(name, **arguments):
    return {"type": "tool", "tool": name, "arguments": arguments, "summary": "offline test action"}


def finish(outcome="completed", summary="offline model completion claim"):
    return {"type": "finish", "outcome": outcome, "summary": summary}


class ScriptedModel:
    agent_backend="offline_legacy"
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.calls = []

    def complete_json(self, messages):
        self.calls.append(copy.deepcopy(messages))
        if not self.decisions:
            raise AssertionError("Unexpected extra model call")
        decision = self.decisions.pop(0)
        if callable(decision):
            decision = decision(messages)
        return {"data": decision, "model": "offline-scripted-test", "usage": {"test_only": True}}


class AgentLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tracer = Tracer(self.root / "traces")
        self.binding = use_context(None, tracer=self.tracer)
        self.binding.__enter__()
        self.addCleanup(self.binding.__exit__, None, None, None)

    def loop(self, decisions, tools, *, snapshot=None, finish_check=None, **kwargs):
        model = ScriptedModel(decisions)
        self.model = model
        return AgentLoop(model, tools, self.root / "run", snapshot=snapshot or {"offline": True},
                         finish_check=finish_check or (lambda: {"complete": True, "media_accepted": False}), **kwargs)

    def feedback(self, messages):
        return [json.loads(m["content"]) for m in messages if m["role"] == "user"
                and json.loads(m["content"]).get("type") == "tool_result"]

    def test_transient_windows_record_lock_retries_only_local_replace(self):
        from pianchang import agent_loop
        temporary, target = self.root / "offline.tmp", self.root / "offline.json"
        temporary.write_text('"offline-new"', encoding="utf8")
        target.write_text('"offline-old"', encoding="utf8")
        real_replace = agent_loop._replace_file
        denied = PermissionError("offline Windows sharing conflict"); denied.winerror = 5
        calls = []
        def replacing(source, destination):
            calls.append((source, destination))
            if len(calls) == 1:
                raise denied
            return real_replace(source, destination)
        with self.tracer.trace("offline.record.lock"), \
                patch("pianchang.agent_loop._replace_file", side_effect=replacing), \
                patch("pianchang.agent_loop._record_retry_sleep") as sleep:
            result = _replace_record(temporary, target)
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(target.read_text(encoding="utf8"), '"offline-new"')
        self.assertFalse(temporary.exists())
        sleep.assert_called_once_with(.05)

    def test_persistent_record_denial_preserves_snapshot_and_stops_before_model(self):
        loop = self.loop([finish()], [Tool("inspect", "offline", EMPTY, lambda: {})])
        denied = PermissionError("offline permanent denial"); denied.winerror = 5
        with patch("pianchang.agent_loop._replace_file", side_effect=denied) as replace, \
                patch("pianchang.agent_loop._record_retry_sleep"), self.assertRaises(AgentLoopError):
            loop.run("offline save failure must stop", max_steps=1)
        self.assertEqual(replace.call_count, 7)
        self.assertFalse(self.model.calls)
        self.assertTrue(list((self.root / "run").glob(".run-*.tmp")))

    def test_real_tool_result_enters_next_model_context_and_parent_trace(self):
        loop = self.loop([action("read_project"), finish()],
                        [Tool("read_project", "read", EMPTY, lambda: {"facts": {"key_holder": "Chen"}})])
        with self.tracer.trace("offline.agent.task", kind="task") as parent:
            result = loop.run("inspect project", max_steps=3)
        self.assertTrue(result["task_complete"])
        feedback = self.feedback(self.model.calls[1])
        self.assertEqual(feedback[0]["result"]["facts"]["key_holder"], "Chen")
        self.assertEqual(feedback[0]["call_id"], "step-001")
        trace = self.tracer.get_trace(parent.trace_id)
        core = next(s for s in trace["spans"] if s["name"] == "agent.loop.run")
        child = next(s for s in trace["spans"] if s["name"] == "agent.tool.read_project")
        self.assertEqual(core["parent_span_id"], parent.span_id)
        self.assertEqual(child["parent_span_id"], core["span_id"])
        self.assertTrue(self.tracer.verify(parent.trace_id)["ok"])

    def test_model_can_choose_tool_order_instead_of_fixed_pipeline(self):
        seen = []
        tools = [Tool(name, name, EMPTY, lambda name=name: seen.append(name) or {"tool": name}) for name in ("inspect", "edit")]
        result = self.loop([action("edit"), action("inspect"), action("edit"), finish()], tools).run("fix", max_steps=4)
        self.assertEqual(seen, ["edit", "inspect", "edit"])
        self.assertEqual(len(self.feedback(self.model.calls[3])), 3)
        self.assertEqual(result["status"], "completed")

    def test_startup_preparation_is_claimed_and_not_replayed(self):
        state={"prepared":False};calls=[]
        def prepare():
            self.assertTrue((self.root/"run/.claimed").is_file())
            self.assertTrue((self.root/"run/run.json").is_file())
            state["prepared"]=True;calls.append(1)
            return {"mode":"explicit_offline_startup_tool","restored_shots":["S001"]}
        loop=self.loop([finish()],[Tool("read_project","offline",EMPTY,lambda:{})],
                       snapshot=lambda:state,prepare=prepare)
        result=loop.run("offline prepared repair state",max_steps=2)
        self.assertTrue(result["task_complete"])
        messages=[json.loads(m["content"]) for m in self.model.calls[0] if m["role"]=="user"]
        self.assertTrue(next(m for m in messages if m["type"]=="state_snapshot")["state"]["prepared"])
        self.assertEqual(next(m for m in messages if m["type"]=="preparation_result")["source"],"executed_startup_tools")
        with self.assertRaises(AgentLoopError):loop.run("offline must not replay startup",max_steps=2)
        self.assertEqual(calls,[1])

    def test_shooting_plan_limits_specific_shots_without_losing_an_authorized_attempt(self):
        shots = [f"S{i:03d}" for i in range(1, 14)]
        plan = {"new_shots": shots, "retake_shots": [], "reuse_shots": ["S099"]}
        attempts = []
        schema = {"type": "object", "properties": {"shot_id": {"type": "string"}}, "required": ["shot_id"]}
        def generate(shot_id):
            attempts.append(shot_id)
            return {"shot_id": shot_id, "offline_details": "x" * 18000}
        tool = Tool("generate_shot", "offline generation", schema, generate, "video",
                    context_result=lambda result: {"shot_id": result["shot_id"]})
        ids = [shots[0], shots[0], "S099", *shots[1:]]
        loop = self.loop([*[action("generate_shot", shot_id=s) for s in ids], finish()], [tool])
        result = loop.run("offline scoped production", max_steps=32, max_video_generations=len(shots), shooting_plan=plan)
        self.assertTrue(result["task_complete"], result)
        self.assertEqual(attempts, shots)
        self.assertEqual(result["video_generations"], len(shots))
        errors = [m["error"]["code"] for m in self.feedback(self.model.calls[-1]) if not m["ok"]]
        self.assertEqual(errors, ["SHOT_OUTSIDE_PLAN", "SHOT_OUTSIDE_PLAN"])

    def test_completion_claim_rejected_until_authoritative_check_passes(self):
        state = {"fixed": False}
        def fix():
            state["fixed"] = True
            return {"fixed": True}
        checker = lambda: {"complete": state["fixed"], "summary": "检查执行完成；媒体仍UNKNOWN", "media_accepted": False}
        result = self.loop([finish(), action("repair"), finish(summary="all videos perfect")],
                           [Tool("repair", "repair", EMPTY, fix, "write")], finish_check=checker).run("inspect", max_steps=3)
        self.assertEqual(self.feedback(self.model.calls[1])[0]["error"]["code"], "FINISH_REJECTED")
        self.assertEqual(result["summary"], "检查执行完成；媒体仍UNKNOWN")
        self.assertFalse(result["completion_check"]["media_accepted"])

    def test_truthy_nonboolean_complete_cannot_bypass_finish_gate(self):
        result = self.loop([finish()], [Tool("read", "read", EMPTY, lambda: {})],
                           finish_check=lambda: {"complete": "true"}).run("inspect", max_steps=1)
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertFalse(result["task_complete"])

    def test_video_budget_refusal_feedback_allows_read_instead(self):
        video = []
        tools = [Tool("generate_video", "video", EMPTY, lambda: video.append(1), "video"),
                 Tool("read", "read", EMPTY, lambda: {"existing_candidate": True})]
        result = self.loop([action("generate_video"), action("read"), finish("needs_input")], tools).run("prepare", max_steps=3)
        self.assertEqual(video, [])
        self.assertEqual(self.feedback(self.model.calls[1])[0]["error"]["code"], "VIDEO_BUDGET_EXHAUSTED")
        self.assertEqual(result["video_generations"], 0)
        self.assertFalse(result["task_complete"])

    def test_video_budget_reserved_durably_before_executing(self):
        called = []
        def video():
            saved = json.loads((self.root / "run" / "run.json").read_text(encoding="utf-8"))
            called.append(saved["video_generations"])
            return {"task_id": "offline-task", "status": "SUCCEEDED"}
        result = self.loop([action("generate_video"), action("generate_video"), finish("needs_input")],
                           [Tool("generate_video", "video", EMPTY, video, "video")]).run("generate", max_steps=3, max_video_generations=1)
        self.assertEqual(called, [1])
        self.assertEqual(result["video_generations"], 1)

    def test_uncertain_video_exception_stops_without_retry(self):
        called = []
        def video():
            called.append(1)
            raise TimeoutError("provider may already have submitted")
        result = self.loop([action("generate_video"), action("generate_video")],
                           [Tool("generate_video", "video", EMPTY, video, "video")]).run("generate", max_steps=4, max_video_generations=2)
        self.assertEqual(called, [1])
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(result["reason"], "video_result_uncertain")
        self.assertFalse(result["task_complete"])

    def test_invalid_tool_and_arguments_do_not_execute(self):
        calls = []
        schema = {"type": "object", "properties": {"count": {"type": "integer", "minimum": 1, "maximum": 3}}, "required": ["count"], "additionalProperties": False}
        decisions = [action("shell", command="anything"), action("read", count=True), action("read", count=2, extra=3), action("read", count=2), finish()]
        result = self.loop(decisions, [Tool("read", "read", schema, lambda **kw: calls.append(kw) or kw)]).run("inspect", max_steps=5)
        self.assertEqual(calls, [{"count": 2}])
        self.assertTrue(result["task_complete"])
        self.assertEqual(len([f for f in self.feedback(self.model.calls[-1]) if f["error"]]), 3)

    def test_invalid_decision_returns_evidence_and_consumes_step(self):
        result = self.loop([{"type": "tool", "tool": "read", "arguments": {}, "summary": "ok", "extra": "bad"}, finish("needs_input")],
                           [Tool("read", "read", EMPTY, lambda: self.fail("must not execute"))]).run("inspect", max_steps=2)
        self.assertEqual(self.feedback(self.model.calls[1])[0]["error"]["code"], "INVALID_DECISION")
        self.assertEqual(result["steps"], 2)

    def test_snapshot_changes_are_returned_without_losing_prior_results(self):
        state = {"version": 1}
        def edit():
            state["version"] = 2
            return {"new_version": 2}
        self.loop([action("edit"), finish()], [Tool("edit", "edit", EMPTY, edit, "write")], snapshot=lambda: state.copy()).run("edit", max_steps=2)
        observations = [json.loads(m["content"]) for m in self.model.calls[1] if m["role"] == "user"]
        self.assertEqual([m["state"]["version"] for m in observations if m.get("type") == "state_snapshot"], [1, 2])
        self.assertEqual(self.feedback(self.model.calls[1])[0]["result"], {"new_version": 2})

    def test_context_limit_stops_without_silent_truncation_or_model_call(self):
        loop = self.loop([finish()], [Tool("read", "read", EMPTY, lambda: {})])
        with patch("pianchang.agent_loop.MAX_CONTEXT_CHARS", 100):
            result = loop.run("inspect")
        self.assertEqual(self.model.calls, [])
        self.assertEqual(result["reason"], "context_limit")
        saved = json.loads((self.root / "run" / "run.json").read_text(encoding="utf-8"))
        self.assertGreater(len(json.dumps(saved["messages"])), 100)

    def test_step_limit_does_not_issue_an_extra_model_call(self):
        result = self.loop([action("read"), finish()], [Tool("read", "read", EMPTY, lambda: {})]).run("inspect", max_steps=1)
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(result["status"], "budget_exhausted")

    def test_cancel_after_model_prevents_proposed_tool(self):
        cancel = {"value": False}
        def decision(_):
            cancel["value"] = True
            return action("read")
        result = self.loop([decision], [Tool("read", "read", EMPTY, lambda: self.fail("cancelled"))], cancelled=lambda: cancel["value"]).run("inspect")
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(result["task_complete"])

    def test_existing_directory_cannot_replay_calls(self):
        loop = self.loop([finish()], [Tool("read", "read", EMPTY, lambda: {})])
        loop.run("inspect")
        before = (self.root / "run" / "events.jsonl").read_bytes()
        with self.assertRaisesRegex(AgentLoopError, "不得自动重放"):
            loop.run("inspect")
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(before, (self.root / "run" / "events.jsonl").read_bytes())

    def test_secret_redacted_in_journal_context_and_error_feedback(self):
        secret = "offline-private-credential-123456"
        self.tracer.register_secret(secret)
        def fail():
            raise ValueError("tool failed " + secret)
        result = self.loop([action("read"), finish("needs_input", secret)], [Tool("read", "read", EMPTY, fail)]).run("inspect " + secret)
        for name in ("run.json", "events.jsonl"):
            self.assertNotIn(secret, (self.root / "run" / name).read_text(encoding="utf-8"))
        self.assertNotIn(secret, json.dumps(self.model.calls))
        self.assertNotIn(secret, json.dumps(result))

    def test_trace_failure_prevents_tool_side_effect(self):
        calls = []
        loop = self.loop([action("edit")], [Tool("edit", "edit", EMPTY, lambda: calls.append(1), "write")])
        from pianchang.agent_loop import event as real_event
        def event_failure(name, *args, **kwargs):
            if name == "agent.loop.tool_requested":
                raise TraceError("offline disk failure")
            return real_event(name, *args, **kwargs)
        with patch("pianchang.agent_loop.event", side_effect=event_failure):
            with self.assertRaises(TraceError):
                loop.run("edit")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
