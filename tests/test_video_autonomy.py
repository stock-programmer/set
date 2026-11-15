"""Explicit offline simulations: no model/provider calls are used by these tests."""
import copy
import json
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pianchang.agent_loop import AgentLoop, Tool
from pianchang.bailian_video import VideoConfig
from pianchang.story_spec import digest_json
from pianchang.video_autonomy import VideoAutonomy, enqueue_recovery, recoverable_order
from pianchang.video_collab_agent import _write, _read
from pianchang.video_repair import acceptance_policy, verified_selection_plan
from trace_system import Tracer, use_context
from tests import test_video_repair as repair_fixtures
from tests import test_collab_negotiation as negotiation_fixtures
from tests.test_collab_negotiation import OfflineDecisions, call, FINISH


class AutonomyTests(unittest.TestCase):
    setUp=repair_fixtures.RepairTests.setUp
    report=repair_fixtures.RepairTests.report

    def configure(self):
        self.w.shooting_plan={"acceptance_loop":acceptance_policy(["S001","S002"]),
            "deferred_shots":[s["id"] for s in self.project["shots"] if s["id"] not in {"S001","S002"}]}
        self.w.autonomy=VideoAutonomy(self.w)
        return {"limits":{"max_video_generations":4},"video_generations":0,"repair_reservations":{},"steps":0}

    def test_unknown_and_uncertain_one_shot_do_not_stop_other_repairable_failures(self):
        record=self.configure()
        self.report("S001","UNKNOWN");self.report("S002","FAIL")
        self.assertFalse(self.w.stop_check(record)["allowed"])
        self.w.autonomy.blocked["S001"]={"reason":"offline lost provider receipt"}
        queue=self.w.autonomy.queue(record)
        self.assertEqual({r["shot_id"] for r in queue["actionable"]},{"S002"})
        self.assertFalse(self.w.stop_check(record)["allowed"])
        record["repair_reservations"]["S002"]=2
        stop=self.w.stop_check(record)
        self.assertTrue(stop["allowed"])
        self.assertTrue(stop["blocked"]);self.assertTrue(stop["exhausted"])

    def test_subtitle_can_use_local_edit_budget_after_video_repair_budget_is_exhausted(self):
        record=self.configure();self.report("S001","PASS");r=self.report("S002","PASS")
        r["results"].append({"criterion_id":"script_subtitle","required":True,"status":"FAIL",
            "reason":"离线模拟：全片已审看但未显示固定剧本字幕。","execution_status":"executed"})
        _write(self.w.reports["S002"]["report_path"],r);self.w.reports["S002"]["report_sha256"]=digest_json(r)
        record.update(video_generations=4,repair_reservations={"S002":2})
        queue=self.w.autonomy.queue(record)
        self.assertEqual([(row["shot_id"],row["repair_method"]) for row in queue["actionable"]],[("S002","edit_shot_asset")])
        self.assertFalse(self.w.stop_check(record)["allowed"])
        self.w.repair.local_edits["S002"]=2
        self.assertFalse(self.w.autonomy.queue(record)["actionable"])
        self.assertTrue(any(row["blocker_kind"]=="local_edit_limit" for row in self.w.autonomy.queue(record)["exhausted"]))

    def test_same_failed_local_edit_is_rejected_before_another_media_operation(self):
        self.configure();self.report("S001","FAIL")
        plan=self.w.repair.prepare("S001",["action_sequence"],"offline local black tail repair plan","offline actual evidence")
        lineage={"operation":{"name":"black_tail","start_s":4.0,"subtitle":""}}
        path=self.root/"prior-local-lineage.json";_write(path,lineage)
        self.w.repair.history=[{"shot_id":"S001","repair_id":"offline-prior-local","strategy":"local_black_tail",
            "source_clip":copy.deepcopy(self.w.shots["S001"]),"result_clip":{"media_lineage":{"path":str(path),"sha256":digest_json(lineage)}}}]
        with patch("pianchang.video_collab_agent._validated_media"),patch("pianchang.media_tools._probe") as probe,patch("pianchang.media_tools._run") as execute:
            with self.assertRaisesRegex(ValueError,"原样重复"):
                self.w.repair.edit("S001",plan["repair_id"],4.0,"")
        probe.assert_not_called();execute.assert_not_called()
        self.assertEqual(self.w.repair.local_edits,{})

    def test_literal_subtitle_preflight_rejects_omitted_characters_before_edit(self):
        self.configure();self.report("S001","FAIL")
        plan=self.w.repair.prepare("S001",["action_sequence"],"offline local subtitle repair plan","offline evidence")
        with patch("pianchang.acceptance.required_subtitle",return_value={"expected_text":"离线字幕。"}),patch("pianchang.media_tools._run") as execute:
            with self.assertRaisesRegex(ValueError,"不能省略"):
                self.w.repair.edit("S001",plan["repair_id"],4.0,"")
        execute.assert_not_called();self.assertFalse(self.w.repair.history)

    def test_approved_caption_terminal_punctuation_does_not_require_changing_frozen_script(self):
        self.configure();self.report("S001","FAIL");self.w.script={"offline":"离线字幕"}
        plan=self.w.repair.prepare("S001",["action_sequence"],"offline approved subtitle punctuation repair","offline evidence")
        with patch("pianchang.acceptance.required_subtitle",return_value={"expected_text":"离线字幕。"}),\
             patch("pianchang.video_collab_agent._validated_media",side_effect=ValueError("offline source gate reached")) as gate:
            with self.assertRaisesRegex(ValueError,"offline source gate reached"):
                self.w.repair.edit("S001",plan["repair_id"],4.0,"离线字幕。")
        gate.assert_called_once()
        with patch("pianchang.acceptance.required_subtitle",return_value={"expected_text":"未经批准的新内容。"}),patch("pianchang.media_tools._run") as execute:
            with self.assertRaisesRegex(ValueError,"字幕文字须来自"):
                self.w.repair.edit("S001",plan["repair_id"],4.0,"未经批准的新内容。")
        execute.assert_not_called()

    def test_project_recovers_missing_literal_subtitle_check_without_new_commission(self):
        self.configure();self.report("S001","PASS")
        project={"shots":[{"id":"S001","description":"字幕浮现：离线测试字幕。"}]}
        _write(self.w.directory/"project.json",project);_write(self.w.directory/"reports.json",self.w.reports)
        _write(self.w.directory/"agent/run.json",{"status":"needs_input","steps":18,"limits":{"max_steps":80},"result":{"reason":"model_stopped"}})
        self.assertTrue(recoverable_order(self.w.directory))
        ref=self.w.reports["S001"];report=_read(ref["report_path"])
        report["results"].append({"criterion_id":"script_subtitle","required":True,"status":"FAIL"})
        _write(ref["report_path"],report)
        self.assertFalse(recoverable_order(self.w.directory))
        report["results"][-1].update(status="UNKNOWN",execution_status="evidence_invalid")
        _write(ref["report_path"],report)
        self.assertTrue(recoverable_order(self.w.directory))
        report["results"][-1]["frame_audit_version"]="subtitle-frame-transcription/1"
        _write(ref["report_path"],report)
        self.assertFalse(recoverable_order(self.w.directory))

    def test_regressing_candidate_does_not_replace_passed_action_and_failed_evidence_is_retained(self):
        self.configure()
        old=self.report("S001","PASS")
        source=copy.deepcopy(self.w.shots["S001"]);old_summary=copy.deepcopy(self.w.reports["S001"])
        candidate={**source,"media_sha256":"b"*64}
        failed={**copy.deepcopy(old),"media_sha256":candidate["media_sha256"],"status":"FAIL","task_complete":False}
        failed["results"][0]["status"]="FAIL"
        path=self.root/"new-candidate/report.json";_write(path,failed)
        new_summary={"report_path":str(path),"report_sha256":digest_json(failed),"status":"FAIL"}
        self.w.shots["S001"]=candidate;self.w.reports["S001"]=new_summary
        history={"repair_id":"offline-new","shot_id":"S001","status":"reviewed","source_clip":source,
                 "source_report":old_summary,"result_clip":candidate,"result_report":copy.deepcopy(new_summary)}
        self.w.repair.history=[history]
        with patch("pianchang.video_collab_agent._validated_media"),patch.object(self.w,"review_shot") as review:
            result=self.w.repair.assess_candidate(history)
        self.assertEqual(result["status"],"kept_previous_candidate")
        self.assertEqual(self.w.shots["S001"]["media_sha256"],source["media_sha256"])
        self.assertEqual(history["status"],"reviewed_rejected_regression")
        self.assertEqual(history["result_report"],new_summary)
        self.assertEqual(_read(path)["results"][0]["status"],"FAIL")
        review.assert_called_once_with("S001")

    def test_restart_can_restore_good_candidate_before_multiple_historical_regressions(self):
        self.configure();old=self.report("S001","PASS")
        source={**copy.deepcopy(self.w.shots["S001"]),"dependency_sha256":"d"*64}
        good=copy.deepcopy(self.w.reports["S001"])
        self.w.impact={"shots":{"S001":{"target":{"sha256":"d"*64}}}}
        bad=[]
        for index in [1,2]:
            clip={**source,"media_sha256":str(index)*64}
            report={**copy.deepcopy(old),"media_sha256":clip["media_sha256"],"status":"FAIL","task_complete":False}
            report["results"][0]["status"]="FAIL"
            path=self.root/str(index)/"report.json";_write(path,report)
            bad.append((clip,{"report_path":str(path),"report_sha256":digest_json(report)}))
        self.w.shots["S001"],self.w.reports["S001"]=bad[1]
        self.w.repair.history=[{"shot_id":"S001","source_clip":source,"source_report":good,"result_clip":bad[0][0],"result_report":bad[0][1]},
                               {"shot_id":"S001","source_clip":bad[0][0],"source_report":bad[0][1],"result_clip":bad[1][0],"result_report":bad[1][1]}]
        with patch("pianchang.video_collab_agent._validated_media"),patch.object(self.w,"review_shot") as review:
            result=self.w.repair.recover_best_candidates()
        self.assertEqual(self.w.shots["S001"]["media_sha256"],source["media_sha256"])
        self.assertEqual(len(result),1)
        review.assert_called_once_with("S001")
        for clip,summary in bad:self.assertEqual(_read(summary["report_path"])["results"][0]["status"],"FAIL")

    def cut_regression_fixture(self):
        self.configure();old=self.report("S001","PASS");self.report("S002","PASS")
        source=copy.deepcopy(self.w.shots["S001"])
        old_summary=copy.deepcopy(self.w.reports["S001"])
        candidate={**source,"media_sha256":"b"*64}
        for clip,report,summary,status in [(source,old,old_summary,"PASS"),
                (candidate,copy.deepcopy(old),{"report_path":str(self.root/"new-cut/report.json")},"FAIL")]:
            report.update(media_sha256=clip["media_sha256"],content_review_version="offline-native-review/1")
            report["results"].append({"criterion_id":"continuity:S001->S002","required":True,"status":status})
            evidence={"selected_media":{"S001":{"sha256":clip["media_sha256"]},"S002":{"sha256":"a"*64}}}
            report["content_evidence_sha256"]=digest_json(evidence)
            _write(Path(summary["report_path"]).parent/"content.json",evidence)
            _write(summary["report_path"],report);summary["report_sha256"]=digest_json(report)
            if clip is candidate:new_summary=summary
        history={"shot_id":"S001","repair_id":"offline-cut-regression","status":"reviewed",
            "source_clip":source,"source_report":old_summary,"result_clip":candidate,"result_report":new_summary}
        self.w.shots["S001"]=candidate;self.w.reports["S001"]=new_summary;self.w.repair.history=[history]
        return source,history

    def test_new_candidate_cannot_destroy_passed_adjacent_cut_with_unchanged_neighbor(self):
        source,history=self.cut_regression_fixture()
        with patch("pianchang.video_collab_agent._validated_media"),patch.object(self.w,"review_shot") as review:
            result=self.w.repair.assess_candidate(history)
        self.assertEqual(result["regressed_criteria"],["continuity:S001->S002"])
        self.assertEqual(self.w.shots["S001"],source)
        self.assertEqual(history["status"],"reviewed_rejected_regression")
        self.assertEqual(_read(history["result_report"]["report_path"])["results"][-1]["status"],"FAIL")
        review.assert_called_once_with("S001")

    def test_historical_cut_pass_is_not_reused_when_other_endpoint_changed(self):
        source,history=self.cut_regression_fixture()
        self.w.shots["S002"]["media_sha256"]="c"*64
        with patch.object(self.w,"review_shot") as review:
            result=self.w.repair.assess_candidate(history)
        self.assertEqual(result["status"],"candidate_reviewed")
        self.assertEqual(self.w.shots["S001"],history["result_clip"])
        review.assert_not_called()

    def test_project_detects_current_cut_regression_for_same_order_recovery(self):
        source,history=self.cut_regression_fixture();self.w.repair._paths()
        _write(self.w.directory/"clips.json",self.w.shots)
        _write(self.w.directory/"agent/run.json",{"status":"needs_input","steps":18,"limits":{"max_steps":80},"result":{"reason":"model_stopped"}})
        self.assertTrue(recoverable_order(self.w.directory))
        self.w.shots["S001"]=source;_write(self.w.directory/"clips.json",self.w.shots)
        self.assertFalse(recoverable_order(self.w.directory))

    def joint_selection_fixture(self):
        self.configure();history=[]
        for sid,sha in [("S001","b"*64),("S002","c"*64)]:
            source_report=self.report(sid,"PASS");source=copy.deepcopy(self.w.shots[sid]);source_summary=copy.deepcopy(self.w.reports[sid])
            candidate={**source,"media_sha256":sha}
            result_summary={"report_path":str(self.root/(sid+"-current")/"report.json")}
            for clip,summary,pair,status in [(source,source_summary,{"S001":"a"*64,"S002":"a"*64},"PASS"),
                    (candidate,result_summary,{"S001":"b"*64,"S002":"c"*64},"FAIL")]:
                report=copy.deepcopy(source_report);report.update(media_sha256=clip["media_sha256"],content_review_version="offline-native/1")
                report["results"].append({"criterion_id":"continuity:S001->S002","required":True,"status":status})
                content={"selected_media":{key:{"sha256":value} for key,value in pair.items()}}
                report["content_evidence_sha256"]=digest_json(content)
                _write(Path(summary["report_path"]).parent/"content.json",content);_write(summary["report_path"],report)
                summary["report_sha256"]=digest_json(report)
            history.append({"shot_id":sid,"repair_id":"offline-joint-"+sid,"status":"reviewed","strategy":"offline",
                "source_clip":source,"source_report":source_summary,"result_clip":candidate,"result_report":result_summary})
            self.w.shots[sid]=candidate;self.w.reports[sid]=result_summary
        self.w.repair.history=history
        return history

    def test_two_changed_neighbors_are_jointly_restored_using_real_old_pair_evidence(self):
        history=self.joint_selection_fixture()
        with patch("pianchang.video_agent.VideoRunner.submit") as submit,patch("pianchang.content_review.ContentObserver.observe_video") as observe:
            plan=verified_selection_plan(self.project,self.w.shots,self.w.reports,history)
        self.assertEqual(set(plan["changes"]),{"S001","S002"})
        self.assertEqual(plan["known_pass_gain"],1)
        self.assertTrue(all(value["clip"]["media_sha256"]=="a"*64 for value in plan["changes"].values()))
        submit.assert_not_called();observe.assert_not_called()
        # Planning is pure; the project executor applies and revalidates separately.
        self.assertEqual(self.w.shots["S001"]["media_sha256"],"b"*64)

    def test_joint_selection_cannot_sacrifice_passed_action_for_a_passed_cut(self):
        history=self.joint_selection_fixture();summary=history[0]["source_report"]
        report=_read(summary["report_path"]);report["results"][0]["status"]="FAIL"
        _write(summary["report_path"],report);summary["report_sha256"]=digest_json(report)
        self.assertFalse(verified_selection_plan(self.project,self.w.shots,self.w.reports,history)["changes"])

    def test_joint_selection_does_not_invent_verdict_for_an_unreviewed_pair(self):
        history=self.joint_selection_fixture();history[1]["source_report"]=None
        summary=history[0]["source_report"];report=_read(summary["report_path"])
        report["results"].append({"criterion_id":"offline-extra-required","required":True,"status":"PASS"})
        _write(summary["report_path"],report);summary["report_sha256"]=digest_json(report)
        self.assertFalse(verified_selection_plan(self.project,self.w.shots,self.w.reports,history)["changes"])

    def test_joint_candidate_execution_rebinds_actual_selection_without_new_video(self):
        self.joint_selection_fixture()
        self.w.impact={"shots":{sid:{"target":{"sha256":"d"*64}} for sid in ["S001","S002"]}}
        for item in self.w.repair.history:item["source_clip"]["dependency_sha256"]="d"*64
        with patch("pianchang.video_collab_agent._validated_media"),patch.object(self.w,"review_shot") as review,patch("pianchang.video_agent.VideoRunner.submit") as submit:
            result=self.w.repair.recover_best_candidates()
        self.assertTrue(result[-1]["joint_selection"])
        self.assertEqual({sid:clip["media_sha256"] for sid,clip in self.w.shots.items()},{"S001":"a"*64,"S002":"a"*64})
        review.assert_called_once_with("S001");submit.assert_not_called()

    def test_designer_changes_actual_strategy_instead_of_resubmitting_failed_prompt(self):
        self.configure();self.report("S002")
        old="offline previous failed instruction: stand and look then walk together without timing"
        job={"metadata":{"acceptance_repair":{"instruction":old,"repair_id":"old"},"required_refs":[]}}
        path=self.root/"old-job.json";_write(path,job)
        self.w.shots["S002"].update(repair_id="old",task_id="offline-task",manifest_path=str(path),generation_job_sha256=digest_json(job))
        choices=[{"strategy_family":"timed_action_beats","change_from_previous":"offline mock explicitly changes starting pose and action timing","instruction":old},
                 {"strategy_family":"blocking_and_paths","change_from_previous":"offline mock uses separate paths and holds the final observable state",
                  "instruction":"offline new choreography: 0 to 1 second start with visibly slumped shoulders; 1 to 2 seconds straighten shoulders; 2 to 4 seconds keep feet fixed and continuously track the approaching partner."}]
        self.w.decision_client=Mock()
        self.w.decision_client.complete_json.side_effect=[{"data":c} for c in choices]
        with patch.object(self.w,"generate") as submit:
            result=self.w.autonomy.design("S002")
            submit.assert_not_called()
        self.assertEqual(self.w.decision_client.complete_json.call_count,2)
        self.assertEqual(result["strategy_design"]["strategy_family"],"blocking_and_paths")
        self.assertNotEqual(result["instruction"],old)

    def test_specification_blockers_do_not_erase_failures_or_skip_other_fields(self):
        record=self.configure()
        r=self.report("S001","FAIL");self.report("S002","PASS")
        r["results"]=[{"criterion_id":"start_end_state","required":True,"status":"FAIL","reason":"offline conflicting field and repairable field",
            "state_checks":[{"phase":"start","path":"prop:book.holder","verdict":"FAIL"},
                            {"phase":"end","path":"char:zhangming.location","verdict":"FAIL"}]}]
        _write(self.w.reports["S001"]["report_path"],r)
        self.w.reports["S001"]["report_sha256"]=digest_json(r)
        controller=self.w.autonomy
        controller.specification_blockers["S001/start/prop:book.holder"]={"shot_id":"S001","reason":"offline bound conflict"}
        self.assertTrue(any(x["shot_id"]=="S001" for x in controller.queue(record)["actionable"]))
        controller.specification_blockers["S001/end/char:zhangming.location"]={"shot_id":"S001","reason":"offline second bound conflict"}
        q=controller.queue(record)
        self.assertFalse(q["actionable"])
        self.assertTrue(q["blocked"])
        self.assertFalse(q["all_required_passed"])
        self.assertEqual(_read(self.w.reports["S001"]["report_path"])["results"][0]["status"],"FAIL")

    def recovery_fixture(self, known):
        record=self.configure();self.report("S001");self.report("S002")
        self.w.video_config=VideoConfig(output_dir=str(self.root/"videos"))
        repair=self.w.repair.prepare("S001",["action_sequence"],"offline different action timing for recovery","offline evidence")
        plan=self.w.repair.plans[repair["repair_id"]]
        self.w.repair._start(plan,"prompt_and_locked_references")
        impact={"shots":{"S001":{"target":{"sha256":"d"*64}}}}
        _write(self.w.directory/"project.json",self.project);_write(self.w.directory/"impact.json",impact)
        _write(self.w.directory/"clips.json",self.w.shots);_write(self.w.directory/"reports.json",self.w.reports)
        record.update(video_generations=1,steps=3,repair_reservations={"S001":1},
            pending_tool={"tool":"regenerate_shot","effect":"video","arguments":{"shot_id":"S001","repair_id":repair["repair_id"]}})
        if known:
            job={"task_id":"offline-existing-provider-id","metadata":{"execution_key":self.w.operation_key("S001",plan),
                "script_asset_ref":self.w.ref,"dependency_sha256":"d"*64}}
            _write(self.root/"videos/offline/job.json",job)
        return record

    def test_recovery_observes_existing_task_without_post_or_budget_reset(self):
        record=self.recovery_fixture(True)
        self.w.defer_content_review=True
        self.w.vision_config=object();self.w.reference_store=Mock()
        with patch("pianchang.video_collab_agent._validated_media"),patch("pianchang.video_agent.VideoRunner.submit") as post,\
             patch("pianchang.video_agent.VideoRunner.resume",return_value={"status":"SUCCEEDED"}) as resume,\
             patch.object(self.w,"adopt_generated") as adopt,patch("pianchang.content_review.run_content_reviews") as review_selection,\
             patch.object(self.w.repair,"recover_pending_evaluations",return_value=[]):
            result=self.w.autonomy.recover(record)
        post.assert_not_called();resume.assert_called_once();adopt.assert_called_once()
        self.assertFalse(self.w.defer_content_review)
        review_selection.assert_called_once_with(self.w)
        self.assertEqual(result["outcomes"][0]["status"],"recovered_and_reviewed")
        self.assertEqual(record["video_generations"],1)
        self.assertEqual(record["repair_reservations"],{"S001":1})
        self.assertEqual(self.w.repair.history[0]["status"],"reviewed")

    def test_generated_candidate_with_only_technical_checks_gets_real_content_review_without_submission(self):
        self.configure();old=self.report("S001","PASS")
        source=copy.deepcopy(self.w.shots["S001"]);source_report=copy.deepcopy(self.w.reports["S001"])
        clip={**source,"media_sha256":"b"*64}
        technical={**copy.deepcopy(old),"media_sha256":clip["media_sha256"],"status":"UNKNOWN"}
        path=self.root/"technical/report.json";_write(path,technical)
        summary={"report_path":str(path),"report_sha256":digest_json(technical)}
        history={"repair_id":"offline-missed-review","shot_id":"S001","status":"reviewed_rejected_regression",
                 "source_clip":source,"source_report":source_report,"result_clip":clip,"result_report":summary}
        self.w.repair.history=[history];self.w.vision_config=object();self.w.reference_store=Mock()
        def actual_review(sid):
            self.assertEqual(self.w.shots[sid]["media_sha256"],clip["media_sha256"])
            reviewed={**technical,"content_review_version":"offline-mock/1","status":"PASS"}
            target=self.root/"reviewed/report.json";_write(target,reviewed)
            self.w.reports[sid]={"report_path":str(target),"report_sha256":digest_json(reviewed)}
        with patch("pianchang.video_collab_agent._validated_media"),patch.object(self.w,"review_shot",side_effect=actual_review) as review,\
             patch("pianchang.video_agent.VideoRunner.submit") as post:
            result=self.w.repair.recover_pending_evaluations()
            self.assertEqual(self.w.repair.recover_pending_evaluations(),[])
        post.assert_not_called();review.assert_called_once_with("S001")
        self.assertEqual(result[0]["status"],"missing_content_review_completed")
        self.assertFalse(self.w.defer_content_review)
        self.assertEqual(history["prior_evaluations"][0]["report"],summary)
        self.assertEqual(_read(path),technical)

    def test_failed_candidate_content_evaluation_preserves_selection_and_does_not_hide_other_work(self):
        record=self.configure();self.report("S001","PASS");self.report("S002","FAIL")
        before_clips,before_reports=copy.deepcopy(self.w.shots),copy.deepcopy(self.w.reports)
        clip={**self.w.shots["S001"],"media_sha256":"b"*64}
        technical={**_read(self.w.reports["S001"]["report_path"]),"media_sha256":clip["media_sha256"]}
        path=self.root/"technical/report.json";_write(path,technical)
        history={"repair_id":"offline-evaluation-failed","shot_id":"S001","status":"reviewed_rejected_regression",
                 "result_clip":clip,"result_report":{"report_path":str(path),"report_sha256":digest_json(technical)}}
        self.w.repair.history=[history];self.w.vision_config=object();self.w.reference_store=Mock()
        with patch("pianchang.video_collab_agent._validated_media"),patch.object(self.w,"review_shot",side_effect=ValueError("offline missing evidence receipt")),\
             patch("pianchang.video_agent.VideoRunner.submit") as post:
            result=self.w.repair.recover_pending_evaluations()
        post.assert_not_called()
        self.assertEqual(self.w.shots,before_clips);self.assertEqual(self.w.reports,before_reports)
        self.assertEqual(result[0]["status"],"blocked_evaluation")
        self.assertEqual(history["status"],"blocked_evaluation")
        self.assertEqual({row["shot_id"] for row in self.w.autonomy.queue(record)["actionable"]},{"S002"})

    def test_missing_provider_id_blocks_only_that_operation_and_never_posts(self):
        record=self.recovery_fixture(False)
        with patch("pianchang.video_collab_agent._validated_media"),patch("pianchang.video_agent.VideoRunner.submit") as post:
            result=self.w.autonomy.recover(record)
        post.assert_not_called()
        self.assertIn("S001",result["blocked_shots"])
        self.assertEqual({x["shot_id"] for x in self.w.autonomy.queue(record)["actionable"]},{"S002"})

    def test_loop_process_interruption_resumes_after_reserved_tool_without_replaying_it(self):
        side_effects=[]
        def interrupted():
            side_effects.append("offline existing remote task")
            raise KeyboardInterrupt("offline simulated process death")
        tool=Tool("generate_shot","offline",{"type":"object","properties":{}},interrupted,effect="video")
        model=Mock();model.complete_json.return_value={"data":call("generate_shot")}
        loop=AgentLoop(model,[tool],self.root/"loop",snapshot={},finish_check=lambda:{"complete":True})
        with self.assertRaises(KeyboardInterrupt):loop.run("offline interrupted task",max_steps=3,max_video_generations=1)
        before=_read(self.root/"loop/run.json")
        self.assertEqual(before["video_generations"],1);self.assertEqual(before["pending_tool"]["tool"],"generate_shot")
        second=Mock();second.complete_json.return_value={"data":FINISH}
        recover=Mock(return_value={"new_video_posts":0,"task_id":"offline existing remote task"})
        resumed=AgentLoop(second,[tool],self.root/"loop",snapshot={},finish_check=lambda:{"complete":True},recover=recover)
        result=resumed.run("offline interrupted task",max_steps=3,max_video_generations=1,resume=True)
        self.assertTrue(result["task_complete"])
        self.assertEqual(result["video_generations"],1);self.assertEqual(result["steps"],2)
        self.assertEqual(len(side_effects),1)
        self.assertTrue(list((self.root/"loop").glob("before-resume-*.json")))

    def test_startup_requeues_same_order_and_cancellation_prevents_recovery(self):
        job={"id":"offline-job","kind":"studio_video","status":"interrupted","project_id":"offline-project",
             "message_id":"offline-frozen-order","_autonomy_enabled":True,"_autonomy_body":{"plan_id":"offline"},"result":None}
        service=SimpleNamespace(lock=threading.RLock(),state={"jobs":[job]},tracer=self.tracer,
            executor=Mock(),_save=Mock(),_project=Mock(return_value=(None,self.project)),_execute=Mock())
        result=enqueue_recovery(service)
        self.assertEqual(result["jobs"],["offline-job"])
        self.assertTrue(job["_autonomy_resuming"])
        self.assertEqual(job["message_id"],"offline-frozen-order")
        self.assertEqual(job["_autonomy_recovery_count"],1)
        self.assertEqual(enqueue_recovery(service)["jobs"],[])
        job.update(status="interrupted",cancel_requested=True)
        self.assertEqual(enqueue_recovery(service)["jobs"],[])
        job.update(cancel_requested=False,trace_incomplete=True)
        self.assertEqual(enqueue_recovery(service)["jobs"],[])
        self.assertEqual(service.executor.submit.call_count,1)

    def test_recovery_limit_counts_stalled_checkpoint_and_preserves_lifetime_count_and_budget(self):
        runid="1"*32;mid="2"*32
        work=self.root/"outputs/collaborations/runs"/runid/("video-spec-"+mid)
        job={"id":"offline-progress","kind":"studio_video","status":"interrupted","project_id":"offline-project",
             "run_id":runid,"message_id":mid,"_autonomy_enabled":True,"_autonomy_body":{"plan_id":"offline-frozen"}}
        service=SimpleNamespace(root=self.root,lock=threading.RLock(),state={"jobs":[job]},tracer=self.tracer,
            executor=Mock(),_save=Mock(),_project=Mock(return_value=(None,self.project)),_execute=Mock())
        record={"video_generations":1,"repair_reservations":{"S001":1}}
        for step in range(1,6):
            record["steps"]=step;_write(work/"agent/run.json",record);job["status"]="interrupted"
            self.assertEqual(enqueue_recovery(service)["jobs"],[job["id"]])
            self.assertEqual(job["_autonomy_recovery_stalls"],1)
        self.assertEqual(job["_autonomy_recovery_count"],5)
        for expected in [2,3]:
            job["status"]="interrupted";self.assertEqual(enqueue_recovery(service)["jobs"],[job["id"]])
            self.assertEqual(job["_autonomy_recovery_stalls"],expected)
        job["status"]="interrupted";self.assertFalse(enqueue_recovery(service)["jobs"])
        self.assertEqual(job["_autonomy_recovery_count"],7)
        self.assertEqual(_read(work/"agent/run.json"),record)

    def test_protocol_failure_recovery_is_scheduled_by_project_without_new_commission(self):
        runid="1"*32;mid="2"*32
        work=self.root/"outputs/collaborations/runs"/runid/("video-spec-"+mid)
        _write(work/"agent/run.json",{"status":"needs_input","steps":12,"video_generations":1,
            "limits":{"max_steps":80},"result":{"reason":"model_stopped"}})
        _write(work/"coordination/mock/response/run.json",{"status":"needs_input","steps":5,"limits":{"max_steps":8},
            "messages":[{"role":"user","content":json.dumps({"type":"tool_result","ok":False,"error":{"code":"INVALID_DECISION"}})}]})
        job={"id":"offline-protocol","kind":"studio_video","status":"failed","project_id":"offline-project",
             "message_id":mid,"run_id":runid,"_autonomy_enabled":True,"_autonomy_body":{"plan_id":"offline-original"},"result":None}
        service=SimpleNamespace(root=self.root,lock=threading.RLock(),state={"jobs":[job]},tracer=self.tracer,
            executor=Mock(),_save=Mock(),_project=Mock(return_value=(None,self.project)),_execute=Mock())
        self.assertEqual(enqueue_recovery(service)["jobs"],[job["id"]])
        self.assertEqual(job["message_id"],mid)
        self.assertEqual(_read(work/"agent/run.json")["video_generations"],1)
        self.assertTrue(job["_autonomy_resuming"])


class CoordinationTests(unittest.TestCase):
    setUp=negotiation_fixtures.NegotiationTests.setUp
    request=negotiation_fixtures.NegotiationTests.request

    def test_missing_display_summary_is_repaired_without_changing_execution_arguments(self):
        mid=self.request()
        decision=call("decide_revision_request",decision="retain",explanation="offline preserve frozen script",instruction="")
        decision.pop("summary")
        model=OfflineDecisions(call("inspect_revision_request"),decision,FINISH)
        w=SimpleNamespace(directory=self.folder/"video",decision_client=model)
        controller=VideoAutonomy(w,self.video)
        result=controller.coordinate()
        self.assertEqual(result["observations"][0]["status"],"answered")
        path=w.directory/"coordination"/digest_json(mid)[:12]/"response/events.jsonl"
        events=[json.loads(x) for x in path.read_text('utf-8').splitlines()]
        raw=next(x["data"]["decision"] for x in events if x["name"]=="decision" and x["data"]["decision"].get("tool")=="decide_revision_request")
        self.assertNotIn("summary",raw)
        self.assertTrue(any(x["name"]=="protocol_metadata_normalized" for x in events))
        reply=self.store.messages(causation_id=mid,message_type="script.revision_response")[0]
        self.assertEqual(reply["payload"]["decision"],decision["arguments"]["decision"])

    def test_protocol_blocked_internal_request_resumes_itself_with_original_step_budget(self):
        from pianchang.collab_negotiation import run_script_negotiation
        mid=self.request()
        bad={**call("decide_revision_request",decision="retain",explanation="offline invalid envelope",instruction=""),"unexpected":True}
        first=OfflineDecisions(call("inspect_revision_request"),bad,bad,bad,
            {"type":"finish","outcome":"needs_input","summary":"offline blocked by protocol errors"})
        w=SimpleNamespace(directory=self.folder/"video",decision_client=first)
        path=w.directory/"coordination"/digest_json(mid)[:12]/"response"
        old=run_script_negotiation(first,path,store=self.store,request_id=mid)
        self.assertEqual(old["status"],"blocked")
        w.decision_client=OfflineDecisions(call("decide_revision_request",decision="alternative",explanation="offline resolved validated format",instruction="offline distinct timing"),FINISH)
        controller=VideoAutonomy(w,self.video)
        result=controller.coordinate()
        self.assertEqual(result["observations"][0]["status"],"answered")
        resumed=_read(path/"run.json")
        self.assertEqual(resumed["steps"],7)
        self.assertEqual(resumed["limits"]["max_steps"],8)
        self.assertTrue(list(path.glob("before-resume-*.json")))
        self.assertEqual(self.store.get_message(mid)["deliveries"]["script"],"completed")

    def test_one_blocked_request_does_not_stop_another_pending_agent_request(self):
        from pianchang.collab_negotiation import run_script_negotiation
        first=self.request()
        second=self.video.request_script_revision("B002","offline second request needs pacing advice","offline preserve original script",
            self.evidence["asset_id"],self.evidence["revision_id"])["request_id"]
        w=SimpleNamespace(directory=self.folder/"video",decision_client=OfflineDecisions())
        path=w.directory/"coordination"/digest_json(first)[:12]/"response"
        run_script_negotiation(OfflineDecisions({"type":"finish","outcome":"needs_input","summary":"offline external evidence missing"}),
            path,store=self.store,request_id=first)
        w.decision_client=OfflineDecisions(call("inspect_revision_request"),call("decide_revision_request",decision="retain",
            explanation="offline second request resolved",instruction=""),FINISH)
        result=VideoAutonomy(w,self.video).coordinate()
        self.assertEqual([r["status"] for r in result["observations"]],["blocked","answered"])
        self.assertEqual(self.store.get_message(second)["deliveries"]["script"],"completed")

    def test_project_routes_request_and_reply_once_without_external_worker(self):
        mid=self.request()
        model=OfflineDecisions(call("inspect_revision_request"),call("decide_revision_request",decision="alternative",
            explanation="offline evidence proves pacing mismatch",instruction="offline alternative timing without changing script"),FINISH)
        w=SimpleNamespace(directory=self.folder/"video",decision_client=model)
        controller=VideoAutonomy(w,self.video)
        first=controller.coordinate();count=len(model.requests)
        self.assertEqual(first["observations"][0]["status"],"answered")
        self.assertEqual(self.store.get_message(mid)["deliveries"]["script"],"completed")
        reply=self.store.messages(causation_id=mid,message_type="script.revision_response")[0]
        self.assertEqual(reply["deliveries"]["video"],"completed")
        controller.coordinate()
        self.assertEqual(len(model.requests),count)
        self.assertEqual(self.store.get_asset("story/script")["revision_id"],"r1")


class DeliveryRecoveryTests(unittest.TestCase):
    def test_original_accepted_and_unknown_receipts_survive_automatic_order_resume(self):
        import tempfile
        from pianchang.collaboration_workflow import Collaboration,ref
        with tempfile.TemporaryDirectory() as temp, use_context(None,tracer=Tracer(Path(temp)/"traces")):
            root=Path(temp);(root/"offline.env").write_text("# explicit offline configuration",encoding="utf-8")
            for prior_status in ["accepted","unknown"]:
                session=Collaboration(root/prior_status,"1"*32,env_file=root/"offline.env")
                session.folder.mkdir(parents=True)
                session.write(id=session.run_id,stages=[])
                source=json.loads((Path(__file__).resolve().parents[1]/"fixtures/story_script.json").read_text("utf-8"))
                script=session.store.publish_asset("offline/script",source,kind="script",producer="script",
                    story_id=session.story,correlation_id=session.run_id,topic="script.revision_published")
                plan={"acceptance_loop":acceptance_policy(["S001"])}
                order=session.store.send("production.requested",sender="commissioner",recipient="video",category="request",
                    story_id=session.story,causation_id=script["event_id"],payload={"script":ref(script),"template":None,
                    "previous":None,"max_video_generations":2,"shooting_plan":plan,"goal":"explicit offline resume"})
                session.store.claim("video",message_id=order["message_id"])
                session.store.ack("video",order["message_id"],"accepted",result={"offline_original_acceptance":True})
                if prior_status=="unknown":session.store.ack("video",order["message_id"],"unknown",result={"offline_interrupted":True})
                with patch("pianchang.video_collab_agent.run_video_agent",return_value={"production":None,"partial":None,
                    "loop":{"summary":"offline evidenced blocker","task_complete":False}}) as execute:
                    with self.assertRaisesRegex(ValueError,"offline evidenced blocker"):
                        session.worker(order["message_id"],resume=True)
                self.assertTrue(execute.call_args.kwargs["resume"])
                self.assertEqual(execute.call_args.kwargs["max_video_generations"],2)
                self.assertEqual(session.store.get_message(order["message_id"])["deliveries"]["video"],"blocked")
                self.assertEqual(len(session.store.messages(session.run_id,message_type="production.requested")),1)


if __name__=="__main__":unittest.main()
