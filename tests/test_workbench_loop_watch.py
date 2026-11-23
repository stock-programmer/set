"""Offline persistent discovery tests. No browser, Codex or provider subprocess is invoked."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools import workbench_loop_watch as watch
from tools import workbench_loop_triage as real_triage
from trace_system import get_tracer, use_context

OLD = "1"*32
NEW = "2"*32
ASSET = "3"*32
PROJECT = "a"*16


def job(jid, kind="pi_script", status="failed"):
    return {"id":jid, "kind":kind, "status":status, "trace_id":jid,
            "error":"private story contents", "project_id":PROJECT}


class WatcherTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root/"outputs/web").mkdir(parents=True)
        tr = get_tracer(self.root/"traces")
        self.enterContext(use_context(None,tracer=tr))
        self.enterContext(tr.trace("test.watch.offline"))
        self.dependency = Mock(return_value=None)
        self.triage = Mock(return_value={"status":"reported","finding":"receipt_unknown"})
        self.enterContext(patch.dict(sys.modules,{
            "tools.workbench_loop_dependency":SimpleNamespace(recheck_dependency=self.dependency),
            "tools.workbench_loop_triage":SimpleNamespace(triage_failure=self.triage)}))
        self.observe = self.enterContext(patch.object(watch,"observe_job",
            return_value={"status":"repairable","reason":"local_tool_exception","fault":{"error_type":"KeyError"}}))
        self.cli = self.enterContext(patch.object(watch.subprocess,"Popen"))
        self.cli.return_value.poll.return_value = None
        self.cli.return_value.pid = 234

    def jobs(self,*rows):
        (self.root/"outputs/web/state.json").write_text(json.dumps({"jobs":rows}),encoding="utf8")

    def daemon(self):
        daemon=watch.Watcher(self.root)
        self.addCleanup(daemon.close)
        return daemon

    def test_partial_execution_routes_only_the_current_order_to_verified_action(self):
        from tools.workbench_loop_input import partial_video_execution, partial_video_question
        from pianchang.video_execution import _stopped_budget
        from pianchang.story_spec import digest_json
        source = {**job(OLD,"studio_video"),"run_id":"6"*32,"message_id":"7"*32}
        child = {**job(NEW,"pi_video","succeeded"),"commission_id":source["message_id"],"commission_job_id":OLD,
                 "result":{"action_required":True,"task_complete":False}}
        jobs = {OLD:source,NEW:child}
        self.assertEqual(partial_video_execution(source,jobs),(source,child))
        self.assertIsNone(partial_video_execution(source,{**jobs, ASSET:{**source,"id":ASSET}}))
        self.assertTrue(watch._needs_attention(child))
        self.assertFalse(watch._needs_attention({**child,"result":{"turn_complete":True,"task_complete":False}}))
        budget = {"kind":"model","used":3,"limit":3,"message":"模型调用已用完（3/3）。"}
        record = {"status":"needs_input","limits":{"max_steps":3,"max_video_generations":1},"model_limit_reached":{"used":3,"limit":3},
                  "result":{"status":"needs_input","reason":"pi_stopped","task_complete":False}}
        limits = {"max_model_calls":3,"max_video_generations":1}
        plan = {"execution_limits":limits}
        usage = {"model_calls":3,"reserved_video_generations":0}
        current = {"commission_id":source["message_id"],"job_id":OLD,"last_execution":{"id":NEW},
                   "spec_ref":{"revision_id":"r1"},"script_ref":{"revision_id":"r1"},"plan":plan,
                   "limits":{**limits,"source":"frozen_commission"},"usage":usage}
        self.assertEqual(_stopped_budget(self.root,record,plan,current)["kind"],"model")
        self.assertIsNone(_stopped_budget(self.root,{**record,"pending_tool":{"tool":"generate"}},plan,current))
        self.assertIsNone(_stopped_budget(self.root,{**record,"model_limit_reached":None},plan,current))
        view = {"spec":current["spec_ref"],"script_ref":current["script_ref"],"production_ref":None,"project":{},
                "store_path":str(self.root/"assets.sqlite3"),"run_id":source["run_id"]}
        self.enterContext(patch("pianchang.studio_assets.studio_project_for_video",return_value=view))
        self.enterContext(patch("pianchang.video_execution.current_commission",return_value=current))
        review = self.enterContext(patch("pianchang.shot_approval.shot_review_panel",return_value={"shots":[]}))
        new_plan = {"plan_id":"8"*64,"continuation":{"request_id":source["message_id"]}}
        next_plan = self.enterContext(patch("pianchang.studio_jobs.studio_video_plan",return_value=new_plan))
        decision = lambda: partial_video_question(self.root,source,child,jobs)
        self.assertIsNone(decision())  # Generic partial + absent delivery is no spending decision.
        review.assert_not_called()
        current["exhausted_budget"] = budget
        self.assertEqual(decision()["plan_id"],new_plan["plan_id"])
        new_plan["unresolved_shots"] = ["S002"]
        self.assertIsNone(decision())
        review.return_value = {"shots":[{"can_approve":True,"shot_id":"S001","media_sha256":"1"*64,"report_sha256":"2"*64}]}
        next_plan.reset_mock()
        request = decision()
        self.assertEqual((request["kind"],request["commission_id"],request["review_shots"][0]["shot_id"]),
                         ("review_commission_shots",source["message_id"],"S001"))
        next_plan.assert_not_called()
        review.return_value = {"shots":[]}
        del current["exhausted_budget"]
        child["result"]["loop"] = {"reason":"retake_approval_required"}
        checkpoint = {"request_id":source["message_id"],"project":view["project"],"shooting_plan":plan}
        folder = self.root/"runs"/source["run_id"]/("video-spec-"+source["message_id"])
        folder.mkdir(parents=True)
        (folder/"partial.json").write_text(json.dumps(checkpoint),encoding="utf8")
        next_plan.return_value = {"plan_id":"8"*64,"extra_retake":{"proposal_id":"9"*64,"shot_ids":["S002"],
            "partial":{"request_id":source["message_id"],"sha256":digest_json(checkpoint)}}}
        request = decision()
        self.assertEqual(request["proposal_id"],"9"*64)
        (folder/"partial.json").write_text("{}",encoding="utf8")
        self.assertIsNone(decision())
        self.jobs(source,child)
        watcher = self.daemon()
        watcher.register(child,include=True)
        with patch("tools.workbench_loop_input.partial_video_question",return_value=request):
            watcher.tick()
            watcher.tick()
        self.assertEqual(watch.watch_status(self.root,OLD)["human_request"]["id"],request["id"])
        self.assertNotIn("human_request",watch.watch_status(self.root,NEW))
        readonly = {"job_id":OLD,"shots":[{"shot_id":"S002","media_sha256":"e"*64,"report_sha256":"f"*64,"can_approve":False}]}
        review.return_value = readonly
        child["result"].pop("loop",None)
        with patch("tools.workbench_loop_observer.acceptance_retake_stopped",return_value=True):
            navigation = decision()
        self.assertEqual(navigation["kind"],"inspect_commission_reports")
        self.assertEqual(navigation["review_shots"][0]["report_sha256"],"f"*64)
        watcher.next_check.clear()
        with patch("tools.workbench_loop_input.partial_video_question",return_value=navigation):
            watcher.tick()
        feedback = watch.watch_status(self.root,OLD)
        self.assertEqual(feedback["status"],"awaiting_evidence")
        self.assertEqual(feedback["review_link"]["id"],navigation["id"])
        self.assertNotIn("human_request",feedback)
        review_job = {**job("5"*32,"studio_review","queued"),"review_request_id":source["message_id"]}
        self.jobs(source,child,review_job)
        with patch("tools.workbench_loop_input.partial_video_question",return_value=request) as lookup:
            watcher.tick()
            lookup.assert_not_called()
            in_review = watch.watch_status(self.root,OLD)
            self.assertEqual(in_review["reason"],"commission_review_active")
            self.assertNotIn("review_link",in_review)
            self.assertNotIn("human_request",in_review)
            self.jobs(source,child,{**review_job,"status":"succeeded"})
            watcher.tick()
            self.assertEqual(watch.watch_status(self.root,OLD)["human_request"]["id"],request["id"])
        self.cli.assert_not_called()

    def test_initial_baseline_restart_and_coordinated_driver(self):
        old=job(OLD)
        active=job(NEW,status="running")
        self.jobs(old,active)
        first=self.daemon()
        first.tick()
        self.assertEqual(first.state["events"],{})
        self.jobs(old,job(NEW))
        resumed=self.daemon()
        with watch._lock(resumed.runs/"driver.lock"):
            resumed.tick()
            self.cli.assert_not_called()
        resumed.tick()
        resumed.tick()
        self.cli.assert_called_once()
        status=watch.watch_status(self.root,NEW)
        run_id=status["run_id"]
        self.assertIn(run_id,self.cli.call_args.args[0])
        self.assertNotIn("private story",json.dumps(resumed.state))
        owner=resumed.runs/run_id/"run.json"
        owner.parent.mkdir()
        owner.write_text(json.dumps({"id":run_id,"phase":"succeeded","job_id":NEW,
                                    "submitted":[NEW],"reason":"page_verified"}),encoding="utf8")
        restarted=self.daemon()
        restarted.tick()
        self.assertEqual(watch.watch_status(self.root,NEW)["status"],"succeeded")
        self.cli.assert_called_once()
        self.assertEqual(watch.watch_status(self.root,OLD)["status"],"historical")

    def test_preexisting_outbox_survives_first_start_and_legacy_baseline(self):
        old, failed = job(OLD), job(NEW)
        index = {"jobs":[old,failed]}
        watch.record_failure_transition(index,failed)
        key = watch._signature(failed)
        watch.write_json(self.root/"outputs/web/state.json",index)
        with watch._lock(watch._folder(self.root).parent/"driver.lock"):
            for migrated in (False,True):
                with self.subTest(migrated=migrated):
                    if migrated:
                        watch.write_json(watch._folder(self.root)/"state.json",{
                            "version":1,"baseline":{OLD:watch._signature(old),NEW:key},
                            "historical_stamps":{key:None},"events":{},"latest":{}})
                    daemon = self.daemon()
                    daemon.tick()
                    self.assertEqual(list(daemon.state["events"]),[key])
                    self.assertEqual(daemon.state["baseline"],{OLD:watch._signature(old)})
                    self.assertEqual(daemon.state["latest"],{NEW:key})
                    resumed = self.daemon()
                    with patch.object(resumed,"_register",wraps=resumed._register) as register:
                        resumed.tick()
                        # Snapshot polling can check an existing fingerprint again; the outbox is consumed once.
                        self.assertFalse(any(c.kwargs.get("source") for c in register.call_args_list))
                    self.assertEqual(list(resumed.state["events"]),[key])
        self.cli.assert_not_called()

    def test_stopped_second_review_waits_for_operator_without_repair_or_continue(self):
        failed = dict(job(NEW,kind="studio_video"),run_id=OLD,message_id=ASSET)
        self.jobs({**failed,"status":"running"})
        daemon = self.daemon()
        daemon.tick()
        checkpoint = self.root/"outputs/collaborations/runs"/OLD/("video-spec-" + ASSET)/"agent/run.json"
        watch.write_json(checkpoint,{"result":{"reason":"acceptance_retake_limit","summary":"S001 复验仍未通过"}})
        self.jobs(failed)
        daemon.tick()
        self.assertEqual(watch.watch_status(self.root,NEW)["reason"],"acceptance_retake_limit")
        self.dependency.assert_not_called()
        self.triage.assert_not_called()
        self.cli.assert_not_called()

    def test_recovered_job_triage_uses_only_original_trace_exception(self):
        tracer = get_tracer(self.root/"outputs/traces")
        failed = job(NEW)
        module = self.root/"pianchang/sample.py"
        module.parent.mkdir()
        module.touch()
        with tracer.trace("agent.task.pi_script") as task:
            failed["trace_id"] = task.trace_id
            def exception(name,line):
                task.event("task.exception",{"type":name,"message":"private story contents",
                    "stack":f'  File "{module}", line {line}, in test\n    private story contents'},kind="error")
            def failure():
                return task.event("task.transition_requested",
                    {"job_id":NEW,"changes":{"status":"failed"}},kind="state")
            exception("KeyError",17)
            boundary = failure()
            index = {"jobs":[job(NEW,status="running")]}
            watch.record_failure_transition(index,failed,transition=boundary)
            # An old stamp with a unique matching transition can still retrieve its original evidence.
            self.assertEqual(real_triage.original_failure_evidence(self.root,failed)["error_type"],"KeyError")
            exception("IndexError",29)
            failure()
            self.assertEqual(real_triage.original_failure_evidence(self.root,failed)["code"],"original_evidence_unavailable")
        watch.write_json(self.root/"outputs/web/state.json",index)
        self.triage.side_effect = real_triage.triage_failure
        packets = []
        def fake_codex(argv,*,input,**kwargs):
            packets.append(json.loads(input.decode().splitlines()[-1]))
            Path(argv[argv.index("--output-last-message")+1]).write_text(
                json.dumps({"finding":"local_code_failure","next_check":"inspect_local_regression"}),encoding="utf8")
            return SimpleNamespace(returncode=0)
        with patch.object(real_triage.shutil,"which",return_value="codex"), patch.object(real_triage.subprocess,"run",side_effect=fake_codex):
            daemon = self.daemon()
            daemon.tick()
            daemon.triage_future.result(timeout=60)
            daemon.tick()
        self.assertEqual(len(packets),1)
        self.assertEqual((packets[0]["error_type"],packets[0]["code_file"],packets[0]["code_line"]),
                         ("KeyError","pianchang/sample.py",17))
        self.assertNotIn("private story",json.dumps(packets))
        self.assertEqual(watch.watch_status(self.root,NEW)["status"],"watching")
        self.cli.assert_not_called()

    def test_include_links_upstream_and_diagnoses_once(self):
        video=job(OLD,"studio_video")
        asset=job(ASSET,"asset_production")
        self.jobs(video,asset)
        daemon=self.daemon()
        watch._include(self.root,OLD)
        def classify(root,j):
            return {"status":"awaiting_evidence","reason":"missing_selected_voice",
                    "related_job_id":ASSET,"receipt_unknown":True} if j["id"]==OLD else None
        self.dependency.side_effect=classify
        daemon.tick()
        for _ in range(3):
            if daemon.triage_future:
                daemon.triage_future.result(timeout=30)
            daemon.tick()
        self.assertEqual(self.triage.call_count,2)
        self.assertEqual(watch.watch_status(self.root,OLD)["triage"]["status"],"reported")
        self.assertEqual(watch.watch_status(self.root,ASSET)["status"],"awaiting_evidence")
        restarted=self.daemon()
        restarted.tick()
        self.assertEqual(self.triage.call_count,2)
        self.cli.assert_not_called()
        self.dependency.side_effect=lambda root,j: {"status":"cleared","reason":"reference_selected"} if j["id"]==OLD else None
        restarted.next_check.clear()
        restarted.tick()
        self.assertEqual(watch.watch_status(self.root,OLD)["status"],"cleared")
        self.cli.assert_not_called()

    def test_existing_browser_job_and_uncertain_triage_are_not_replayed(self):
        self.jobs()
        daemon=self.daemon()
        known=job(NEW)
        self.jobs(known)
        run=daemon.runs/("c"*32)
        run.mkdir()
        (run/"run.json").write_text(json.dumps({"id":run.name,"phase":"watch","submitted":[NEW]}),encoding="utf8")
        with watch._lock(daemon.runs/"driver.lock"):
            daemon.tick()
        self.assertEqual(watch.watch_status(self.root,NEW)["run_id"],run.name)
        self.cli.assert_not_called()
        # Simulated loss of an in-flight read-only diagnostic response.
        (run/"run.json").write_text(json.dumps({"id":run.name,"phase":"unknown","submitted":[NEW]}),encoding="utf8")
        record=daemon.state["events"][daemon.state["latest"][NEW]]
        record["triage"]={"status":"running"}
        daemon.save()
        restarted=self.daemon()
        restarted.tick()
        self.assertEqual(watch.watch_status(self.root,NEW)["triage"]["status"],"unknown")
        self.triage.assert_not_called()

    def test_unowned_agent_rechecks_original_pending_receipt_after_diagnosis(self):
        from tools.workbench_loop_observer import observe_job
        self.observe.side_effect = observe_job
        self.triage.return_value = {"status": "reported", "finding": "safe_to_continue"}
        self.jobs()
        watcher = self.daemon()
        failed = job(NEW)
        tracer = get_tracer(self.root / "outputs/traces")
        with use_context(None, tracer=tracer), tracer.trace("test.original.script_worker") as task:
            failed["trace_id"] = task.trace_id
            task.event("http.request", {"method": "POST"}, kind="http")
            self.jobs(failed)
            watcher.tick()
            self.assertEqual(watch.watch_status(self.root, NEW)["reason"], "provider_receipt_unknown")
            watcher.triage_future.result(timeout=60)
            watcher.tick()
            watcher.next_check.clear()
            watcher.tick()
            count = self.observe.call_count
            self.assertGreaterEqual(count, 2)
            watcher.tick()
            self.assertEqual(self.observe.call_count, count)
            self.triage.assert_called_once()
            self.cli.assert_not_called()
            task.event("http.response", {"http_status": 429}, kind="http")
            watcher.next_check.clear()
            watcher.tick()
            watcher.tick()
            self.cli.assert_called_once()
            result = watch.watch_status(self.root, NEW)
            self.assertEqual(result["status"], "handling")
            self.assertEqual(self.cli.call_args.args[0][-4:], ["--job-id", NEW, "--run-id", result["run_id"]])
            self.triage.assert_called_once()

    def test_unowned_recheck_preserves_cancel_trace_and_previous_driver_limits(self):
        self.jobs()
        watcher = self.daemon()
        source = job(NEW)
        watcher.register(source, include=True)
        key = watch._signature(source)
        record = watcher.state["events"][key]
        base = {**record, "triage": {"status": "reported"}, "status": "blocked"}
        for fields, source_changes in [
            ({"reason": "repair_limit"}, {}), ({"reason": "trace_incomplete"}, {}),
            ({"run_id": "4" * 32, "status": "unknown"}, {}),
            ({}, {"cancel_requested": True}), ({}, {"trace_incomplete": True}),
            ({}, {"result": {"status": "budget_exhausted"}}),
        ]:
            with self.subTest(fields=fields, source_changes=source_changes):
                watcher.process(key, {**base, **fields}, {**source, **source_changes}, {NEW: source})
        self.observe.assert_not_called()
        self.triage.assert_not_called()
        self.cli.assert_not_called()

    def test_diagnosed_owner_reopens_same_driver_only_after_original_wire_is_reconciled(self):
        from tools.workbench_loop_observer import observe_job
        self.observe.side_effect = observe_job
        self.jobs()
        watcher = self.daemon()
        failed = job(NEW)
        tracer = get_tracer(self.root/"outputs/traces")
        module = self.root/"pianchang/story_agent.py"
        module.parent.mkdir()
        module.touch()
        with use_context(None,tracer=tracer), tracer.trace("test.original.worker") as task:
            failed["trace_id"] = task.trace_id
            task.event("http.request", {"method":"POST"}, kind="http")
            self.jobs(failed)
            prior = observe_job(self.root,failed)
            self.assertEqual(prior["reason"], "provider_receipt_unknown")
            state = {"id":OLD, "job_id":NEW, "phase":"unknown", "role":"script", "project_id":PROJECT,
                     "submitted":[NEW], "observation":prior, "reason":prior["reason"]}
            watch.write_json(watcher.runs/OLD/"run.json", state)
            watcher.tick()
            watcher.triage_future.result(timeout=60)
            watcher.tick()
            self.triage.assert_called_once()
            self.cli.assert_not_called()
            self.assertTrue(watch.passive_observation(state))
            for reason in ("repairing_receipt_lost","submission_receipt_unconfirmed","repair_limit","job_cancelled"):
                self.assertFalse(watch.passive_observation({**state,"reason":reason}))
            task.event("http.response", {"http_status":200}, kind="http")
            task.event("task.exception", {"type":"KeyError", "stack":f'  File "{module}", line 17, in write_draft'}, kind="error")
            watcher.next_check.clear()
            watcher.tick()
            watcher.tick()
            self.cli.assert_called_once()
            self.assertEqual(self.cli.call_args.args[0][-2:], ["resume", OLD])

    def test_planner_failure_goes_to_codex_while_original_commission_resumes(self):
        self.jobs()
        watcher=self.daemon()
        failed=dict(job(NEW,kind="studio_video"),message_id=ASSET)
        self.jobs(failed)
        evidence={"status":"cleared","reason":"planner_request_not_sent","commission_id":ASSET,
                  "receipt_unknown":False,"safe_to_continue":True,"continuation_checkpoint":"e"*64,
                  "transport_failure":{"request_kind":"model","dispatch":"not_sent","phase":"start_tls"}}
        self.dependency.return_value=evidence
        watcher.tick()
        watcher.triage_future.result(timeout=60)
        self.triage.assert_called_once_with(self.root,failed,evidence)
        self.cli.assert_called_once()
        watcher.tick()
        self.triage.assert_called_once()
        self.cli.assert_called_once()

    def test_legacy_retry_keeps_original_driver_and_respects_durable_backoff(self):
        self.jobs()
        watcher=self.daemon()
        failed=job(NEW)
        self.jobs(failed)
        observation={"job_id":NEW,"trace_id":NEW,"status":"transient","reason":"provider_rate_limited","safe_to_continue":True}
        state={"id":OLD,"job_id":NEW,"submitted":[NEW],"project_id":PROJECT,"observation":observation,"phase":"blocked",
               "reason":"repeated_interruption","history":[{"phase":"watch"},{"phase":"blocked",
               "reason":"repeated_interruption","observation":observation}]}
        for changes in ({"trace_incomplete":True},{"cancel_requested":True},{"project_id":"b"*16},{"trace_id":OLD}):
            self.assertFalse(watch._legacy_retry(state,{**failed,**changes}))
        path=watcher.runs/OLD/"run.json"
        watch.write_json(path,state)
        watcher.tick()
        self.assertEqual(self.cli.call_args.args[0][-2:],["resume",OLD])
        self.cli.reset_mock()
        watch.write_json(path,{**state,"phase":"repair","repair_not_before":watch.time.time()+3600})
        reboot=self.daemon()
        reboot.tick()
        self.cli.assert_not_called()
        self.assertEqual(watch.watch_status(self.root,NEW)["driver_phase"],"repair")

    def test_new_unresolved_evidence_goes_back_to_codex_without_authorizing_submission(self):
        self.jobs()
        watcher=self.daemon()
        self.jobs(job(NEW))
        unknown={"job_id":NEW,"trace_id":NEW,"status":"unknown","reason":"provider_receipt_unknown"}
        self.observe.return_value=unknown
        self.triage.return_value={"status":"reported","finding":"local_code_failure","next_check":"resume_verified_commission"}
        watcher.tick()
        watcher.triage_future.result(timeout=60)
        watcher.tick()
        self.assertEqual(self.observe.call_count,2)
        self.triage.assert_called_once()
        self.observe.return_value={**unknown,"reason":"pending_operation"}
        watcher.next_check.clear()
        watcher.tick()
        watcher.triage_future.result(timeout=60)
        watcher.tick()
        self.assertEqual([c.args[2]["reason"] for c in self.triage.call_args_list],
                         ["provider_receipt_unknown","pending_operation"])
        self.cli.assert_not_called()

    def test_image_access_rejection_publishes_handoff_on_recheck_without_repeating_codex(self):
        self.jobs()
        daemon = self.daemon()
        self.jobs(job(ASSET, "asset_production"))
        previous = {"status":"awaiting_evidence","reason":"reference_selection_pending","receipt_unknown":False}
        self.dependency.return_value = previous
        daemon.tick()
        daemon.triage_future.result(timeout=30)
        daemon.tick()
        self.triage.assert_called_once()
        action = {"kind":"reference_image_model_access","items":[{"subject_id":"prop:cup","purpose":"prop"}]}
        evidence = {**previous,"status":"needs_input","reason":"reference_image_model_access","human_intervention":action}
        self.dependency.return_value = evidence
        self.daemon().tick()
        self.daemon().tick()
        current = watch.watch_status(self.root,ASSET)
        self.assertEqual(current["status"],"needs_input")
        self.assertEqual(current["dependency"]["human_intervention"], action)
        restarted = self.daemon()
        record = restarted.state["events"][restarted.state["latest"][ASSET]]
        record.update(triage={"status":"running"}, triage_evidence="old-diagnostic")
        restarted.save()
        restarted.tick()
        self.assertEqual(watch.watch_status(self.root, ASSET)["triage"]["status"], "unknown")
        self.triage.assert_called_once()
        self.cli.assert_not_called()

    def test_handoff_does_not_rebuild_the_baseline(self):
        self.jobs()
        daemon = self.daemon()
        watch.write_json(daemon.folder/"generation.json",{"generation":"server-new"})
        with patch.object(watch,"_spawn") as spawn:
            daemon.serve("server-old")
        spawn.assert_called_once_with(self.root,handoff=True)
        self.jobs(job(NEW))
        self.daemon().tick()
        self.cli.assert_called_once()

    def test_transient_failure_between_polls_is_delivered_once_after_recovery(self):
        self.jobs()
        daemon = self.daemon()
        failed = job(NEW)
        state = {"jobs":[failed]}
        watch.record_failure_transition(state,failed)
        watch.record_failure_transition(state,failed)
        state["jobs"] = [job(NEW,status="running")]
        self.assertNotIn("private story",json.dumps(state["feedback_failures"]))
        (self.root/"outputs/web/state.json").write_text(json.dumps(state),encoding="utf8")
        daemon.tick()
        daemon.triage_future.result(timeout=60)
        daemon.tick()
        self.assertEqual(watch.watch_status(self.root,NEW)["triage"]["status"],"reported")
        self.assertEqual(watch.watch_status(self.root,NEW)["status"],"watching")
        self.assertEqual(self.triage.call_args.args[1]["status"],"failed")
        self.daemon().tick()
        self.triage.assert_called_once()
        self.cli.assert_not_called()

    def test_new_receipt_enables_only_the_verified_original_commission(self):
        self.jobs()
        daemon = self.daemon()
        video = job(OLD,"studio_video")
        video["commission_id"]="9"*32
        self.jobs(video)
        self.dependency.return_value={"status":"blocked","reason":"needs_asset","receipt_unknown":True}
        daemon.tick()
        daemon.triage_future.result(timeout=60)
        daemon.tick()
        self.cli.assert_not_called()
        daemon.next_check.clear()
        self.dependency.return_value={"status":"cleared","reason":"reconciled","safe_to_continue":True,
                                      "source_revision":"r2","receipt_unknown":False}
        daemon.tick()
        daemon.tick()
        self.cli.assert_called_once()
        self.assertIn(OLD,self.cli.call_args.args[0])

    def test_original_video_shows_its_resumed_driver_through_delivery_verification(self):
        self.jobs()
        daemon=self.daemon()
        video=job(OLD,"studio_video")
        video["message_id"]="9"*32
        self.jobs(video)
        self.dependency.return_value={"status":"cleared","reason":"frozen_commission_ready","safe_to_continue":True,
                                      "receipt_unknown":False,"continuation_checkpoint":"c"*64}
        daemon.tick()
        run_id=watch.watch_status(self.root,OLD)["run_id"]
        driver={"id":run_id,"origin_job_id":OLD,"project_id":PROJECT,"role":"video","job_id":NEW,
                "submitted":[OLD,NEW],"phase":"watch","reason":"continue_accepted"}
        path=daemon.runs/run_id/"run.json"
        watch.write_json(path,driver)
        daemon.next_check.clear()
        self.dependency.return_value={"status":"blocked","reason":"frozen_commission_unavailable","receipt_unknown":False}
        daemon.tick()
        self.assertEqual(watch.watch_status(self.root,OLD)["status"],"handling")
        watch.write_json(path,{**driver,"phase":"succeeded","reason":"page_verified","observation":{"reason":"delivery_gate_passed"}})
        daemon.tick()
        self.assertEqual(watch.watch_status(self.root,OLD)["status"],"succeeded")
        self.assertIn("交付验收",watch.watch_status(self.root,OLD)["progress_message"])
        self.cli.assert_called_once()

    def test_original_voice_evidence_progresses_once_and_survives_restart(self):
        self.jobs()
        daemon=self.daemon()
        self.jobs(job(ASSET,"asset_production"))
        failure={"request_kind":"audio","error_type":"ConnectError","phase":"unknown","dispatch":"unknown"}
        evidence={"status":"awaiting_evidence","reason":"provider_receipt_unknown","receipt_unknown":True,
                  "provider_calls":[failure],"transport_failure":failure}
        self.dependency.side_effect=lambda *_: dict(evidence)
        recovery=Mock(return_value=NEW)
        probe=Mock(return_value={"status":"http_response","http_status":401,"trace_id":"a"*32})
        with patch.dict(sys.modules,{"tools.workbench_loop_voice":SimpleNamespace(request_voice_recovery=recovery),
                   "tools.workbench_loop_connectivity":SimpleNamespace(check_voice_route=probe)}):
            daemon.tick()
            daemon.triage_future.result(timeout=60)
            daemon.tick()
            self.triage.assert_called_once()
            recovery.assert_not_called()
            prior=self.triage.call_args.args[2]
            self.assertEqual(prior["transport_failure"]["dispatch"],"unknown")
            failure={**failure,"dispatch":"not_sent","phase":"start_tls"}
            evidence.update(reason="voice_request_not_sent",receipt_unknown=False,transport_failure=failure,provider_calls=[failure])
            daemon.next_check.clear()
            daemon.tick()
            daemon.triage_future.result(timeout=60)
            daemon.tick()
            self.daemon().tick()
            self.assertEqual(self.triage.call_count,2)
            recovery.assert_called_once()
            self.assertEqual(watch.watch_status(self.root,ASSET)["voice_recovery_job_id"],NEW)
            self.assertEqual(len(list((daemon.folder/"triage").glob("*.json"))),2)

    def test_planner_codex_receives_the_failed_exchange_and_original_commission(self):
        packets=[]
        def codex(argv,*,input,**kwargs):
            packets.append(json.loads(input.decode().splitlines()[-1]))
            Path(argv[argv.index("--output-last-message")+1]).write_text(json.dumps({
                "finding":"planner_connection_failed_before_send","next_check":"resume_verified_commission"}))
            return SimpleNamespace(returncode=0)
        evidence={"safe_to_continue":True,"receipt_unknown":False,"remaining_video_generations":3,
                  "transport_failure":{"request_kind":"model","trace_id":OLD,"span_id":NEW,"commission_id":ASSET,
                      "request_seq":1090,"error_seq":1113,"phase":"start_tls","error_type":"ConnectError",
                      "dispatch":"not_sent","proxy_host":"private-host","api_key":"private-key"},
                  "connectivity":{"status":"reachable","http_status":405,"url":"private-url"}}
        with patch.object(real_triage.shutil,"which",return_value="codex"),patch.object(real_triage.subprocess,"run",side_effect=codex):
            receipt=real_triage.triage_failure(self.root,{**job(NEW,"studio_video"), "message_id":ASSET},evidence)
        packet=packets[0]
        self.assertEqual(packet["original_planner_attempt"],{k:v for k,v in evidence["transport_failure"].items()
                                                            if k not in {"proxy_host","api_key"}})
        self.assertEqual(packet["current_planner_connection"],{"status":"reachable","http_status":405})
        self.assertEqual(packet["remaining_video_generations"],3)
        self.assertNotIn("private",json.dumps(packet))
        self.assertEqual((receipt["finding"],receipt["next_check"]),("planner_connection_failed_before_send","resume_verified_commission"))

    def test_voice_codex_receives_original_network_fault_separately_from_current_probe(self):
        packets=[]
        def codex(argv,*,input,**kwargs):
            packets.append(json.loads(input.decode().splitlines()[-1]))
            Path(argv[argv.index("--output-last-message")+1]).write_text(json.dumps({
                "finding":"voice_connection_failed_before_send","next_check":"check_original_voice_recovery"}))
            return SimpleNamespace(returncode=0)
        evidence={"transport_failure":{"request_kind":"audio","error_type":"ConnectError","dispatch":"not_sent",
                      "phase":"connect_or_tls","request_seq":406,"error_seq":408,"errno":2,"body":"private audio prompt"},
                  "connectivity":{"status":"http_response","http_status":401},"provider_calls":[{"token":"private token"}]}
        with patch.object(real_triage.shutil,"which",return_value="codex"),patch.object(real_triage.subprocess,"run",side_effect=codex):
            receipt=real_triage.triage_failure(self.root,job(ASSET,"asset_production"),evidence)
        self.assertEqual(receipt["status"],"reported")
        attempt=packets[0]["original_audio_attempt"]
        self.assertEqual((attempt["error_type"],attempt["dispatch"],attempt["phase"],attempt["error_seq"]),
                         ("ConnectError","not_sent","connect_or_tls",408))
        self.assertEqual(packets[0]["current_voice_connection"],evidence["connectivity"])
        self.assertNotIn("private",json.dumps(packets))

    def test_corrected_voice_observer_only_inspects_the_recorded_candidate_once(self):
        self.jobs()
        daemon=self.daemon()
        asset=job(ASSET,"asset_regenerate")
        self.jobs(asset)
        pin={"asset_id":"voice","revision_id":"r2","media_sha256":"d"*64}
        evidence={"status":"awaiting_evidence","reason":"voice_review_input_unavailable","provider_calls":[],
                  "voice_review":{"candidate":pin,"corrected_input_format":None}}
        self.dependency.side_effect=lambda *_: dict(evidence)
        inspect=Mock(return_value=NEW)
        with patch.dict(sys.modules,{"tools.workbench_loop_voice":SimpleNamespace(request_voice_inspection=inspect)}):
            daemon.tick()
            daemon.triage_future.result(timeout=60)
            daemon.tick()
            inspect.assert_not_called()
            evidence["voice_review"]={**evidence["voice_review"],"corrected_input_format":"voice-audition/input-audio-last/1"}
            daemon.next_check.clear()
            daemon.tick()
            daemon.triage_future.result(timeout=60)
            daemon.tick()
            self.daemon().tick()
        inspect.assert_called_once_with(self.root,asset,pin)
        self.assertEqual(watch.watch_status(self.root,ASSET)["voice_inspection_job_id"],NEW)

    def test_trace_pause_survives_restart_and_lock_probe_is_read_only(self):
        self.assertFalse(watch.watcher_alive(self.root))
        self.assertFalse(watch._folder(self.root).exists())
        with watch._lock(watch._folder(self.root)/"watch.lock"):
            self.assertTrue(watch.watcher_alive(self.root))
        watch.mark_trace_pause(self.root)
        marker=watch._folder(self.root)/"trace-paused.json"
        original=marker.read_bytes()
        watch.mark_trace_pause(self.root)
        self.assertEqual(marker.read_bytes(),original)
        self.assertEqual(watch.start_watcher(self.root)["reason"],"trace_incomplete")
        self.cli.assert_not_called()
