import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from pi_bridge.creative_input import validate_reservation
from pi_bridge.runtime import PiLoop
from pi_bridge.service import PiSessions
from pianchang.app_service import write_json
from pianchang.bailian_text import TextConfig
from tools.workbench_loop_script import accept_script_reply, script_question
from tools.workbench_loop_watch import _folder
from trace_system import get_tracer, use_context


class ScriptCreativeInputTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        tracer = get_tracer(self.root / "traces")
        self.enterContext(use_context(None, tracer=tracer))
        self.enterContext(tracer.trace("test.creative_input", inputs={"offline": True}))
        self.job = {"id": "a"*32, "kind": "studio_script", "trace_id": "b"*32, "status": "interrupted",
                    "project_id": None}
        service = SimpleNamespace(root=self.root, tracer=tracer, state={"jobs":[self.job]}, lock=threading.RLock(),
                                  _save=Mock(), _story_record=Mock(return_value={"project_id": None}))
        self.sessions = PiSessions(service)
        self.sessions.close()
        self.sessions.executor = Mock()  # Job/message storage is real. No creative worker is run.
        self.session = self.sessions.resolve({"story": self.job["id"]}, "script")
        self.quote = "等我从雨夜、清晨里选好故事结局再写。"
        self.sessions.repo.append(self.session["id"], "user", self.quote, message_id=self.job["id"],
                                  job_id=self.job["id"], status="delivered", mode="work")
        self.proposed = {"topic":"ending", "source_quote":self.quote, "choices":["雨夜","清晨"]}

    def capture(self):
        loop = PiLoop(None, [], self.root, snapshot={}, finish_check=lambda: {"complete": False},
                      creative_input_check=lambda p: validate_reservation(self.root, self.session, self.job, p))
        args = {"outcome":"needs_input", "summary":"等作者补充结局。", "creative_input":self.proposed}
        loop.stop_check = lambda record: {"allowed": False}
        self.assertTrue(loop.execute("finish_task", args, "mock")["finish_rejected"])
        loop.stop_check = lambda record: {"allowed": True}
        loop.execute("finish_task", args, "mock")
        check = loop.outcome[2]
        self.job["result"] = {"status":"needs_input", "backend":"pi", "reason":check["reason"], "completion_check":check}
        return script_question(self.root, self.job)

    def save_answer(self, request):
        receipt = {"job_id":self.job["id"], "request_id":request["id"]}
        write_json(_folder(self.root)/"answers"/(request["id"]+".json"),
                   {**receipt, "request":request, "answers":{"answer":"清晨"}})
        return receipt

    def test_scoped_reply_and_lost_response(self):
        request = self.capture()
        self.assertEqual(request["fields"][0]["type"], "select")
        self.assertEqual(request["story_id"], self.job["id"])
        self.assertIsNone(request["project_id"])
        receipt = self.save_answer(request)
        first = accept_script_reply(self.sessions, receipt)
        self.assertFalse(first["duplicate"])
        self.assertEqual(first["session_id"], self.session["id"])
        self.assertIsNone(script_question(self.root, self.job))
        again = accept_script_reply(self.sessions, receipt)
        self.assertTrue(again["duplicate"])
        self.assertEqual(first["message_id"], again["message_id"])
        self.assertEqual(first["job_id"], again["job_id"])
        self.assertEqual(self.sessions.executor.submit.call_count, 1)
        child = self.sessions.service.state["jobs"][-1]
        self.assertEqual((child["kind"],child["story_job_id"],child["_pi_mode"],child["_pi_creative_answer"]),
                         ("pi_script",self.job["id"],"work",request["id"]))
        self.assertNotIn("_pi_continue_commission", child)
        saved_message = next(m for m in self.sessions.repo.messages(self.session["id"]) if m["id"] == first["message_id"])
        self.assertEqual(saved_message["creative_answer"], request["id"])
        with self.assertRaises(ValueError):
            self.sessions.resume_queued(self.session, saved_message)
        self.sessions.repo.recover()
        recovered = next(m for m in self.sessions.repo.messages(self.session["id"]) if m["id"] == saved_message["id"])
        self.assertEqual(recovered["status"], "interrupted")
        self.assertEqual(accept_script_reply(self.sessions,receipt)["job_id"],first["job_id"])

    def test_superseded_by_new_author_message(self):
        request = self.capture()
        receipt = self.save_answer(request)
        self.sessions.repo.append(self.session["id"],"user","我已经选了清晨",status="delivered")
        self.assertIsNone(script_question(self.root,self.job))
        self.assertEqual(accept_script_reply(self.sessions,receipt)["status"],"superseded")
        self.sessions.executor.submit.assert_not_called()

    def test_rejects_model_guess_permissions_and_stale_draft(self):
        for changes in ({"_pi_mode":"inspect"},{"_pi_bus_message":"another-agent"},{"cancel_requested":True}):
            with self.assertRaises(ValueError):
                validate_reservation(self.root,self.session,{**self.job,**changes},self.proposed)
        for changes in ({"topic":"permission"},{"source_quote":"请自行决定故事结局。"},
                        {"source_quote":"等我授权拍摄并选好结局再写。"},{"choices":["雨夜","黄昏"]}):
            with self.assertRaises(ValueError):
                validate_reservation(self.root,self.session,self.job,{**self.proposed,**changes})
        request = self.capture()
        draft = self.root/"outputs/pi/workspaces"/self.session["story_id"]/"script/candidate.json"
        write_json(draft, {"title":"已有草稿"})
        self.assertIsNone(script_question(self.root,self.job))
        result = accept_script_reply(self.sessions,self.save_answer(request))
        self.assertEqual(result["status"],"superseded")

    def test_free_text_and_unstructured_stop(self):
        self.proposed.pop("choices")
        request = self.capture()
        self.assertEqual(request["fields"][0]["type"], "text")
        self.job["result"]["completion_check"].pop("creative_input")
        self.assertIsNone(script_question(self.root,self.job))
        loop = PiLoop(None, [], self.root, snapshot={}, finish_check=lambda: {"complete":False})
        with self.assertRaises(ValueError):
            loop.execute("finish_task", {"outcome":"needs_input","summary":"需要资料","creative_input":self.proposed}, "mock")

    def test_does_not_turn_a_negated_author_statement_into_a_blocker(self):
        self.sessions.repo.append(self.session["id"], "user", "不用" + self.quote, job_id=self.job["id"], status="delivered")
        with self.assertRaises(ValueError):
            validate_reservation(self.root,self.session,self.job,self.proposed)

    def test_native_pi_returns_typed_stop_without_creative_service(self):
        calls = []
        def exchange(payload):
            calls.append(payload)
            finish = next(t["function"] for t in payload["tools"] if t["function"]["name"] == "finish_task")
            self.assertIn("creative_input", finish["parameters"]["properties"])
            args = {"outcome":"needs_input","summary":"需要原作者结局资料。","creative_input":self.proposed}
            return {"id":"offline-creative", "model":"offline-model", "created":0, "choices":[{"index":0,
                    "message":{"role":"assistant","content":None,"tool_calls":[{"id":"offline-question","type":"function",
                    "function":{"name":"finish_task","arguments":json.dumps(args,ensure_ascii=False)}}]},
                    "finish_reason":"tool_calls"}], "usage":{"prompt_tokens":10,"completion_tokens":10,"total_tokens":20}}
        client = SimpleNamespace(config=TextConfig(api_key="offline-no-network",model="offline-model"),pi_exchange=exchange)
        loop = PiLoop(client, [], self.root/"native", snapshot={}, finish_check=lambda: {"complete":False},
                      creative_input_check=lambda p: validate_reservation(self.root,self.session,self.job,p))
        self.job["result"] = loop.run(self.quote,max_steps=2)
        self.assertEqual(self.job["result"]["status"],"needs_input")
        self.assertEqual(self.job["result"]["reason"],"script_creative_input")
        self.assertEqual(script_question(self.root,self.job)["fields"][0]["options"][1]["value"],"清晨")
        self.assertEqual(len(calls),1)
