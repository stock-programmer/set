import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import workbench_loop as loop
from trace_system import get_tracer, use_context

J1, J2 = "1"*32, "2"*32


class DriverTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.enterContext(patch.object(loop,"RUNS",self.root/"runs"))
        self.enterContext(patch.object(loop,"WEB",self.root/"web"))
        loop.WEB.mkdir()
        self.enterContext(use_context(None,tracer=get_tracer(self.root/"trace")))
        self.enterContext(get_tracer().trace("test.loop.offline"))
        (loop.WEB/"server-process.json").write_text('{"pid":1}',encoding="utf8")
        self.state={"id":"a"*32,"phase":"submit","next_action":"start","role":"script",
                    "submitted":[],"history":[],"repairs":0,"retry_count":0,"max_repairs":2}

    def test_repair_restarts_then_continues_same_story(self):
        actions=[]
        def page(state,action):
            actions.append((action,state.get("story_id")))
            return {"ok":True,"job_id":J1 if action=="start" else J2}
        self.enterContext(patch.object(loop,"page",page))
        self.enterContext(patch.object(loop,"jobs",return_value=[{"id":j,"status":"interrupted"} for j in (J1,J2)]))
        self.enterContext(patch("tools.workbench_loop_observer.observe_job",side_effect=[
            {"status":"repairable","reason":"local_tool_exception","fault":{"error_type":"KeyError"}},
            {"status":"succeeded","reason":"script_and_spec_published"}]))
        repair=self.enterContext(patch("tools.workbench_loop_repair.repair_fault",return_value={"status":"applied"}))
        restart=self.enterContext(patch.object(loop,"restart",return_value=True))
        self.assertEqual(loop.run(self.state),"succeeded")
        self.assertEqual(actions,[("start",None),("continue",J1),("observe",J1)])
        repair.assert_called_once()
        restart.assert_called_once()
        self.assertEqual(self.state["submitted"],[J1,J2])

    def test_unknown_response_never_submits_again(self):
        self.state.update(phase="submitting",attempt="3"*32)
        with patch.object(loop,"page",side_effect=AssertionError("duplicate submission")):
            self.assertEqual(loop.run(self.state),"unknown")

    def test_failed_offline_candidate_rechecks_with_receipt_before_restart(self):
        self.state.update(phase="repair",fault={"error_type":"KeyError"},after_restart="verify_page")
        failure={"file":"pianchang/example.py","after_sha256":"f"*64,
                 "checks":{"suite":"tests.test_example","failed_tests":["test_restore"],"lines":[14]}}
        outcomes=[{"status":"blocked","reason":"isolated_regression_failed","validation":failure},
                  {"status":"applied","files":["pianchang/example.py"]}]
        with patch("tools.workbench_loop_repair.repair_fault",side_effect=outcomes) as repair, \
                patch.object(loop,"restart",return_value=True) as restart, \
                patch.object(loop,"page",return_value={"ok":True}):
            self.assertEqual(loop.run(self.state),"succeeded")
            self.assertEqual(repair.call_args_list[0].kwargs["previous"],[])
            self.assertEqual(repair.call_args_list[1].kwargs["previous"],[failure])
            restart.assert_called_once()
            self.assertEqual(self.state["submitted"],[])

    def test_new_failed_checks_keep_original_repair_open(self):
        self.state.update(phase="repair",fault={"error_type":"ValueError"},
                          after_restart="verify_page",max_repairs=None)
        failures=[{"file":"pianchang/example.py","after_sha256":str(i)*64,
                   "checks":{"failed_tests":[name]}}
                  for i,name in enumerate(("test_first","test_first","test_next","test_last"))]
        outcomes=[{"status":"blocked","reason":"isolated_regression_failed","validation":failure}
                  for failure in failures]+[{"status":"applied","files":["pianchang/example.py"]}]
        with patch("tools.workbench_loop_repair.repair_fault",side_effect=outcomes) as repair, \
                patch.object(loop,"restart",return_value=True),patch.object(loop,"page",return_value={"ok":True}):
            self.assertEqual(loop.run(self.state),"succeeded")
        self.assertEqual(repair.call_args.kwargs["previous"],failures)
        self.assertEqual(self.state["submitted"],[])

    def test_four_distinct_rejections_then_repair_and_verify_original_business(self):
        self.state.update(phase="repair",fault={"error_type":"ValueError"},after_restart="verify_page",max_repairs=None)
        rejected=[{"file":"pianchang/example.py","after_sha256":str(i)*64,
                   "checks":{"failed_tests":["test_restore"]}} for i in range(4)]
        fault={"code":"page_exception","error_type":"ValueError","frames":[{"file":"web/studio.js","line":3}]}
        outcomes=[{"status":"blocked","reason":"isolated_regression_failed","validation":item} for item in rejected]
        outcomes.extend([{"status":"applied","files":["pianchang/example.py"]},{"status":"applied","files":["web/studio.js"]}])
        with patch("tools.workbench_loop_repair.repair_fault",side_effect=outcomes) as repair, \
                patch.object(loop,"restart",return_value=True) as restart, \
                patch.object(loop,"page",side_effect=[{"ok":False,"fault":fault},{"ok":True}]):
            self.assertEqual(loop.run(self.state),"succeeded")
        self.assertEqual(repair.call_args_list[4].kwargs["previous"],rejected)
        self.assertEqual(repair.call_args_list[-1].args[1],fault)
        self.assertEqual(restart.call_count,2)
        self.assertEqual(self.state["submitted"],[])

    def test_missing_proposal_waits_without_holding_driver_then_rechecks(self):
        self.state.update(phase="repair",fault={"error_type":"KeyError"},after_restart="verify_page",watch_managed=True)
        outcomes=[{"status":"blocked","reason":"no_applicable_repair"},{"status":"applied","files":["pianchang/example.py"]}]
        with patch("tools.workbench_loop_repair.repair_fault",side_effect=outcomes) as repair, \
                patch.object(loop,"restart",return_value=True),patch.object(loop,"page",return_value={"ok":True}):
            self.assertEqual(loop.run(self.state),"repair")
            self.assertEqual(loop.run(self.state),"repair")
            self.assertEqual(repair.call_count,1)
            self.state["repair_not_before"]=0
            self.assertEqual(loop.run(self.state),"succeeded")
            self.assertEqual(repair.call_args.kwargs["previous"],[{"reason":"no_applicable_repair"}])

    def test_old_retry_migration_only_reopens_known_checkpoint(self):
        fault={"error_type":"KeyError"}
        state={"phase":"blocked","reason":"same_fault_after_three_repairs","fault":fault,
               "last_fault":json.dumps(fault,sort_keys=True),"history":[{"phase":"repair"},
               {"phase":"blocked","reason":"same_fault_after_three_repairs"}]}
        self.assertEqual(loop.legacy_retry_phase(state),"repair")
        state["history"][0]["phase"]="submitting"
        self.assertIsNone(loop.legacy_retry_phase(state))
        self.assertIsNone(loop.legacy_retry_phase({**state,"reason":"repair_limit"}))

    def test_cleared_dependency_schedules_only_the_proven_commission_once(self):
        cid="c"*32
        job={"id":J1,"kind":"studio_video","status":"failed","project_id":"a"*16,"message_id":cid}
        evidence={"status":"cleared","reason":"frozen_commission_ready","safe_to_continue":True,
                  "receipt_unknown":False,"commission_id":cid,"continuation_checkpoint":"d"*64}
        self.state.update(phase="watch",role="video",job_id=J1)
        self.enterContext(patch.object(loop,"jobs",return_value=[job]))
        self.enterContext(patch("tools.workbench_loop_dependency.recheck_dependency",return_value=evidence))
        loop.watch(self.state)
        self.assertEqual((self.state["phase"],self.state["next_action"],self.state["commission_id"]),("submit","continue",cid))
        loop.watch(self.state)
        self.assertEqual(self.state["phase"],"blocked")
        self.state.update(last_continuation_checkpoint=None,commission_id="e"*32)
        loop.watch(self.state)
        self.assertEqual(self.state["phase"],"blocked")

    def test_recovers_native_creation_receipt_without_resubmit(self):
        attempt="4"*32
        self.state.update(phase="submitting",attempt=attempt,idea="archived idea")
        directory=loop.RUNS/self.state["id"]
        directory.mkdir(parents=True)
        (directory/f"intent-{attempt}.json").write_text(json.dumps(
            {"action":"start","role":"script","idempotency_key":"browser-key"}),encoding="utf8")
        job={"id":J1,"kind":"studio_script","_idempotency_key":"browser-key","story_input":{"idea":"archived idea"}}
        with patch.object(loop,"jobs",return_value=[job]):
            loop.reconcile(self.state)
        self.assertEqual(self.state["phase"],"watch")
        self.assertEqual(self.state["story_id"],J1)
