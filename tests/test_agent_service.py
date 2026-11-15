"""Offline retirement of the duplicate Agent API; history and Pi session are retained."""
import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from pianchang.app_service import AppService,read_json,write_json
from pianchang.video_execution import StandaloneAgentRetired
from pianchang.web_server import create_server

class AgentServiceTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        self.enterContext(patch.dict(os.environ,{"AGENT_TRACE_DIR":str(self.root/"traces")}))
        (self.root/".env").write_text("# explicit offline fixture\n",encoding="utf-8")
        write_json(self.root/"fixtures/story_project.json",read_json(Path(__file__).resolve().parents[1]/"fixtures/story_project.json"))
        self.service=AppService(self.root);self.addCleanup(self.service.close);self.pid=next(iter(self.service._discover()))
        self.enterContext(patch("urllib.request.OpenerDirector.open",side_effect=AssertionError("No paid APIs in offline tests")))

    def test_all_old_completion_modes_are_gone_without_creating_a_task_or_calling_a_model(self):
        server=create_server(self.service,port=0);server.daemon_threads=False
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(lambda:(server.shutdown(),server.server_close(),thread.join(3)))
        before=list(self.service.state["jobs"])
        with patch("pianchang.app_service.BailianTextClient",side_effect=AssertionError("Duplicate model must not be constructed")):
            for mode in ("inspect","preview","accepted"):
                connection=http.client.HTTPConnection("127.0.0.1",server.server_address[1],timeout=60)
                connection.request("POST",f"/api/projects/{self.pid}/agent",json.dumps({"goal":"offline old request","mode":mode,"max_steps":30,"max_video_generations":10}),
                    {"Origin":f"http://127.0.0.1:{server.server_address[1]}","Content-Type":"application/json"})
                response=connection.getresponse();result=json.loads(response.read());connection.close()
                self.assertEqual(response.status,410);self.assertEqual(result["code"],"AGENT_ENTRY_MERGED")
                self.assertEqual(result["conversation"],{"scope":{"project":self.pid},"role":"video"})
                self.assertTrue(self.service.tracer.verify(response.getheader("X-Trace-Id"))["ok"])
        self.assertEqual(self.service.state["jobs"],before)

    def test_private_legacy_runner_also_cannot_start_a_second_model_loop(self):
        with self.assertRaises(StandaloneAgentRetired):self.service._run_agent({"project_id":self.pid},{"goal":"offline"},"offline")

    def test_old_records_remain_readable_and_cancelable(self):
        job={"id":"a"*32,"kind":"agent","status":"running","project_id":self.pid,"message":"explicit offline historical fixture"}
        self.service.state["jobs"].append(job);self.service._save()
        self.service.cancel_agent(job["id"])
        self.assertTrue(job["cancel_requested"])
        self.assertTrue(any(j["id"]==job["id"] for j in self.service.list_jobs()))

    def test_plan_submission_runs_in_its_reserved_video_session_and_releases_after_terminal_status(self):
        from pi_bridge.runtime import BINDING
        from pi_bridge.service import get_sessions
        from pianchang.story_spec import digest_json
        sessions=get_sessions(self.service);session=sessions.resolve({"project":self.pid},"video")
        entered=threading.Event();release=threading.Event();self.addCleanup(release.set)
        observed={}
        def offline_worker(service,job,body):
            observed.update(options=BINDING.get()("video"),job=dict(job))
            entered.set();release.wait(30)
            return {"action_required":True,"task_complete":False,"summary":"explicit offline preflight blocker; no actual media acceptance"}
        body={"plan_id":"d"*64,"project_sha256":digest_json(self.service._project(self.pid)[1])}
        with patch("pianchang.studio_jobs.run_video_job",side_effect=offline_worker),patch("pianchang.video_autonomy.enqueue_recovery"):
            response=self.service.submit("studio_video",self.pid,body,"offline-plan-id")
            self.assertTrue(entered.wait(30))
            duplicate=self.service.submit("studio_video",self.pid,body,"offline-plan-id")
            self.assertEqual(response["id"],duplicate["id"])
            self.assertEqual(response["session_id"],session["id"])
            self.assertEqual(observed["job"]["session_id"],session["id"])
            self.assertIn("[message_id="+response["id"]+"]",observed["options"]["initial_message"])
            self.assertEqual(sessions.repo.get(session["id"])["active_job"],response["id"])
            release.set();deadline=time.monotonic()+45
            while sessions.repo.get(session["id"])["active_job"] and time.monotonic()<deadline:time.sleep(.1)
            self.assertIsNone(sessions.repo.get(session["id"])["active_job"])
        raw=next(j for j in self.service.state["jobs"] if j["id"]==response["id"])
        self.assertEqual(raw["status"],"interrupted")
        messages=sessions.repo.messages(session["id"])
        self.assertEqual(sum(m["id"]==response["id"] for m in messages),1)
        self.assertEqual(next(m for m in messages if m["id"]==response["id"])["status"],"not_executed")
        self.assertTrue(any(m["role"]=="assistant" and m["job_id"]==response["id"] for m in messages))
        self.assertTrue(self.service.tracer.verify(response["trace_id"])["ok"])

if __name__=="__main__":unittest.main()
