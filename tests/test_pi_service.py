"""Offline workbench delivery/collaboration integration. No model API calls."""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pianchang.app_service import AppService
from pianchang.collab_store import CollabStore
from pi_bridge.service import get_sessions
from trace_system import use_context


class PiServiceTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        env=patch.dict(os.environ,{"AGENT_TRACE_DIR":str(self.root/"traces")});env.start();self.addCleanup(env.stop)
        (self.root/".env").write_text("# explicit offline fixture\n",encoding="utf-8")
        self.service=AppService(self.root);self.addCleanup(self.service.close)
        self.context=use_context(None,tracer=self.service.tracer);self.context.__enter__();self.addCleanup(self.context.__exit__,None,None,None)
        self.story="a"*32
        self.service.state["jobs"].append({"id":self.story,"kind":"studio_script","status":"interrupted",
            "project_id":None,"story_input":{"idea":"offline fixture story"}})
        self.service._save();self.sessions=get_sessions(self.service)

    def wait_until(self,predicate):
        deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            if predicate():return
            time.sleep(0.1)
        self.fail("Offline delivery did not settle within the bounded wait")

    def test_messages_queued_during_execution_are_not_lost_or_duplicated(self):
        started=threading.Event();release=threading.Event();self.addCleanup(release.set);calls=[]
        def execute(session,job,text):
            self.sessions.repo.delivered(job["_pi_message_id"])  # explicit offline callback fixture
            calls.append(text)
            if len(calls)==1:started.set();release.wait(timeout=45)
            return {"backend":"explicit offline service fixture","task_complete":False,"summary":"offline response"}
        with patch.object(self.sessions,"run_script",side_effect=execute):
            first=self.sessions.submit({"story":self.story},"script","first message","b"*32)
            self.assertTrue(started.wait(timeout=30))
            second=self.sessions.submit({"story":self.story},"script","follow up","c"*32)
            duplicate=self.sessions.submit({"story":self.story},"script","follow up","c"*32)
            self.assertTrue(second["queued"]);self.assertTrue(duplicate["duplicate"])
            release.set()
            self.wait_until(lambda:len(calls)==2 and self.sessions.repo.get(first["session_id"])["active_job"] is None)
            messages=self.sessions.repo.messages(first["session_id"])
            self.assertEqual(sum(m["role"]=="user" for m in messages),2)
            self.assertEqual(sum(m["role"]=="assistant" for m in messages),2)
            self.assertTrue(all(m["status"]=="delivered" for m in messages))

    def test_failed_turn_has_durable_feedback_and_reloaded_execution_evidence(self):
        def fail(session,job,text):
            self.sessions.repo.delivered(job["_pi_message_id"])
            raise ValueError("offline preflight: image_count=8, allowed=1..5")
        with patch.object(self.sessions,"run_video",side_effect=fail):
            first=self.sessions.submit({"story":self.story},"video","offline diagnose failure","d"*32)
            self.wait_until(lambda:self.sessions.repo.get(first["session_id"])["active_job"] is None)
            snapshot=self.sessions.view({"story":self.story},"video")
            self.assertEqual(snapshot["execution"]["status"],"failed")
            self.assertIn("image_count=8",snapshot["execution"]["error"])
            feedback=[m for m in snapshot["messages"] if m["role"]=="assistant"]
            self.assertEqual(len(feedback),1)
            self.assertIn("image_count=8",feedback[0]["text"])
            self.assertEqual(feedback[0]["trace_id"],snapshot["execution"]["trace_id"])
            duplicate=self.sessions.submit({"story":self.story},"video","offline diagnose failure","d"*32)
            self.assertTrue(duplicate["duplicate"])
            self.assertEqual(len(self.sessions.view({"story":self.story},"video")["messages"]),2)

    def test_video_diagnostics_rejects_frozen_overflow_inside_pi_tool(self):
        session=self.sessions.resolve({"story":self.story},"video")
        session["project_id"]="b"*16
        current={"commission_id":"f"*32,"resumable":True,"last_execution":None,"blockers":[],"job_id":"source"}
        observed={"preflight_ready":False,"shots":[{"shot_id":"S001","status":"FAIL","image_count":8}]}
        captured={}
        class OfflineLoop:
            def __init__(self,client,tools,directory,**options):
                self.record={"assistant_text":"offline: exact preflight failed"};captured["tools"]={t.name:t for t in tools}
            def run(self,text,**options):
                captured["continuation"]=captured["tools"]["continue_authorized_production"].execute("f"*32)
                return {"turn_complete":True,"task_complete":False,"summary":"offline diagnostic only"}
        with patch("pi_bridge.service.PiLoop",OfflineLoop),patch("pi_bridge.diagnostics.production_diagnostics",return_value=observed),\
                patch("pianchang.video_execution.current_commission",return_value=current),patch("pianchang.studio_jobs.resume_video_job") as resume:
            result=self.sessions.run_video(session,{"id":"offline-job"},"offline diagnostic")
            self.assertFalse(captured["continuation"]["accepted"])
            self.assertEqual(captured["continuation"]["diagnostics"]["shots"][0]["image_count"],8)
            self.assertIn("read_production_diagnostics",captured["tools"])
            self.assertTrue(result["turn_complete"]);resume.assert_not_called()

    def test_deferred_production_failure_cannot_erase_turn_or_claim_success(self):
        session=self.sessions.resolve({"story":self.story},"video");session["project_id"]="b"*16
        job={"id":"offline-conversation","status":"running"}
        self.service.state["jobs"].extend([job,{"id":"source"}])
        current={"commission_id":"f"*32,"resumable":True,"last_execution":None,"blockers":[],"job_id":"source","limits":{},"usage":{}}
        class OfflineLoop:
            def __init__(self,client,tools,directory,**options):
                self.tools={t.name:t for t in tools};self.record={"assistant_text":"offline model mistakenly says success"}
            def run(self,text,**options):
                self.tools["continue_authorized_production"].execute("f"*32)
                return {"turn_complete":True,"task_complete":False,"summary":"offline handoff accepted"}
        with patch("pi_bridge.service.PiLoop",OfflineLoop),patch("pi_bridge.diagnostics.production_diagnostics",return_value={"preflight_ready":True}),\
                patch("pianchang.video_execution.current_commission",return_value=current),patch("pianchang.studio_jobs.resume_video_job",side_effect=ValueError("offline actual deferred failure")):
            result=self.sessions.run_video(session,job,"offline explicit continuation")
        self.assertFalse(result["task_complete"]);self.assertFalse(result["turn_complete"])
        self.assertIn("offline actual deferred failure",result["assistant_text"])
        self.assertNotIn("mistakenly says success",result["assistant_text"])
        self.assertEqual(job["continuation_error"],"offline actual deferred failure")

    def test_cross_agent_request_reply_and_wakeup_preserve_story_and_pins(self):
        story_id="story-"+self.story
        with self.service.tracer.trace("test.pi.collaboration",inputs={"label":"explicit offline service fixture"}):
            store=CollabStore(self.root/"outputs/collaborations/assets.sqlite3")
            asset=store.publish_asset(story_id+"/script",{"label":"offline pinned script"},kind="script",producer="script",story_id=story_id)
            source=self.sessions.resolve({"story":self.story},"script")
            def response(session,job,text):
                self.sessions.repo.delivered(job["_pi_message_id"])  # explicit offline callback fixture
                return {"backend":"explicit offline service fixture","task_complete":False,"summary":"offline: reference assets still missing"}
            with patch.object(self.sessions,"run_video",side_effect=response),patch.object(self.sessions,"run_script",side_effect=response):
                submitted=self.sessions.ask(source,"video","只读核对，禁止生成视频")
                self.wait_until(lambda:bool(store.messages(causation_id=submitted["request_id"],message_type="agent.conversation_response")))
                self.wait_until(lambda:self.sessions.repo.get(source["id"])["active_job"] is None and
                    any(m["role"]=="assistant" for m in self.sessions.repo.messages(source["id"])))
                request=store.get_message(submitted["request_id"])
                self.assertEqual(request["story_id"],story_id)
                self.assertEqual(request["payload"]["pins"][0]["sha256"],asset["sha256"])
                reply=store.messages(causation_id=submitted["request_id"],message_type="agent.conversation_response")[0]
                self.assertEqual(reply["sender"],"video");self.assertFalse(reply["payload"]["task_complete"])
                self.assertEqual(store.get_message(submitted["request_id"])["deliveries"]["video"],"completed")
                self.assertTrue(any(m["role"]=="agent" for m in self.sessions.repo.messages(source["id"])))

    def test_request_queued_behind_busy_agent_keeps_causation_and_wakes_sender(self):
        started=threading.Event();release=threading.Event();self.addCleanup(release.set);calls=[]
        def video(session,job,text):
            self.sessions.repo.delivered(job["_pi_message_id"],job_id=job["id"])
            calls.append(job.get("_pi_bus_message"))
            if len(calls)==1:started.set();release.wait(timeout=45)
            return {"summary":"offline busy-agent reply","turn_complete":True,"task_complete":False}
        def script(session,job,text):
            self.sessions.repo.delivered(job["_pi_message_id"],job_id=job["id"])
            return {"summary":"offline sender resumed","turn_complete":True,"task_complete":False}
        with self.service.tracer.trace("test.pi.busy_collaboration",inputs={"label":"explicit offline fixture"}),\
                patch.object(self.sessions,"run_video",side_effect=video),patch.object(self.sessions,"run_script",side_effect=script):
            self.sessions.submit({"story":self.story},"video","offline existing work","e"*32)
            self.assertTrue(started.wait(timeout=30))
            source=self.sessions.resolve({"story":self.story},"script")
            request=self.sessions.ask(source,"video","offline request while busy")
            release.set()
            store=CollabStore(self.root/"outputs/collaborations/assets.sqlite3")
            self.wait_until(lambda:bool(store.messages(causation_id=request["request_id"],message_type="agent.conversation_response")))
            self.wait_until(lambda:any(m["role"]=="assistant" for m in self.sessions.repo.messages(source["id"])))
            self.assertEqual(calls,[None,request["request_id"]])
            self.assertEqual(store.get_message(request["request_id"])["deliveries"]["video"],"completed")

if __name__=="__main__":unittest.main()
