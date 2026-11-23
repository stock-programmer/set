"""Offline repair-loop integration. All semantic judgments and provider exchanges are explicit mocks."""
import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pianchang.acceptance import create_contract, file_sha256
from pianchang.agent_loop import AgentLoop, Tool
from pianchang.bailian_video import VideoConfig
from pianchang.media_tools import find_ffmpeg, inspect_media
from pianchang.story_spec import digest_json
from trace_system import TraceError, Tracer, span, use_context
from pianchang.video_collab_agent import VideoWorkspace, _read, _write, run_video_agent
from pianchang.video_repair import acceptance_policy, local_failure_before_generation, validate_lineage


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tracer = Tracer(self.root / "traces")
        self.enterContext(self.tracer.storage_session())
        self.binding = use_context(None, tracer=self.tracer)
        self.binding.__enter__()
        self.addCleanup(self.binding.__exit__, None, None, None)
        self.project = _read(Path(__file__).resolve().parents[1] / "fixtures/story_project.json")
        self.script = self.project["script_source"]["script"]
        self.w = VideoWorkspace(script=self.script, script_ref={"asset_id":"offline-story", "revision_id":"r1"},
            directory=self.root / "work", text_config=None, video_config=VideoConfig(), template_project=self.project,
            previous=None, decision_client=None, vision_config=None, progress=None)
        self.w.project = self.project

    def report(self, sid, status="FAIL", required=True):
        folder = self.root / sid
        p = folder / "report.json"
        clip = {"media_sha256": "a" * 64, "media_path": str(folder / "video.mp4")}
        report = {"shot_id": sid, "project_sha256": digest_json(self.project), "media_sha256": clip["media_sha256"],
                  "status": status, "task_complete": status == "PASS", "results": [
                      {"criterion_id":"action_sequence", "required":required, "status":status,
                       "reason":"offline mocked semantic observation"}]}
        _write(p, report)
        _write(folder / "contract.json", {"requirements":[{"criterion_id":"action_sequence", "required":True}]})
        self.w.shots[sid] = clip
        self.w.reports[sid] = {"report_path":str(p), "report_sha256":digest_json(report), "media_accepted":True}
        return report

    def test_fail_unknown_and_missing_required_cannot_unlock_preview(self):
        for sid in [s["id"] for s in self.project["shots"]]:
            self.report(sid, "PASS")
        self.assertTrue(self.w.acceptance_gate(validate=False)["passed"])
        for status, required in [("FAIL", True), ("UNKNOWN", True), ("PASS", False)]:
            self.report("S002", status, required)
            self.assertFalse(self.w.acceptance_gate(validate=False)["passed"])
            with patch.object(self.w, "review_shot"), patch.object(self.w, "_export") as export, \
                 patch.object(self.w, "acceptance_gate", return_value=self.w.acceptance_gate(validate=False)):
                result = self.w.review_and_export()
                self.assertIsNone(result["preview"])
                export.assert_not_called()

    def test_mixed_audio_requires_bound_failure_before_a_retake(self):
        from pianchang.creative_sound import POLICY
        self.project["shots"][0]["description"] = "屏幕行情跳动，财经主播播报市场下跌。"
        report = self.report("S001", "FAIL")
        report["content_review_version"] = "grounded-video-review/2"
        summary = self.w.reports["S001"]
        def save(status, **fields):
            report["results"][0].update(status=status, **fields)
            _write(Path(summary["report_path"]), report)
            summary["report_sha256"] = digest_json(report)
        save("FAIL")
        issue = next(i for i in self.w.acceptance_gate(validate=False)["issues"] if i["shot_id"] == "S001")
        self.assertTrue(issue["needs_creative_audio"])
        self.assertEqual(issue["status"], "UNKNOWN")
        for verified in (False, True):
            if verified:
                save("UNKNOWN", creative_audio_policy=POLICY, execution_status="inconclusive")
            with self.assertRaisesRegex(ValueError, "先补齐"):
                self.w.repair.prepare("S001", ["action_sequence"], "offline retake direction", "offline evidence")
        save("FAIL", execution_status="executed")
        plan = self.w.repair.prepare("S001", ["action_sequence"], "offline retake direction", "bound visual failure")
        self.assertEqual(plan["criterion_ids"], ["action_sequence"])

    def test_repair_is_bound_to_real_unresolved_rows_and_stale_candidates_are_rejected(self):
        self.report("S001")
        with self.assertRaisesRegex(ValueError, "验收项"):
            self.w.repair.prepare("S001", ["invented"], "offline targeted direction", "offline")
        plan = self.w.repair.prepare("S001", ["action_sequence"], "offline targeted direction", "offline")
        self.w.shots["S001"]["media_sha256"] = "b" * 64
        with patch.object(self.w, "generate") as generate:
            with self.assertRaisesRegex(ValueError, "已改变"):
                self.w.repair.regenerate("S001", plan["repair_id"])
            generate.assert_not_called()

    def test_atomic_candidate_archive_uses_short_temporary_basename(self):
        parent=self.root
        while len(str(parent.resolve()))<165:parent=parent/("d"*16)
        target=parent/("a"*64+".json")
        self.assertLess(len(str(target.resolve())),260)
        self.assertGreater(len(str(target.resolve()))+34,259)
        _write(target,{"mode":"explicit_offline_atomic_fixture"})
        self.assertEqual(_read(target)["mode"],"explicit_offline_atomic_fixture")
        self.assertFalse(list(parent.glob('.w-*.tmp')))

    def test_identical_failed_generation_strategy_is_rejected_before_any_submission(self):
        self.report("S001")
        instruction = "offline previous failed action direction"
        manifest = {"metadata": {"acceptance_repair": {"repair_id": "offline-old", "instruction": instruction},
                                 "required_refs": []}}
        path = self.root / "offline-prior-job.json"; _write(path, manifest)
        self.w.shots["S001"].update(repair_id="offline-old", manifest_path=str(path), generation_job_sha256=digest_json(manifest))
        plan = self.w.repair.prepare("S001", ["action_sequence"], instruction, "offline repeated strategy")
        with patch.object(self.w, "generate") as generate:
            with self.assertRaisesRegex(ValueError, "不能原样"):
                self.w.repair.preflight("S001", plan["repair_id"])
            with self.assertRaisesRegex(ValueError, "不能原样"):
                self.w.repair.regenerate("S001", plan["repair_id"])
            generate.assert_not_called()
        self.assertFalse(self.w.repair.history)
        changed = self.w.repair.validate_generation_strategy("S001", instruction + "; different explicit movement path")
        self.assertTrue(changed["changed_prompt"])
        with patch.object(self.w, "_required", return_value=[{"asset_id": "offline-new-reference", "revision_id": "r2"}]):
            self.assertTrue(self.w.repair.validate_generation_strategy("S001", instruction)["changed_assets"])
        manifest["metadata"]["acceptance_repair"]["instruction"] = "offline tampered source"
        _write(path, manifest)
        with self.assertRaisesRegex(ValueError, "记录不符"):
            self.w.repair.validate_generation_strategy("S001", instruction)

    def test_local_archive_failure_is_recoverable_but_generation_started_is_uncertain(self):
        class Model:
            def complete_json(self,messages):return {"data":{"type":"tool","tool":"regenerate_shot","arguments":{},"summary":"offline mocked archive failure"}}
        for started in [False,True]:
            folder=self.root/str(started)
            def fail():
                if started:
                    with span("video.collab.generate",kind="tool",inputs={"mode":"offline_mock_boundary"}):
                        raise FileNotFoundError("offline mocked failure inside generation")
                raise FileNotFoundError("offline mocked archive path failure before generation")
            loop=AgentLoop(Model(),[Tool("regenerate_shot","offline",{"type":"object","properties":{}},fail,effect="video")],
                           folder/'agent',snapshot={},finish_check=lambda:{"complete":False})
            result=loop.run("offline pre-submit recovery test",max_steps=1,max_video_generations=1)
            self.assertFalse(result["task_complete"])
            repair={"strategy":"prompt_and_locked_references","repair_id":"offline-repair"}
            proof=local_failure_before_generation(repair,folder)
            self.assertEqual(proof["not_dispatched"],not started)
            self.assertEqual(local_failure_before_generation(repair,folder,saved_run=_read(folder/"agent/run.json")),proof)

    def test_nested_continuation_reuses_ancestor_report_without_model_replay(self):
        self.report("S001")
        original=copy.deepcopy(self.w.reports["S001"])
        earlier=self.root/("video-spec-"+"2"*32);recent=self.root/("video-spec-"+"1"*32)
        source={"prior_continuation":None,"mode":"offline provenance fixture"};sha=digest_json(source)
        _write(earlier/("resume-"+sha[:16]+".json"),{"source":source,"project":self.project})
        _write(earlier/"reports.json",{"S001":original});_write(recent/"reports.json",{})
        clip={"shot_id":"S001","media_sha256":"a"*64,"dependency_sha256":"d"*64,
              "generation_job_sha256":"c"*64,"script_ref":self.w.ref}
        self.w.partial={"script_ref":self.w.ref,"shots":{"S001":clip},"source":{"prior_continuation":{"request_id":"2"*32,"sha256":sha}}}
        self.w.shooting_plan={"continuation":{"request_id":"1"*32,"sha256":"c"*64}}
        self.w.impact={"reuse":["S001"],"shots":{"S001":{"target":{"sha256":"d"*64}}}}
        self.w.shots={};self.w.reports={}
        with patch("pianchang.video_collab_agent._validated_media",return_value={}),patch.object(self.w,"review_shot") as review:
            self.w.reuse("S001")
        self.assertEqual(self.w.reports["S001"]["report_sha256"],original["report_sha256"])
        self.assertEqual(self.w.reports["S001"]["report_path"],original["report_path"])
        review.assert_called_once_with("S001")

    def test_agent_cannot_ask_human_to_take_over_while_evidenced_repairs_remain(self):
        for s in self.project["shots"]:self.report(s["id"],"FAIL")
        record={"limits":{"max_video_generations":6},"video_generations":0,"messages":[]}
        self.assertFalse(self.w.stop_check(record)["allowed"])
        record["video_generations"]=6
        self.assertTrue(self.w.stop_check(record)["allowed"])
        self.report("S001","UNKNOWN")
        record["video_generations"]=0
        stop=self.w.stop_check(record)
        self.assertFalse(stop["allowed"])
        for s in self.project["shots"]:
            if s["id"]!="S001":self.report(s["id"],"PASS")
        stop=self.w.stop_check(record)
        self.assertTrue(stop["allowed"])
        self.assertTrue(stop["issues"])

    def test_trace_failure_stops_before_generation_and_unknown_operation_cannot_replay(self):
        self.report("S001")
        p = self.w.repair.prepare("S001", ["action_sequence"], "offline targeted direction", "offline")
        with patch("pianchang.video_collab_agent._write", side_effect=TraceError("offline store unavailable")), \
             patch.object(self.w, "generate") as generate:
            with self.assertRaises(TraceError):
                self.w.repair.regenerate("S001", p["repair_id"])
            generate.assert_not_called()
        with self.assertRaisesRegex(ValueError, "不得重复"):
            self.w.repair.regenerate("S001", p["repair_id"])

    def test_repair_budget_is_per_shot_and_does_not_consume_initial_other_shot(self):
        policy = acceptance_policy(["S001", "S002"], repairs_per_shot=1)
        plan = {"new_shots":["S001"],"retake_shots":[],"reuse_shots":["S002"],"acceptance_loop":policy}
        decisions = [
            {"type":"tool","tool":"regenerate_shot","arguments":{"shot_id":"S002"},"summary":"offline repair"},
            {"type":"tool","tool":"regenerate_shot","arguments":{"shot_id":"S002"},"summary":"offline over limit"},
            {"type":"tool","tool":"generate_shot","arguments":{"shot_id":"S001"},"summary":"offline initial"},
            {"type":"finish","outcome":"needs_input","summary":"offline still incomplete"}]
        class Model:
            def complete_json(self, messages): return {"data":decisions.pop(0)}
        calls=[]
        schema={"type":"object","properties":{"shot_id":{"type":"string"}},"required":["shot_id"]}
        tools=[Tool(name,"offline",schema,lambda shot_id,name=name:calls.append((name,shot_id)),effect="video")
               for name in ("generate_shot","regenerate_shot")]
        loop=AgentLoop(Model(),tools,self.root/"loop",snapshot={"project_ready":True},finish_check=lambda:{"complete":False})
        result=loop.run("offline autonomous repair",shooting_plan=plan,max_video_generations=3,max_steps=5)
        self.assertFalse(result["task_complete"])
        self.assertEqual(calls,[("regenerate_shot","S002"),("generate_shot","S001")])
        self.assertEqual(result["video_generations"],2)

    def test_real_local_asset_edit_preserves_origin_and_has_verifiable_lineage(self):
        self.report("S001")
        media=self.root/"S001/video.mp4"
        subprocess.run([str(find_ffmpeg()),"-nostdin","-v","error","-f","lavfi","-i",
                        "color=c=red:s=160x240:r=10:d=1","-c:v","libx264",str(media)],check=True)
        metadata={"shot_id":"S001","script_asset_ref":self.w.ref,"dependency_sha256":"d"*64}
        job={"schema_version":"bailian-video-job/1","base_url":"offline-mock","request":{},"metadata":metadata}
        job.update(request_sha256=digest_json({k:job[k] for k in ("base_url","request","metadata")}),
                   status="SUCCEEDED",task_id="offline-task",download_sha256=file_sha256(media))
        job_path=media.with_name("job.json");_write(job_path,job);original_job=job_path.read_bytes()
        clip={"shot_id":"S001","operation":"generated","media_path":str(media),"media_sha256":file_sha256(media),
              "task_id":"offline-task","manifest_path":str(job_path),"generation_job_sha256":digest_json(job),
              "dependency_sha256":"d"*64,"origin_script_ref":self.w.ref,"script_ref":self.w.ref}
        self.w.shots["S001"]=clip
        report_path=Path(self.w.reports["S001"]["report_path"]);report=_read(report_path)
        report["media_sha256"]=clip["media_sha256"];_write(report_path,report)
        self.w.reports["S001"]["report_sha256"]=digest_json(report)
        plan=self.w.repair.prepare("S001",["action_sequence"],"offline cut black tail after action", "offline editing test")
        def rereview(sid):self.w.reports[sid]={"status":"UNKNOWN","mode":"offline mocked semantic recheck"}
        with patch.object(self.w,"review_shot",side_effect=rereview),patch.object(self.w,"acceptance_gate",return_value={"passed":False}):
            result=self.w.repair.edit("S001",plan["repair_id"],.5,"")
        edited=self.w.shots["S001"]
        self.assertNotEqual(edited["media_sha256"],clip["media_sha256"])
        self.assertEqual(job_path.read_bytes(),original_job)
        self.assertEqual(validate_lineage(edited["media_lineage"],output_path=edited["media_path"],output_sha256=edited["media_sha256"]),job)
        self.assertFalse(result["acceptance"]["passed"])
        receipt=_read(edited["media_lineage"]["path"]);self.assertNotIn("provider_response",receipt)
        Path(edited["media_path"]).write_bytes(b"offline deliberate tamper")
        with self.assertRaises(ValueError):validate_lineage(edited["media_lineage"],output_path=edited["media_path"],output_sha256=edited["media_sha256"])

    def test_model_repairs_failed_candidate_then_only_exports_after_pass(self):
        self._exercise_model_repair()

    def test_native_pi_sdk_repairs_and_respects_the_same_export_gate(self):
        self._exercise_model_repair(native_pi=True)

    def test_failed_retake_stops_both_runtimes_with_the_second_verdict(self):
        import io
        from contextlib import redirect_stderr
        for native in (False, True):
            with self.subTest(native_pi=native), redirect_stderr(io.StringIO()) as console:
                self._exercise_model_repair(native_pi=native, retry_passes=False)
                self.assertIn("offline second verdict", console.getvalue())

    def _exercise_model_repair(self,native_pi=False,retry_passes=True):
        from tempfile import TemporaryDirectory
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        diagnostic=root/"offline-diagnostic.mp4"
        subprocess.run([str(find_ffmpeg()),"-nostdin","-v","error","-f","lavfi","-i",
                        "color=c=blue:s=160x240:r=10:d=1","-c:v","libx264",str(diagnostic)],check=True)
        original=root/"offline-initial.mp4"
        subprocess.run([str(find_ffmpeg()),"-nostdin","-v","error","-f","lavfi","-i",
                        "color=c=red:s=160x240:r=10:d=1","-c:v","libx264",str(original)],check=True)
        submissions=[];mode={"repaired":False};old_reports={}
        class Runner:
            def __init__(self,config):pass
            def submit(self,request,metadata):
                sid=metadata["shot_id"];repair=metadata.get("acceptance_repair")
                folder=root/"offline-jobs"/(sid+("-repair" if repair else "-initial"));folder.mkdir(parents=True)
                media=folder/"video.mp4";media.write_bytes((diagnostic if repair else original).read_bytes())
                job={"schema_version":"bailian-video-job/1","base_url":"offline-mock","request":request,"metadata":metadata}
                job.update(request_sha256=digest_json({k:job[k] for k in ("base_url","request","metadata")}),task_id=folder.name,status="SUCCEEDED",download_sha256=file_sha256(media),local_video_path=str(media))
                _write(folder/"job.json",job);submissions.append((sid,bool(repair)))
                if repair:mode["repaired"]=True
                return {**job,"manifest_path":str(folder/"job.json")}
            def resume(self,path,wait=True):return {**_read(path),"manifest_path":path}
        def offline_review(w,sid,**kwargs):
            clip=w.shots[sid];folder=w.directory/"mock-reports"/(sid+("-repair" if clip.get("repair_id") else "-initial"))
            inspection=inspect_media(clip["media_path"],folder/"inspection",interval_s=1,max_frames=3)
            contract=create_contract(w.project,sid,width=160,height=240)
            status="PASS" if sid!="S001" or mode["repaired"] and retry_passes else "FAIL"
            report={"schema_version":"offline-mock-report/1","shot_id":sid,"status":status,"task_complete":status=="PASS",
                    "project_sha256":digest_json(w.project),"media_sha256":clip["media_sha256"],"inspection_sha256":digest_json(inspection),
                    "results":[{"criterion_id":r["criterion_id"],"required":r["required"],
                                "status":status if r["criterion_id"]=="action_sequence" else "PASS",
                                "reason":"offline second verdict" if clip.get("repair_id") else "offline first verdict"} for r in contract["requirements"]]}
            _write(folder/"contract.json",contract);_write(folder/"report.json",report)
            if sid=="S001" and not mode["repaired"]:old_reports[str(folder/"report.json")]=digest_json(report)
            return {"shot_id":sid,"status":status,"media_accepted":status=="PASS","report_path":str(folder/"report.json"),
                    "report_sha256":digest_json(report),"inspection_path":inspection["manifest_path"],
                    "unresolved":["action_sequence"] if status=="FAIL" else []}
        class Model:
            agent_backend="offline_legacy"
            def complete_json(self,messages):
                entries=[json.loads(m["content"]) for m in messages if m["role"]=="user"]
                state=[e["state"] for e in entries if e.get("type")=="state_snapshot"][-1]
                args={}
                if not state["project_ready"]:tool="plan_production"
                elif state["generate_needed"]:tool="generate_shot";args={"shot_id":state["generate_needed"][0]}
                elif not state["acceptance"]["passed"]:
                    if not state["repair_plans"]:
                        tool="prepare_shot_repair";args={"shot_id":"S001","criterion_ids":["action_sequence"],"instruction":"offline explicit corrected action sequence","reason":"offline observed failure"}
                    else:tool="regenerate_shot";args={"shot_id":"S001","repair_id":state["repair_plans"][-1]["repair_id"]}
                elif not state["review_and_export_done"]:tool="review_and_export"
                else:return {"data":{"type":"finish","outcome":"completed","summary":"offline completion claim"}}
                return {"data":{"type":"tool","tool":tool,"arguments":args,"summary":"offline model-directed action"}}
        ids=[s["id"] for s in self.project["shots"]]
        plan={"plan_id":"offline-plan","new_shots":ids,"retake_shots":[],"reuse_shots":[],"acceptance_loop":acceptance_policy(ids,repairs_per_shot=1)}
        model=Model()
        if native_pi:
            from pianchang.bailian_text import TextConfig
            model.agent_backend="pi";model.config=TextConfig(api_key="offline-no-network",model="offline-native-video")
            counter=[0]
            def exchange(payload):
                state=None
                for message in payload["messages"]:
                    if message.get("role") not in {"user","tool"}:continue
                    text=message.get("content") or "{}"
                    if isinstance(text,list):text="".join(p.get("text","") for p in text if p.get("type")=="text")
                    try:value=json.loads(text)
                    except ValueError:continue
                    if isinstance(value,dict):state=value.get("current_state",value.get("state",state))
                if isinstance(state,dict) and "state" in state:state=state["state"]
                decision=model.complete_json([{"role":"user","content":json.dumps({"type":"state_snapshot","state":state})}])["data"]
                name=decision.get("tool","finish_task")
                arguments=decision.get("arguments") if decision["type"]=="tool" else {"outcome":decision["outcome"],"summary":decision["summary"]}
                counter[0]+=1
                return {"id":"explicit-offline-native-video-"+str(counter[0]),"model":"offline-native-video","created":0,
                    "choices":[{"index":0,"message":{"role":"assistant","content":None,"tool_calls":[{"id":"offline-native-call-"+str(counter[0]),
                        "type":"function","function":{"name":name,"arguments":json.dumps(arguments)}}]},"finish_reason":"tool_calls"}],
                    "usage":{"prompt_tokens":10,"completion_tokens":10,"total_tokens":20}}
            model.pi_exchange=exchange
        with patch("pianchang.video_collab_agent.VideoRunner",Runner),patch.object(VideoWorkspace,"_review",offline_review):
            result=run_video_agent(script=self.script,script_ref=self.w.ref,directory=root/"production",text_config=None,
                video_config=VideoConfig(),template_project=self.project,client=model,shooting_plan=plan,max_video_generations=6)
        if native_pi:
            self.assertEqual(result["loop"]["backend"],"pi")
            messages=[json.loads(m["content"]) for m in _read(root/"production/agent/run.json")["messages"]]
            errors=[m.get("error") for m in messages if m.get("ok") is False]
        else:
            journal=[json.loads(x) for x in (root/"production/agent/events.jsonl").read_text(encoding="utf8").splitlines()]
            errors=[e["data"].get("error") for e in journal if e["name"]=="tool_result" and not e["data"].get("ok")]
        self.assertEqual(submissions,[("S001",False),("S002",False),("S003",False),("S001",True)])
        for path,sha in old_reports.items():self.assertEqual(digest_json(_read(path)),sha)
        if not retry_passes:
            self.assertEqual(result["loop"]["reason"], "acceptance_retake_limit", (result["loop"], errors))
            self.assertIn("offline second verdict", result["loop"]["summary"])
            self.assertIsNone(result["production"])
            return
        self.assertTrue(result["loop"]["task_complete"],(result["loop"],errors))
        self.assertTrue(result["production"]["media_accepted"])
        self.assertTrue(Path(result["production"]["preview"]["preview_path"]).is_file())
        self.assertTrue(self.tracer.verify(_read(root/"production/agent/run.json")["trace_context"]["trace_id"])["ok"])


if __name__ == "__main__":unittest.main()
