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
    def test_continuation_waits_for_a_review_job_then_resumes_once(self):
        review = {"id":"9"*32,"kind":"studio_review","project_id":self.pid,"status":"running",
                  "message":"offline review","trace_id":"8"*32}
        self.service.state["jobs"].append(review)
        with patch.object(self.sessions.executor,"submit") as start:
            queued = self.sessions.submit({"project":self.pid},"video","继续","7"*32,continue_commission=self.cid)
            self.assertTrue(queued["queued"])
            start.assert_not_called()
            review["status"] = "succeeded"
            self.sessions.release_review(review)
            self.sessions.release_review(review)
            start.assert_called_once()
            self.assertEqual(start.call_args.args[2]["_pi_continue_commission"],self.cid)

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

    def test_button_continuation_queues_once_outside_steering_and_keeps_both_click_receipts(self):
        session_id = self.session["id"]
        scope = {"project": self.pid}
        busy = {"id": "1"*32, "kind": "pi_video", "status": "running"}
        self.service.state["jobs"].append(busy)
        self.sessions.repo.update(session_id, active_job=busy["id"])
        with patch.object(self.sessions.executor, "submit") as dispatch:
            first = self.sessions.submit(scope, "video", "继续", "2"*32, continue_commission=self.cid)
            self.assertTrue(first["queued"])
            self.assertEqual(first["message"]["continue_commission"], self.cid)
            joined = self.sessions.submit(scope, "video", "继续", "3"*32, continue_commission=self.cid)
            self.assertTrue(joined["duplicate"])
            self.assertEqual(joined["message"]["job_id"], first["message"]["job_id"])
            with self.assertRaises(ValueError):
                self.sessions.submit(scope, "video", "继续", "2"*32, continue_commission="4"*32)
            options = self.sessions._options(self.sessions.repo.get(session_id), busy)
            self.assertEqual(options["inbox"](), [])
            options["on_event"]({"message":{"role":"user","content":"[message_id=" + "2"*32 + "]"}})
            queued = [m for m in self.sessions.repo.messages(session_id) if m["status"]=="queued"]
            self.assertEqual([m["id"] for m in queued], ["2"*32])
            dispatch.assert_not_called()

            busy["status"] = "succeeded"
            self.sessions.repo.update(session_id, active_job=None)
            # After an intervening stop leaves the command waiting, an explicit click can wake it.
            self.sessions.repo.ensure(self.story,"video",source_job=self.run_id)
            started = self.sessions.submit(scope, "video", "继续", "5"*32, continue_commission=self.cid)
            job = dispatch.call_args.args[2]
            self.assertEqual(job["_pi_continue_commission"], self.cid)
            self.assertEqual(job["id"], first["message"]["job_id"])
            self.assertEqual(started["job"]["id"], job["id"])
            self.sessions.repo.delivered(queued[0]["id"])
            active_duplicate = self.sessions.submit(scope, "video", "继续", "4"*32, continue_commission=self.cid)
            self.assertEqual(active_duplicate["message"]["job_id"],job["id"])
            self.sessions.repo.update(session_id, active_job=None)
            for message_id in ("2"*32, "3"*32, "4"*32, "5"*32):
                result = self.sessions.submit(scope, "video", "继续", message_id, continue_commission=self.cid)
                self.assertTrue(result["duplicate"])
                self.assertEqual(result["message"]["job_id"], job["id"])
            dispatch.assert_called_once()

    def test_button_requires_project_video_work_user_and_structured_intent(self):
        scope = {"project":self.pid}
        for role, mode, sender, actual_scope in (
                ("video","inspect","user",scope), ("script","work","user",scope),
                ("video","work","agent",scope), ("video","work","user",{"story":self.run_id})):
            with self.subTest(role=role, mode=mode, sender=sender, scope=actual_scope), self.assertRaises(ValueError):
                self.sessions.submit(actual_scope, role, "继续", "2"*32, mode=mode, sender=sender, continue_commission=self.cid)
        with self.assertRaises(ValueError):
            self.sessions.submit(scope, "video", "继续", continue_commission=self.cid)
        with patch.object(self.sessions.executor,"submit") as dispatch:
            self.sessions.submit(scope, "video", "继续当前制作委托 " + self.cid, "2"*32)
            self.assertNotIn("_pi_continue_commission",dispatch.call_args.args[2])
            with self.assertRaises(ValueError):
                self.sessions.submit(scope,"video","继续当前制作委托 " + self.cid,"2"*32,continue_commission=self.cid)

    def test_confirmed_rejection_is_projected_from_frozen_inputs_and_hidden_during_new_attempt(self):
        from pianchang.story_spec import digest_json
        work=self.path.parents[1]
        shot="S001"
        repair_id="r1"
        key=digest_json({"order":str(work),"shot_id":shot,"repair_id":repair_id})
        trace_id="f"*32
        record=json.loads(self.path.read_bytes())
        self.path.write_text(json.dumps({**record,"shooting_plan":self.plan,"trace_context":{"trace_id":trace_id}}))
        stopped={"id":"1"*32,"kind":"pi_video","project_id":self.pid,"commission_id":self.cid,
                 "worker_trace_id":trace_id,"status":"interrupted","message":"unverified free text"}
        self.service.state["jobs"].append(stopped)
        pin={"asset_id":"asset","revision_id":"r1","purpose":"portrait","subject_id":"actor"}
        self.view["shots"]=[{"id":shot,"required_refs":[pin]}]
        output=self.root/"outputs/videos"
        self.service.video_config=VideoConfig(output_dir=output)
        path=output/"candidate/job.json"
        path.parent.mkdir(parents=True)
        meta={"execution_key":key,"shot_id":shot,"script_asset_ref":self.view["script_ref"],
              "dependency_sha256":"b"*64,"project_sha256":self.view["spec"]["sha256"],"shooting_plan_id":self.plan["plan_id"]}
        path.write_text(json.dumps({"metadata":meta}))
        (work/"impact.json").write_text(json.dumps({"shots":{shot:{"target":{"sha256":"b"*64}}}}))
        pending={"effect":"video","arguments":{"shot_id":shot,"repair_id":repair_id}}
        journal=work/"autonomy.json"
        journal.write_text(json.dumps({"provider_blockers":{shot:{"execution_key":key,"manifest_paths":[str(path)],"pending_tool":pending}}}))
        evidence={"shot_id":shot,"provider":"volcengine-ark","model":"test-model","http_status":400,"provider_request_id":"vendor-id",
                  "provider_error":{"code":"test-rejection","message":"private response"},"manifest_sha256":"f"*64,"execution_key":key,
                  "references":[{**pin,"payload_path":"content[1]"}],"reason":"private response"}
        with patch("pianchang.video_agent.submission_rejection",return_value=evidence) as verify:
            shown=current_commission(self.service,self.pid)
            row,=shown["submission_rejections"]
            self.assertEqual(row,{"shot_id":shot,"provider":"volcengine-ark","model":"test-model","http_status":400,
                                  "code":"test-rejection","provider_request_id":"vendor-id",
                                  "references":[{"revision_id":"r1","purpose":"portrait","subject_id":"actor"}]})
            self.assertNotIn("unverified",shown["last_execution"]["message"])
            # A new-style blocker points to the same signed manifest; polling reuses that verification.
            journal.write_text(json.dumps({"provider_blockers":{shot:{"kind":"submission_rejected","repair_id":repair_id,"execution_key":key,
                                     "manifest_path":str(path),"manifest_sha256":evidence["manifest_sha256"]}}}))
            self.assertEqual(current_commission(self.service,self.pid)["submission_rejections"],[row])
            verify.assert_called_once()
            active={"id":"2"*32,"kind":"pi_video","project_id":self.pid,"status":"queued","_pi_continue_commission":self.cid}
            self.service.state["jobs"].append(active)
            self.assertEqual(current_commission(self.service,self.pid)["submission_rejections"],[])
            self.service.state["jobs"].remove(active)
            stopped["worker_trace_id"]="7"*32
            self.assertEqual(current_commission(self.service,self.pid)["submission_rejections"],[])
            stopped["worker_trace_id"]=trace_id
            path.write_text(json.dumps({"metadata":{**meta,"execution_key":"other-order"}}))
            self.assertEqual(current_commission(self.service,self.pid)["submission_rejections"],[])
            verify.assert_called_once()

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
        resumed = {"id":"c"*32,"project_id":self.pid,"kind":"pi_video","status":"running",
                   "commission_id":self.cid,"commission_job_id":self.source["id"]}
        self.service.state["jobs"].append(resumed)
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
        self.assertEqual([job["id"] for job in detail["jobs"]], [resumed["id"],self.source["id"]])
        self.assertEqual(detail["jobs"][0]["commission_job_id"],self.source["id"])
        job = detail["jobs"][1]
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

    def test_resume_reports_current_validation_error_with_old_stopped_checkpoint(self):
        from pianchang.studio_jobs import resume_video_job
        self.path.write_text(json.dumps({"status": "needs_input", "result": {"summary": "old provider failure"}}), encoding="utf-8")
        worker = Mock(side_effect=ValueError("current receipt mismatch"))
        session = SimpleNamespace(folder=self.path.parents[2], store=self.store, worker=worker)
        with patch("pianchang.studio_jobs._session", return_value=session):
            with self.assertRaisesRegex(ValueError, "current receipt mismatch"):
                resume_video_job(self.service, self.source, explicit_continuation=True)

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
        def resumed(service,previous,*,explicit_continuation=False):
            self.assertTrue(explicit_continuation)
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

    def _button_job(self):
        job = {"id":"f"*32, "kind":"pi_video", "status":"running", "project_id":self.pid,
               "_pi_mode":"work", "_pi_message_id":"8"*32, "_pi_continue_commission":self.cid}
        self.service.state["jobs"].append(job)
        self.sessions.repo.append(self.session["id"], "user", "继续当前制作委托", message_id=job["_pi_message_id"],
                                  job_id=job["id"], status="dispatching", continue_commission=self.cid)
        self.sessions.repo.update(self.session["id"], active_job=job["id"])
        return job

    def test_button_resumes_original_worker_despite_historical_arrearage(self):
        self.source["error"] = "HTTP 400 Arrearage (historical)"
        self.service.executor = Mock()
        original = self.path.read_bytes()
        def resume(service, previous, *, explicit_continuation):
            self.assertIs(service, self.service)
            self.assertIs(previous, self.source)
            self.assertTrue(explicit_continuation)
            options = BINDING.get()("video")
            self.assertIn(self.cid, options["initial_message"])
            self.assertIn(self.session["id"], options["execution_lock"])
            return {"task_complete":False, "action_required":True, "summary":"offline actual worker result"}
        with patch("pi_bridge.service.PiLoop", side_effect=AssertionError("button must not ask the chat model")), \
             patch("pi_bridge.diagnostics.production_diagnostics", return_value={"preflight_ready":True,
                   "jobs":[{"error":self.source["error"]}]}), \
             patch.object(self.sessions.executor, "submit") as dispatch, \
             patch("pianchang.studio_jobs.resume_video_job", side_effect=resume) as worker:
            response = self.sessions.submit({"project":self.pid}, "video", "继续当前制作委托", "8"*32,
                                            mode="work", continue_commission=self.cid)
            bound, session, job, message = dispatch.call_args.args
            bound(session, job, message)
        worker.assert_called_once()
        self.assertEqual(response["job"]["id"], job["id"])
        self.assertEqual(job["result"]["summary"], "offline actual worker result")
        self.assertFalse(job["result"]["task_complete"])
        history = self.sessions.repo.messages(self.session["id"])
        self.assertEqual(history[-1]["text"], "offline actual worker result")
        receipt = next(m for m in history if m["id"] == job["_pi_message_id"])
        self.assertEqual(receipt["status"], "delivered")
        self.assertEqual(receipt["continue_commission"], self.cid)
        self.assertEqual(job["commission_id"], self.cid)
        self.assertEqual(self.path.read_bytes(), original)

    def test_button_checks_scope_preflight_cancellation_and_current_worker_failure(self):
        job = self._button_job()
        with patch("pi_bridge.service.PiLoop", side_effect=AssertionError("no chat request")), \
             patch("pi_bridge.diagnostics.production_diagnostics", return_value={"preflight_ready":False}) as diagnostics, \
             patch("pianchang.studio_jobs.resume_video_job", side_effect=ValueError("current media receipt mismatch")) as worker:
            job["_pi_mode"] = "inspect"
            with self.assertRaisesRegex(ValueError, "继续指令"):
                self.sessions.run_video(self.session, job, "offline")
            job.update(_pi_mode="work", _pi_continue_commission="9"*32)
            with self.assertRaisesRegex(ValueError, "委托已改变"):
                self.sessions.run_video(self.session, job, "offline")
            job["_pi_continue_commission"] = self.cid
            with self.assertRaisesRegex(ValueError, "模型能力校验"):
                self.sessions.run_video(self.session, job, "offline")
            diagnostics.return_value = {"preflight_ready":True}
            job["cancel_requested"] = True
            with self.assertRaisesRegex(ValueError, "已取消"):
                self.sessions.run_video(self.session, job, "offline")
            worker.assert_not_called()
            job["cancel_requested"] = False
            result = self.sessions.run_video(self.session, job, "offline")
        self.assertEqual(result["continuation_error"], "current media receipt mismatch")
        self.assertFalse(result["task_complete"])
        worker.assert_called_once()

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
