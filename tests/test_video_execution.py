"""Explicit offline single-session commission tests. No model/provider requests."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch

from pianchang.bailian_text import TextConfig
from pianchang.bailian_video import VideoConfig
from pianchang.collab_store import CollabStore
from pi_bridge.runtime import BINDING
from pi_bridge.service import PiSessions
from trace_system import current_context,get_tracer,use_context
from pianchang.video_execution import current_commission,execution_limits


class VideoExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        tracer=get_tracer(self.root/"traces");self.enterContext(use_context(None,tracer=tracer))
        self.enterContext(patch("urllib.request.OpenerDirector.open",side_effect=AssertionError("Offline fixture forbids provider requests")))
        self.enterContext(tracer.trace("test.video.single_agent",inputs={"label":"explicit offline fixtures"}))
        self.pid="a"*16;self.run_id="b"*32;self.story="story-"+self.run_id
        self.store=CollabStore(self.root/"outputs/collaborations/assets.sqlite3")
        spec=self.store.publish_asset(self.story+"/spec",{"shots":[{"id":"S001","required_refs":[]}]},kind="production_spec",producer="script",story_id=self.story)
        self.view={"project_id":self.pid,"story_id":self.story,"run_id":self.run_id,"store_path":str(self.store.path),
            "script_ref":{"asset_id":self.story+"/script","revision_id":"r1","sha256":"1"*64},
            "spec":{k:spec[k] for k in ("asset_id","revision_id","sha256")}}
        self.service=SimpleNamespace(root=self.root,lock=threading.RLock(),state={"jobs":[]},text_config=TextConfig(api_key="offline-no-network"),video_config=VideoConfig(api_key="offline-no-network"),tracer=tracer)
        self.service.list_jobs=lambda:self.service.state["jobs"]
        self.service._save=lambda:None
        self.service._update=lambda job,**fields:job.update(fields)
        self.enterContext(patch("pianchang.studio_assets.studio_project_for_video",return_value=self.view))
        self.sessions=PiSessions(self.service);self.addCleanup(self.sessions.close)
        self.session=self.sessions.repo.ensure(self.story,"video",project_id=self.pid)
        self.cid="c"*32
        policy={"shot_ids":["S001"],"max_repairs_per_shot":2,"max_local_edits_per_shot":2}
        self.plan={"plan_id":"d"*64,"new_shots":["S001"],"retake_shots":[],"reuse_shots":[],"acceptance_loop":policy}
        self.store.send("production.requested",sender="commissioner",recipient="video",message_id=self.cid,story_id=self.story,
            payload={"script":self.view["script_ref"],"template":self.view["spec"],"shooting_plan":self.plan,"max_video_generations":3})
        self.source={"id":"e"*32,"project_id":self.pid,"kind":"studio_video","status":"interrupted","message_id":self.cid,"run_id":self.run_id,"trace_id":current_context()["trace_id"]}
        self.service.state["jobs"].append(self.source)
        self.path=self.root/"outputs/collaborations/runs"/self.run_id/("video-spec-"+self.cid)/"agent/run.json"
        self.path.parent.mkdir(parents=True);self.path.write_text(json.dumps({"steps":7,"video_generations":2,"repair_reservations":{"S001":1},
            "limits":{"max_steps":32,"max_video_generations":3}}),encoding="utf-8")

    def test_video_detail_reads_frozen_order_without_opening_review_or_new_plan(self):
        from pianchang.studio_flow import studio_video_detail
        pin = {"asset_id": "reference", "revision_id": "r1", "sha256": "9"*64}
        view = {**self.view, "production_ref": None, "pending_script_ref": None, "published_delivery": None,
                "project_path": str(self.root/"project.json"), "project": {"title": "offline", "shots": [
                    {"id": "S001", "description": "offline shot", "required_refs": [pin]}]}}
        self.source.update(plan_id="pinned", cancel_requested=True, error={"message":"offline error", "payload":"omit"},
                           result={"turn_complete": True, "action_required": True, "summary":"offline summary",
                                   "provider_response": "omit", "extra_retake": {"proposal_id":"request", "payload":"omit"}})
        self.service.state["jobs"].append({**self.source, "project_id": "other", "id": "f"*32})
        with patch("pianchang.studio_assets.studio_project_for_video", return_value=view), \
             patch("pianchang.studio_jobs.studio_video_plan", side_effect=AssertionError("should not authorize a new plan")), \
             patch("pianchang.studio_flow.studio_asset_catalog", side_effect=AssertionError("irrelevant full catalog")), \
             patch("pi_bridge.input_tools.reference_inputs_view", side_effect=AssertionError("on-demand reference checks")), \
             patch.object(CollabStore, "__init__", side_effect=AssertionError("read must not initialize a store")):
            detail = studio_video_detail(self.service, self.pid)
        self.assertEqual(detail["projection"], "video")
        self.assertIsNone(detail["video_plan"])
        self.assertEqual(detail["video_execution"]["plan"], self.plan)
        self.assertEqual(detail["video_execution"]["commission_id"], self.cid)
        self.assertEqual(detail["assets"]["shots"][0]["required_refs"], [pin])
        self.assertEqual([job["id"] for job in detail["jobs"]], [self.source["id"]])
        job = detail["jobs"][0]
        self.assertTrue(job["cancel_requested"])
        self.assertEqual(job["plan_id"], "pinned")
        self.assertEqual(job["error"], {"message":"offline error"})
        self.assertEqual(job["result"], {"turn_complete": True, "action_required": True, "summary":"offline summary",
                                         "extra_retake": {"proposal_id":"request"}})
        self.assertNotIn("preview_gate", detail)

    def test_current_scope_and_remaining_budget_are_read_from_original_receipts(self):
        original=self.path.read_bytes();state=current_commission(self.service,self.pid)
        self.assertEqual(state["limits"]["max_model_calls"],32)
        self.assertEqual(state["usage"]["remaining_video_generations"],1)
        self.assertTrue(state["resumable"]);self.assertEqual(self.path.read_bytes(),original)
        self.source["status"]="succeeded"
        self.assertFalse(current_commission(self.service,self.pid)["resumable"])

    def test_newer_completed_commission_and_changed_spec_cannot_fall_back_to_old_failure(self):
        new={**self.source,"id":"f"*32,"status":"succeeded"};self.service.state["jobs"].append(new)
        current=current_commission(self.service,self.pid)
        self.assertEqual(current["job_id"],new["id"]);self.assertFalse(current["resumable"])
        self.service.state["jobs"].pop();self.view["spec"]={**self.view["spec"],"revision_id":"r2"}
        self.assertFalse(current_commission(self.service,self.pid)["resumable"])

    def test_failed_plan_preflight_does_not_replace_the_frozen_commission(self):
        self.service.state["jobs"].append({"id":"f"*32,"kind":"studio_video","project_id":self.pid,"status":"failed"})
        self.assertEqual(current_commission(self.service,self.pid)["commission_id"],self.cid)

    def test_approval_and_queued_messages_share_one_session_and_unexecuted_plan_is_not_replayed(self):
        job={"id":"f"*32,"kind":"studio_video","project_id":self.pid,"status":"queued","trace_id":current_context()["trace_id"]}
        def enqueue():self.service.state["jobs"].append(job);return job
        response=self.sessions.submit_production(self.pid,{"plan_id":"d"*64},enqueue)
        self.assertEqual(response["session_id"],self.session["id"])
        self.assertIn("[message_id="+job["id"]+"]",self.sessions._options(self.session,job)["initial_message"])
        self.sessions.repo.append(self.session["id"],"user","offline follow up",message_id="1"*32)
        job.update(status="failed",message="offline preflight failure")
        with patch.object(self.sessions,"resume_queued") as resume:
            self.sessions.release_production(job)
            self.assertEqual(resume.call_args.args[1]["id"],"1"*32)
        approval=next(m for m in self.sessions.repo.messages(self.session["id"]) if m["id"]==job["id"])
        self.assertEqual(approval["status"],"not_executed")
        self.assertIsNone(self.sessions.repo.get(self.session["id"])["active_job"])

    def test_busy_conversation_cannot_start_a_second_production(self):
        active={"id":"f"*32,"kind":"pi_video","status":"running","project_id":self.pid}
        self.service.state["jobs"].append(active);self.sessions.repo.update(self.session["id"],active_job=active["id"])
        with patch.object(self.service,"_save") as enqueue:
            with self.assertRaises(ValueError):self.sessions.submit_production(self.pid,{},enqueue)
            enqueue.assert_not_called()

    def test_conversation_resumes_exact_commission_in_same_binding_without_resetting_budget(self):
        job={"id":"f"*32,"kind":"pi_video","status":"running","project_id":self.pid,"_pi_mode":"work"}
        self.service.state["jobs"].append(job);self.sessions.repo.update(self.session["id"],active_job=job["id"])
        original=self.path.read_bytes();observed={}
        class OfflineLoop:
            def __init__(loop,client,tools,directory,**options):loop.tools={t.name:t for t in tools};loop.record={};observed["lock"]=options["execution_lock"]
            def run(loop,text,**options):
                current=loop.tools["read_current_commission"].execute()
                accepted=loop.tools["continue_authorized_production"].execute(commission_id=current["commission_id"])
                if not accepted["accepted"]:raise AssertionError(accepted)
                return {"turn_complete":True,"task_complete":False,"summary":"explicit offline continuation request"}
        def resumed(service,previous):
            self.assertEqual(previous["id"],self.source["id"])
            self.assertEqual(BINDING.get()("video")["execution_lock"],observed["lock"])
            return {"task_complete":True,"summary":"explicit offline worker completion; no actual media approval"}
        with patch("pi_bridge.service.PiLoop",OfflineLoop),patch("pianchang.studio_jobs.resume_video_job",side_effect=resumed) as resume:
            result=self.sessions.run_video(self.session,job,"offline: continue the existing commission")
        self.assertEqual(resume.call_count,1);self.assertTrue(result["task_complete"])
        self.assertEqual(job["commission_id"],self.cid);self.assertEqual(self.source["status"],"interrupted")
        self.assertEqual(self.path.read_bytes(),original)

    def test_inspection_does_not_expose_mutating_continuation_or_claim_media_completion(self):
        observed={}
        class OfflineLoop:
            def __init__(loop,client,tools,directory,**options):observed["tools"]={t.name for t in tools};loop.record={}
            def run(loop,*args,**options):return {"turn_complete":True,"task_complete":False,"summary":"offline read-only response"}
        job={"id":"f"*32,"kind":"pi_video","status":"running","project_id":self.pid,"_pi_mode":"inspect"}
        with patch("pi_bridge.service.PiLoop",OfflineLoop),patch("pianchang.studio_jobs.resume_video_job") as resume:
            result=self.sessions.run_video(self.session,job,"offline inspect only")
        self.assertNotIn("continue_authorized_production",observed["tools"]);resume.assert_not_called();self.assertFalse(result["task_complete"])

    def test_proposed_limits_match_the_production_loop_formula(self):
        self.assertEqual(execution_limits(self.plan,3)["max_model_calls"],32)

    def test_background_recovery_reserves_same_session_and_skips_newer_or_busy_work(self):
        from pianchang.video_autonomy import enqueue_recovery
        self.service._pi_sessions=self.sessions;self.service.executor=Mock();self.service._execute=Mock()
        self.service._project=lambda pid:(None,{"shots":[]})
        self.source.update(_autonomy_enabled=True,_autonomy_body={"plan_id":"d"*64})
        active={"id":"f"*32,"kind":"pi_video","project_id":self.pid,"status":"running"}
        self.service.state["jobs"].append(active);self.sessions.repo.update(self.session["id"],active_job=active["id"])
        self.assertEqual(enqueue_recovery(self.service)["jobs"],[])
        self.sessions.repo.update(self.session["id"],active_job=None);active["status"]="succeeded"
        newer={**self.source,"id":"1"*32,"status":"succeeded"};self.service.state["jobs"].append(newer)
        self.assertEqual(enqueue_recovery(self.service)["jobs"],[])
        self.service.state["jobs"].pop();original=self.path.read_bytes()
        self.assertEqual(enqueue_recovery(self.service)["jobs"],[self.source["id"]])
        self.assertEqual(self.sessions.repo.get(self.session["id"])["active_job"],self.source["id"])
        self.assertEqual(self.source["session_id"],self.session["id"])
        self.assertEqual(self.path.read_bytes(),original);self.service.executor.submit.assert_called_once()

    def test_recovery_disk_scan_does_not_hold_global_lock_and_rechecks_cancellation(self):
        from concurrent.futures import ThreadPoolExecutor
        from pianchang.video_autonomy import enqueue_recovery
        entered=threading.Event();release=threading.Event()
        self.source.update(_autonomy_enabled=True,_autonomy_body={},result={"action_required":True})
        self.service.executor=Mock();self.service._execute=Mock();self.service._project=lambda pid:(None,{"shots":[]})
        context=current_context()
        def inspect(directory):entered.set();release.wait(20);return True
        def run():
            with use_context(context,tracer=self.service.tracer):return enqueue_recovery(self.service)
        with patch("pianchang.video_autonomy.recoverable_order",side_effect=inspect),ThreadPoolExecutor(max_workers=1) as executor:
            future=executor.submit(run)
            try:
                self.assertTrue(entered.wait(10))
                self.assertTrue(self.service.lock.acquire(timeout=.2),"Sidebar/project reads must not wait for media scans")
                try:self.source["cancel_requested"]=True
                finally:self.service.lock.release()
            finally:release.set()
            self.assertEqual(future.result(timeout=10)["jobs"],[])
        self.service.executor.submit.assert_not_called()


if __name__=="__main__":unittest.main()
