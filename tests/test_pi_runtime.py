"""Explicit offline Pi SDK integration; never calls a billable API."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pianchang.agent_loop import Tool
from pianchang.bailian_text import TextConfig
from pi_bridge.runtime import PiLoop,make_loop
from trace_system import get_tracer, use_context


class PiRuntimeTests(unittest.TestCase):
    def test_only_explicit_legacy_fixture_selects_old_loop(self):
        from pianchang.agent_loop import AgentLoop
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);tracer=get_tracer(root/"traces")
            with use_context(None,tracer=tracer),tracer.trace("test.pi.backend",inputs={"label":"offline"}):
                options={"snapshot":{},"finish_check":lambda:{"complete":False}}
                tools=[Tool("read_fixture","offline fixture",{"type":"object","properties":{}},lambda:{"offline":True})]
                self.assertIsInstance(make_loop(SimpleNamespace(agent_backend="offline_legacy"),tools,root/"legacy",**options),AgentLoop)
                self.assertIsInstance(make_loop(SimpleNamespace(config=TextConfig()),tools,root/"native",**options),PiLoop)

    def test_gate_rejects_model_claim_and_generation_outside_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);tracer=get_tracer(root/"traces");calls=[]
            with use_context(None,tracer=tracer),tracer.trace("test.pi.guards",inputs={"label":"offline"}):
                client=SimpleNamespace(config=TextConfig(api_key="offline-no-network"))
                tool=Tool("generate_shot","offline",{"type":"object","properties":{"shot_id":{"type":"string"}},"required":["shot_id"]},
                    lambda **args:calls.append(args),effect="video")
                loop=PiLoop(client,[tool],root/"run",snapshot={},finish_check=lambda:{"complete":False,"media_accepted":False},
                    stop_check=lambda record:{"allowed":False,"actionable":["S001"]})
                loop.record={"limits":{"max_video_generations":0},"video_generations":0,"messages":[]}
                result=loop.execute("finish_task",{"outcome":"completed","summary":"模型自称通过"},"offline-1")
                self.assertTrue(result["finish_rejected"]);self.assertIsNone(loop.outcome)
                replied=loop.execute("finish_task",{"outcome":"responded","summary":"尝试用对话结束绕过制作"},"offline-response")
                self.assertTrue(replied["finish_rejected"]);self.assertIsNone(loop.outcome)
                with self.assertRaises(ValueError):loop.execute("generate_shot",{"shot_id":"S001"},"offline-2")
                self.assertEqual(calls,[])
                loop.stop_check=None
                response=loop.execute("finish_task",{"outcome":"responded","summary":"只读回答完成"},"offline-response-2")
                self.assertTrue(response["turn_complete"]);self.assertFalse(response["task_complete"]);self.assertFalse(response["media_accepted"])

    def test_finish_tool_ends_native_pi_without_an_extra_paid_turn(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);tracer=get_tracer(root/"traces");requests=[]
            def exchange(payload):
                requests.append(payload)
                return {"id":"offline-finish","model":"offline-model","created":0,"choices":[{"index":0,
                    "message":{"role":"assistant","content":None,"tool_calls":[{"id":"offline-finish-call","type":"function",
                    "function":{"name":"finish_task","arguments":json.dumps({"outcome":"completed","summary":"离线完成"})}}]},
                    "finish_reason":"tool_calls"}],"usage":{"prompt_tokens":10,"completion_tokens":10,"total_tokens":20}}
            client=SimpleNamespace(config=TextConfig(api_key="offline-no-network",model="offline-model"),pi_exchange=exchange)
            with use_context(None,tracer=tracer),tracer.trace("test.pi.finish",inputs={"label":"offline"}):
                result=PiLoop(client,[],root/"run",snapshot={},finish_check=lambda:{"complete":True,"summary":"offline validated"}).run("离线完成检查",max_steps=3)
                self.assertTrue(result["task_complete"],result);self.assertEqual(len(requests),1)

    def test_native_compaction_after_finish_preserves_business_outcome(self):
        for fail_compaction in (False,True):
            with self.subTest(fail_compaction=fail_compaction),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);tracer=get_tracer(root/"traces");requests=[];events=[]
                def exchange(payload):
                    requests.append(payload)
                    if not payload.get("tools"):
                        if fail_compaction:raise ValueError("explicit offline compaction failure")
                        message={"role":"assistant","content":"Offline checkpoint: original constraints and failed checks retained."}
                    elif len(requests)==1:
                        message={"role":"assistant","content":None,"tool_calls":[{"id":"offline-read","type":"function",
                            "function":{"name":"read_long_evidence","arguments":"{}"}}]}
                    else:
                        message={"role":"assistant","content":None,"tool_calls":[{"id":"offline-finish","type":"function",
                            "function":{"name":"finish_task","arguments":json.dumps({"outcome":"responded","summary":"offline response only"})}}]}
                    usage=120000 if payload.get("tools") and len(requests)==2 else 20
                    return {"id":"offline-compaction","model":"offline-model","created":0,
                        "choices":[{"index":0,"message":message,"finish_reason":"tool_calls" if message.get("tool_calls") else "stop"}],
                        "usage":{"prompt_tokens":usage,"completion_tokens":10,"total_tokens":usage+10}}
                client=SimpleNamespace(config=TextConfig(api_key="offline-no-network",model="offline-model"),pi_exchange=exchange)
                tool=Tool("read_long_evidence","explicit offline large history",{"type":"object","properties":{}},
                    lambda:{"offline_evidence":"offline original constraint; failed gate retained. "*1800})
                with use_context(None,tracer=tracer),tracer.trace("test.pi.compaction",inputs={"label":"explicit offline mock","failure":fail_compaction}):
                    loop=PiLoop(client,[tool],root/"run",snapshot={},finish_check=lambda:{"complete":False},on_event=events.append)
                    result=loop.run("Read offline evidence, then answer without accepting media",max_steps=6)
                    self.assertEqual(result["status"],"responded",result)
                    self.assertFalse(result["task_complete"])
                    self.assertTrue(any(e["type"]=="compaction_start" for e in events),events)
                    self.assertGreaterEqual(len(requests),3)
                    self.assertTrue(all(not p.get("tools") for p in requests[2:]),"No new decision after accepted finish")
                    if fail_compaction:self.assertEqual(result["context_maintenance"]["status"],"incomplete")
                    else:
                        self.assertIsNone(result["context_maintenance"])
                        entries=[json.loads(s) for s in Path(result["session_file"]).read_text("utf-8").splitlines()]
                        self.assertTrue(any(e["type"]=="compaction" for e in entries))

    def test_native_tool_loop_and_persistent_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);tracer=get_tracer(root/"traces");requests=[];calls=[]
            role_prompt = "Offline fixture: you are the script Agent for this story."
            developer_note = "Offline fixture: repository maintainer instructions."
            for run_dir in ("first", "second"):
                agent_dir = root / run_dir / "pi-config"
                agent_dir.mkdir(parents=True)
                (agent_dir / "AGENTS.md").write_text(developer_note, encoding="utf-8")
            def exchange(payload):
                requests.append(payload)
                has_result=any(m.get("role")=="tool" for m in payload["messages"])
                message=({"role":"assistant","content":"离线验证完成"} if has_result else
                    {"role":"assistant","content":None,"tool_calls":[{"id":"offline-call-1","type":"function","function":{"name":"read_evidence","arguments":"{}"}}]})
                return {"id":"offline-pi-provider", "model":"offline-model","created":0,
                    "choices":[{"index":0,"message":message,"finish_reason":"stop" if has_result else "tool_calls"}],
                    "usage":{"prompt_tokens":30,"completion_tokens":10,"total_tokens":40}}
            client=SimpleNamespace(config=TextConfig(api_key="offline-no-network",model="offline-model"),pi_exchange=exchange)
            tool=Tool("read_evidence","读取离线证据",{"type":"object","properties":{}},lambda:calls.append(True) or {"actual":"offline evidence"})
            with use_context(None,tracer=tracer),tracer.trace("test.pi.offline",inputs={"label":"explicit offline mock"}):
                first=PiLoop(client,[tool],root/"first",system_prompt=role_prompt,snapshot=lambda:{"offline":True},initial_message="[message_id="+"a"*32+"] offline approved plan",
                    finish_check=lambda:{"complete":bool(calls),"summary":"offline fixture passed"}).run("读取离线证据",max_steps=4)
                self.assertEqual(first["backend"],"pi")
                self.assertEqual(first["status"],"completed",first)
                self.assertEqual(len(calls),1)
                self.assertTrue(Path(first["session_file"]).is_file())
                self.assertIn("[message_id="+"a"*32+"]",Path(first["session_file"]).read_text("utf-8"))
                prior_count=len(requests)
                second=PiLoop(client,[tool],root/"second",system_prompt=role_prompt,session_file=first["session_file"],snapshot=lambda:{"offline":True},
                    finish_check=lambda:{"complete":True,"summary":"offline follow-up"}).run("结合刚才的证据继续回答",max_steps=4)
                self.assertEqual(second["session_id"],first["session_id"])
                self.assertGreater(len(requests[prior_count]["messages"]),len(requests[0]["messages"]))
                self.assertEqual(len(calls),1,"A follow-up must not replay the old tool")
                for payload in (requests[0], requests[prior_count]):
                    instructions = next(m["content"] for m in payload["messages"] if m["role"] == "system")
                    self.assertIn(role_prompt, instructions)
                    self.assertNotIn(developer_note, instructions)
                    self.assertNotIn("<project_instructions", instructions)

if __name__=="__main__":unittest.main()
