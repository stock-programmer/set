"""Offline real HTTP/SSE and Trace tests; no model/provider calls."""
import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from pianchang.app_service import AppService
from pi_bridge.live import decode_cursor,encode_cursor,project_event,read_session_events,session_sources
from pi_bridge.service import get_sessions
from trace_system import current_context,event,span,use_context
from pianchang.web_server import create_server


class PiLiveTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.enterContext(patch.dict(os.environ,{"AGENT_TRACE_DIR":str(self.root/"traces")}))
        (self.root/".env").write_text("# explicit offline fixture\n",encoding="utf-8")
        self.service=AppService(self.root);self.addCleanup(self.service.close)
        self.tracer=self.service.tracer;self.enterContext(self.tracer.storage_session());self.enterContext(use_context(None,tracer=self.tracer))
        self.task=self.enterContext(self.tracer.trace("offline.pi_live",kind="test",inputs={"label":"explicit offline"}))
        self.story="a"*32
        self.service.state["jobs"].append({"id":self.story,"kind":"studio_script","status":"interrupted","project_id":None,"story_input":{"idea":"offline live story"}})
        self.sessions=get_sessions(self.service);self.session=self.sessions.resolve({"story":self.story},"video")
        self.job={"id":"b"*32,"kind":"pi_video","session_id":self.session["id"],"project_id":None,"status":"running","trace_id":self.task.trace_id}
        self.service.state["jobs"].append(self.job);self.sessions.repo.update(self.session["id"],active_job=self.job["id"])
        self.session=self.sessions.repo.get(self.session["id"])
        self.enterContext(patch("urllib.request.OpenerDirector.open",side_effect=AssertionError("No provider calls in offline SSE tests")))

    def test_cursor_resume_is_deduplicated_scoped_and_never_dispatches(self):
        event("pi.session.event",{"type":"message_end","message":{"role":"assistant","timestamp":1,"content":[
            {"type":"thinking","thinking":"PRIVATE INTERNAL"},{"type":"text","text":"offline public explanation"}]}})
        other=self.tracer.start_span("offline.other_story",new_trace=True)
        with use_context(other.context,tracer=self.tracer):event("pi.session.event",{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"FOREIGN STORY"}]}})
        with patch.object(self.service,"submit",side_effect=AssertionError("Read must not dispatch")):
            first=read_session_events(self.service,self.session,{other.trace_id:0})
            text=json.dumps(first,ensure_ascii=False)
            self.assertIn("offline public explanation",text);self.assertNotIn("PRIVATE INTERNAL",text);self.assertNotIn("FOREIGN STORY",text)
            self.assertNotIn(other.trace_id,decode_cursor(first["cursor"]))
            repeated=read_session_events(self.service,self.session,decode_cursor(first["cursor"]))
            self.assertEqual(repeated["events"],[])
            event("pi.tool.request",{"tool":"regenerate_shot","call_id":"offline-call","arguments":{"shot_id":"S002","private_prompt":"must not publish"}})
            next_page=read_session_events(self.service,self.session,decode_cursor(first["cursor"]))
            self.assertEqual(next_page["events"][0]["arguments"],{"shot_id":"S002"})
            self.assertNotIn("private_prompt",json.dumps(next_page))
            self.assertEqual(len(self.service.state["jobs"]),2)
            with self.assertRaisesRegex(ValueError,"offline inline limit"):
                with span("content.model.observe_video",kind="validation"):
                    raise ValueError("offline inline limit; model request not sent")
            failed=read_session_events(self.service,self.session,decode_cursor(next_page["cursor"]))
            errors=[e for e in failed["events"] if e.get("status")=="ERROR"]
            self.assertEqual(errors[0]["error"],"offline inline limit; model request not sent")

    def test_handoff_follows_actual_worker_checkpoint_in_same_story(self):
        pid="c"*16;cid="d"*32;source_id="e"*32
        self.job.update(project_id=pid,commission_id=cid,commission_job_id=source_id)
        self.sessions.repo.update(self.session["id"],project_id=pid)
        self.session=self.sessions.repo.get(self.session["id"])
        self.service.state["jobs"].append({"id":source_id,"kind":"studio_video","project_id":pid,"story_id":self.session["story_id"],"run_id":self.story,"message_id":cid,"status":"failed"})
        root=self.tracer.start_span("offline.production_handoff",new_trace=True)
        path=self.root/"outputs/collaborations/runs"/self.story/("video-spec-"+cid)/"agent/run.json"
        path.parent.mkdir(parents=True);path.write_text(json.dumps({"trace_context":root.context,"status":"running","steps":6,
            "pending_tool":{"tool":"regenerate_shot","arguments":{"shot_id":"S002"}}}),encoding="utf-8")
        with use_context(root.context,tracer=self.tracer):event("pi.tool.request",{"tool":"regenerate_shot","arguments":{"shot_id":"S002"},"call_id":"worker-call"})
        feed=read_session_events(self.service,self.session,{})
        self.assertIn(root.trace_id,feed["trace_ids"])
        self.assertEqual(feed["execution"]["worker"]["shot_id"],"S002")
        self.assertTrue(any(e["trace_id"]==root.trace_id and e["type"]=="tool_start" for e in feed["events"]))
        self.job["status"]="interrupted"
        stopped=session_sources(self.service,self.session)["execution"]["worker"]
        self.assertEqual(stopped["status"],"interrupted");self.assertEqual(stopped["recorded_status"],"running")
        self.assertTrue(stopped["state_conflict"])
        self.service.state["jobs"][-1]["story_id"]="different-story"
        self.assertNotIn(root.trace_id,session_sources(self.service,self.session)["trace_ids"])

    def test_sse_real_transport_reconnect_and_origin_boundary(self):
        server=create_server(self.service,port=0);server.daemon_threads=False
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def stop():server.shutdown();server.server_close();thread.join(5)
        self.addCleanup(stop)
        route="/api/pi/events/stream?story="+self.story+"&role=video"
        event("pi.model.purpose",{"purpose":"decision"})
        def connect(headers=None,method="GET"):
            connection=http.client.HTTPConnection("127.0.0.1",server.server_address[1],timeout=10)
            connection.request(method,route,headers=headers or {});return connection,connection.getresponse()
        def frame(response):
            rows=[]
            while True:
                line=response.readline().decode("utf-8").strip()
                if not line and rows:break
                if line:rows.append(line)
            return json.loads(next(line[6:] for line in rows if line.startswith("data: ")))
        with patch.object(self.service,"submit",side_effect=AssertionError("SSE cannot dispatch")):
            first,response=connect();self.assertEqual(response.status,200)
            self.assertIn("text/event-stream",response.getheader("Content-Type"))
            initial=frame(response);first.close()
            event("pi.tool.request",{"tool":"generate_shot","arguments":{"shot_id":"S001"},"call_id":"next-offline"})
            second,response=connect({"Last-Event-ID":initial["cursor"]})
            updated=frame(response);second.close()
            self.assertTrue(any(e["type"]=="tool_start" for e in updated["events"]))
            self.assertFalse(any(e["type"]=="model_start" for e in updated["events"]))
            rejected,response=connect({"Origin":"https://untrusted.example"});self.assertEqual(response.status,403);response.read();rejected.close()
            head,response=connect(method="HEAD");self.assertEqual(response.status,405);response.read();head.close()
            self.assertEqual(self.job["status"],"running")

    def test_malformed_cursor_is_rejected(self):
        for value in ["!notjson",encode_cursor({"not-a-trace":0}),encode_cursor({"a"*32:-1}),encode_cursor({"a"*32:True})]:
            with self.assertRaises(ValueError):decode_cursor(value)


if __name__=="__main__":unittest.main()
