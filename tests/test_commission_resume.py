"""Offline recovery of a frozen commission stopped before its first Pi tool."""
import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pianchang.app_service import read_json, write_json
from pianchang.bailian_text import TextConfig
from pianchang.bailian_video import VideoConfig
from pianchang.collaboration_workflow import Collaboration, VideoAgentStopped, dep, open_ref
from pianchang.script_collab_agent import publish_script_revision
from pianchang.story_spec import digest_json
from pianchang.video_autonomy import VideoAutonomy, recoverable_order
from pianchang.video_collab_agent import run_video_agent
from pianchang.video_repair import acceptance_policy
from trace_system import get_tracer, use_context


class OfflineCommissionTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.enterContext(patch.dict(os.environ, {"AGENT_TRACE_DIR": str(self.root / "offline-traces")}))
        (self.root / ".env").write_text("# offline fixture\n", encoding="utf-8")
        self.project = read_json(Path(__file__).resolve().parents[1] / "fixtures/story_project.json")
        tracer = get_tracer(self.root / "offline-traces")
        self.enterContext(tracer.storage_session())
        self.enterContext(use_context(None, tracer=tracer))
        self.enterContext(tracer.trace("offline.commission_resume", inputs={"mode": "offline"}))
        ids = [shot["id"] for shot in self.project["shots"]]
        self.plan = {"plan_id": "1" * 32, "project_sha256":digest_json(self.project),
                     "new_shots": ids, "retake_shots": [], "reuse_shots": [],
                     "acceptance_loop": acceptance_policy(ids)}


class UnpreparedCommissionResumeTests(OfflineCommissionTestCase):
    def setUp(self):
        super().setUp()
        from pianchang.video_execution import execution_limits
        self.plan["execution_limits"] = execution_limits(self.plan, 9, len(self.project["shots"]))
        self.session = Collaboration(self.root / "collaborations", "a" * 32, env_file=self.root / ".env")
        self.session.write(id=self.session.run_id, stages=[], productions=[])
        self.session.store.subscribe("video", "script.revision_published", self.session.story)
        script = self.project["script_source"]["script"]
        published = publish_script_revision(
            self.session.store, script, {"status": "PASS", "script_sha256": digest_json(script)},
            asset_id=self.session.story + "/script", story_id=self.session.story, correlation_id=self.session.run_id,
        )
        self.script = open_ref(self.session.store, published)
        spec = self.session.store.publish_asset(
            self.session.story + "/spec", self.project, kind="production_spec", producer="commissioner",
            story_id=self.session.story, correlation_id=self.session.run_id,
            dependencies=[dep(self.script, "target_script")],
        )
        self.order = self.session.commission(self.script, spec, None, 9, shooting_plan=self.plan)
        self.cid = self.order["message_id"]
        self.session.store.claim("video", message_id=self.cid)
        self.session.store.ack("video", self.cid, "blocked", result={"reason": "offline ConnectTimeout"})
        self.work = self.session.folder / ("video-spec-" + self.cid)
        self.record = {
            "backend": "pi", "status": "needs_input", "result": {"reason": "pi_stopped"},
            "steps": 1, "limits": {"max_steps": self.order["payload"]["execution_limits"]["max_model_calls"],
                                  "max_video_generations": 9},
            "shooting_plan": self.plan, "video_generations": 0, "external_model_calls": 0,
            "messages": [], "reserved_shots": [], "repair_reservations": {},
        }
        self.autonomy = {
            "steps": 0, "video_generations": 0, "provider_blockers": {}, "coordination": [],
            "coordination_errors": {}, "candidate_selection": [], "review_attempts": {},
            "specification_blockers": {}, "repair_reservations": {},
            "actionable": [], "blocked": [], "exhausted": [],
        }
        self.save_checkpoint()

    def save_checkpoint(self, record=None, autonomy=None):
        write_json(self.work / "agent/run.json", self.record if record is None else record)
        write_json(self.work / "autonomy.json", self.autonomy if autonomy is None else autonomy)

    def test_explicit_resume_reuses_original_commission_inputs_and_remaining_budget(self):
        self.assertFalse(recoverable_order(self.work))
        before = self.session.store.get_message(self.cid)
        with patch("pianchang.video_collab_agent.run_video_agent",
                   return_value={"production": None, "loop": {"summary": "offline model unavailable"}}) as run:
            with self.assertRaises(VideoAgentStopped):
                self.session.worker(self.cid, resume=True, explicit_continuation=True)
        run.assert_called_once()
        args = run.call_args.kwargs
        self.assertTrue(args["resume"])
        self.assertEqual(args["directory"], self.work)
        self.assertEqual(args["shooting_plan"], self.plan)
        self.assertEqual(args["max_video_generations"], 9)
        self.assertEqual(args["max_steps"], self.record["limits"]["max_steps"])
        self.assertEqual(args["inbound_message"]["message_id"], self.cid)
        self.assertEqual(read_json(self.work / "agent/run.json"), self.record)
        self.assertEqual(self.session.store.get_message(self.cid)["payload"], before["payload"])
        self.assertEqual(self.session.store.get_message(self.cid)["deliveries"]["video"], "blocked")
        self.assertFalse((self.work / "project.json").exists())

    def test_implicit_or_ambiguous_work_is_rejected_before_agent_dispatch(self):
        cases = {
            "pending_tool": {"pending_tool": {"tool": "generate_shot", "effect": "video"}},
            "reservation": {"reserved_shots": ["S001"]},
            "used_video": {"video_generations": 1},
            "used_auxiliary_model": {"external_model_calls": 1},
            "repair_reservation": {"repair_reservations": {"repair-1": "S001"}},
            "tool_history": {"messages": [{"role": "assistant", "content": "tool decision"}]},
            "reconciled_tool": {"reconciled_operations": [{"operation": "unknown"}]},
            "changed_plan": {"shooting_plan": {**self.plan, "plan_id": "different"}},
            "steps_exhausted": {"steps": self.record["limits"]["max_steps"]},
        }
        with patch("pianchang.video_collab_agent.run_video_agent") as run:
            with self.assertRaisesRegex(ValueError, "须核实原任务"):
                self.session.worker(self.cid, resume=True)
            for label, mutation in cases.items():
                with self.subTest(label=label):
                    self.save_checkpoint({**copy.deepcopy(self.record), **mutation})
                    with self.assertRaisesRegex(ValueError, "须核实原任务"):
                        self.session.worker(self.cid, resume=True, explicit_continuation=True)
            self.save_checkpoint(autonomy={**self.autonomy, "provider_blockers": {"S001": {"kind": "unknown"}}})
            with self.assertRaisesRegex(ValueError, "须核实原任务"):
                self.session.worker(self.cid, resume=True, explicit_continuation=True)
            self.save_checkpoint()
            write_json(self.work / "clips.json", {"S001": {"task_id": "existing-task"}})
            with self.assertRaisesRegex(ValueError, "须核实原任务"):
                self.session.worker(self.cid, resume=True, explicit_continuation=True)
            run.assert_not_called()

    def test_planned_only_commission_requires_matching_inputs_and_unsent_decision_receipt(self):
        origin = {"trace_id":"b"*32, "span_id":"c"*32}
        impact = {"shots":[{"id":s["id"]} for s in self.project["shots"]]}
        result = {"type":"tool_result","tool":"plan_production","ok":True,"result":impact}
        self.record.update(steps=2, trace_context=origin, process_cleanup={"confirmed_exit":True},
                           messages=[{"role":"user","content":json.dumps(result)}])
        self.save_checkpoint()
        write_json(self.work/"project.json",self.project)
        write_json(self.work/"impact.json",impact)
        proof = {"trace_id":origin["trace_id"],"run_span_id":origin["span_id"],
                 "commission_id":self.cid,"run_id":self.session.run_id}
        evidence = {"trace_failed":False,"outstanding":False,"planner_not_sent":proof}
        with patch("tools.workbench_loop_observer._trace",return_value=evidence) as trace, \
                patch("pianchang.video_collab_agent.run_video_agent",
                      return_value={"production":None,"loop":{"summary":"offline"}}) as run:
            for invalid in ({**evidence,"outstanding":True},{**evidence,"trace_failed":True},
                            {**evidence,"planner_not_sent":{**proof,"run_span_id":"1"*32}},
                            {**evidence,"planner_not_sent":{**proof,"commission_id":"2"*32}}):
                trace.return_value = invalid
                with self.assertRaisesRegex(ValueError,"须核实原任务"):
                    self.session.worker(self.cid,resume=True,explicit_continuation=True)
            trace.return_value = evidence
            write_json(self.work/"impact.json",{**impact,"shots":["different"]})
            with self.assertRaisesRegex(ValueError,"须核实原任务"):
                self.session.worker(self.cid,resume=True,explicit_continuation=True)
            run.assert_not_called()
            write_json(self.work/"impact.json",impact)
            with self.assertRaises(VideoAgentStopped):
                self.session.worker(self.cid,resume=True,explicit_continuation=True)
            run.assert_called_once()
            self.assertEqual(run.call_args.kwargs["inbound_message"]["message_id"],self.cid)
            self.assertEqual(read_json(self.work/"agent/run.json")["video_generations"],0)

    def test_paid_decision_inherits_only_confirmed_original_candidates(self):
        from httpx import ConnectError
        from pi_bridge.runtime import PiLoop
        from pianchang.agent_loop import Tool
        from trace_system import current_context, event, span
        from tools.workbench_loop_observer import _planner_not_sent, _read_db, _trace

        sid = self.plan["new_shots"][0]
        script = self.order["payload"]["script"]
        session_file = self.root / "video.jsonl"
        session_file.write_text(json.dumps({"type":"session", "id":"test-video-session"})+"\n", encoding="utf-8")
        tracer = get_tracer(self.root / "outputs/traces")
        with use_context(None,tracer=tracer), tracer.trace("agent.task.video", inputs={"request":{"message_id":self.cid}},
                attributes={"correlation_id":self.session.run_id}), span("pi.loop.run"):
            origin = current_context()
            event("pi.sdk.ready",{"sessionId":"test-video-session","sessionFile":str(session_file)},kind="agent")
            with span("pi.loop.run"):
                event("pi.sdk.ready",{"sessionId":"other-role","sessionFile":str(self.root/"other.jsonl")},kind="agent")
                event("pi.run.stopped",{"type":"ValueError","steps":1},kind="error")
            try:
                with span("pi.model.exchange"), span("models.sdk.call"):
                    event("model.route",{"operation":"completion","route":{"model_id":"planner"}},kind="model")
                    event("http.request",{"method":"POST","url":"https://example.invalid/v1/chat/completions",
                        "body":{"model":"planner"},"wire_body_sha256":"1"*64},kind="http")
                    event("http.proxy.selected",{"mode":"direct","target_host":"example.invalid"},kind="http")
                    event("http.error",{"dispatch":"not_sent","phase":"connect_tcp","error_type":"ConnectError"},kind="http")
                    raise ConnectError("offline")
            except ConnectError:
                event("pi.model.failure",{"purpose":"decision","type":"ConnectError"},kind="error")
                event("pi.run.stopped",{"type":"ConnectError","steps":3},kind="error")
        proof = _trace(self.root, origin["trace_id"], origin["span_id"])["planner_not_sent"]
        self.assertEqual(proof["pi_session_id"], "test-video-session")
        with _read_db(self.root/"outputs/traces/trace.sqlite3") as db:
            self.assertIsNone(_planner_not_sent(db,origin["trace_id"]))
            self.assertEqual(_planner_not_sent(db,origin["trace_id"],pi_session_id="test-video-session"),proof)
        folder = self.root / "outputs/videos/a"
        folder.mkdir(parents=True)
        manifest, media = folder / "job.json", folder / "video.mp4"
        media.write_bytes(b"original recorded video")
        sha = hashlib.sha256(media.read_bytes()).hexdigest()
        metadata = {"shooting_plan_id":self.plan["plan_id"],"script_asset_ref":script,"shot_id":sid,
                    "dependency_sha256":"a"*64,"execution_key":digest_json({"order":str(self.work),"shot_id":sid,"repair_id":None})}
        signature = {"base_url":"https://example.invalid","request":{},"metadata":metadata}
        job = {**signature,"request_sha256":digest_json(signature),"status":"SUCCEEDED","task_id":"paid-once",
               "local_video_path":str(media),"download_sha256":sha,
               "provider_response":{"output":{"task_status":"SUCCEEDED","task_id":"paid-once"}}}
        write_json(manifest,job)
        clip = {"shot_id":sid,"task_id":"paid-once","script_ref":script,"origin_script_ref":script,
                "dependency_sha256":metadata["dependency_sha256"],"manifest_path":str(manifest),
                "generation_job_sha256":digest_json(job),"media_path":str(media),"media_sha256":sha}
        write_json(self.work/"project.json",self.project)
        write_json(self.work/"impact.json",{"shots":[{"id":sid}]})
        write_json(self.work/"clips.json",{sid:clip})
        self.record.update(steps=3, video_generations=1, reserved_shots=[sid], trace_context=origin,
                           process_cleanup={"confirmed_exit":True}, session_id="test-video-session",session_file=str(session_file),
                           result={"reason":"pi_stopped","task_complete":False,"steps":3,"video_generations":1},
                           messages=[{"role":"user","content":json.dumps({"type":"tool_result","tool":"generate_shot",
                                                                         "ok":True,"result":clip})}])
        self.save_checkpoint()
        with patch("pianchang.video_collab_agent.run_video_agent",
                   return_value={"production":None,"loop":{"summary":"offline recovered"}}) as run:
            for changed in ({"reserved_shots":[]},{"session_id":"other"},{"external_model_calls":1},
                            {"pending_tool":{"tool":"generate_shot"}},{"steps":4},{"messages":[]}):
                with self.subTest(changed=changed):
                    self.save_checkpoint({**self.record,**changed})
                    with self.assertRaisesRegex(ValueError,"须核实原任务"):
                        self.session.worker(self.cid,resume=True,explicit_continuation=True)
            self.save_checkpoint()
            write_json(manifest,{**job,"provider_response":{"output":{"task_status":"SUCCEEDED","task_id":"another"}}})
            with self.assertRaisesRegex(ValueError,"须核实原任务"):
                self.session.worker(self.cid,resume=True,explicit_continuation=True)
            write_json(manifest,job)
            media.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError,"须核实原任务"):
                self.session.worker(self.cid,resume=True,explicit_continuation=True)
            media.write_bytes(b"original recorded video")
            run.assert_not_called()
            from pianchang.video_autonomy import can_resume_paid_pi_order
            recovery = {"operation":{"effect":"video","tool":"generate_shot","arguments":{"shot_id":sid}},
                        "observation":{"budget_preserved":True,"new_video_posts":0,"blocked_shots":{},
                                       "outcomes":[{"status":"recovered_and_reviewed","new_video_posts":0,
                                                    "shot_id":sid,"task_id":"paid-once"}]}}
            recovered_record = {**self.record,"messages":[],"reconciled_operations":[recovery]}
            bindings = {"request_id":self.cid,"run_id":self.session.run_id,"script":script,
                        "output":folder.parent,"limits":self.plan["execution_limits"]}
            self.assertTrue(can_resume_paid_pi_order(self.work,self.plan,recovered_record,proof,**bindings))
            recovery["observation"]["outcomes"][0]["task_id"] = "unknown-task"
            self.assertFalse(can_resume_paid_pi_order(self.work,self.plan,recovered_record,proof,**bindings))
            # The original can remain selected after a paid retake. Both spends need receipts.
            with tempfile.TemporaryDirectory(dir=folder.parent) as retake:
                repaired_media = Path(retake)/"video.mp4"
                repaired_media.write_bytes(media.read_bytes())
                repaired_manifest = Path(retake)/"job.json"
                repair_id = "retake-1"
                repair_meta = {**metadata,"acceptance_repair":{"repair_id":repair_id},
                               "execution_key":digest_json({"order":str(self.work),"shot_id":sid,"repair_id":repair_id})}
                repair_signature = {**signature,"metadata":repair_meta}
                repaired_job = {**job,**repair_signature,"task_id":"second-task","local_video_path":str(repaired_media),
                                "provider_response":{"output":{"task_id":"second-task","task_status":"SUCCEEDED"}},
                                "request_sha256":digest_json(repair_signature)}
                write_json(repaired_manifest,repaired_job)
                repaired_clip = {**clip,"repair_id":repair_id,"manifest_path":str(repaired_manifest),
                                 "media_path":str(repaired_media),"generation_job_sha256":digest_json(repaired_job),
                                 "task_id":"second-task"}
                history_path = self.work/"repair-history.json"
                write_json(history_path,{"history":[{"shot_id":sid,"repair_id":repair_id,"source_clip":clip,
                                                     "result_clip":repaired_clip,"status":"reviewed"}]})
                receipt = {"type":"tool_result","tool":"regenerate_shot","ok":True,"result":{"shot_id":sid,"repair_id":repair_id}}
                retaken = {**self.record,"video_generations":2,"repair_reservations":{sid:1},
                           "result":{**self.record["result"],"video_generations":2},
                           "messages":[*self.record["messages"],{"role":"user","content":json.dumps(receipt)}]}
                self.assertTrue(can_resume_paid_pi_order(self.work,self.plan,retaken,proof,**bindings))
                self.assertFalse(can_resume_paid_pi_order(self.work,self.plan,self.record,proof,**bindings))
                history_path.unlink()
            with self.assertRaisesRegex(VideoAgentStopped,"offline recovered"):
                self.session.worker(self.cid,resume=True,explicit_continuation=True)
            run.assert_called_once()
            self.assertEqual(run.call_args.kwargs["directory"],self.work)
            self.assertEqual(read_json(self.work/"agent/run.json"),self.record)
        submit = Mock()
        loop = PiLoop(None,[Tool("generate_shot","offline",{},submit,effect="video")],self.work/"agent",
                      snapshot=dict,finish_check=dict,session_file=str(self.root/"another-session.jsonl"))
        loop.record = read_json(self.work/"agent/run.json")
        with self.assertRaisesRegex(ValueError,"授权次数已用完"):
            loop.execute("generate_shot",{"shot_id":sid},"duplicate")
        loop.record = {}  # a new worker loads the saved record only after its session binding passes
        with self.assertRaisesRegex(ValueError,"原会话不一致"):
            loop.run("offline",max_steps=self.record["limits"]["max_steps"],max_video_generations=9,
                     shooting_plan=self.plan,resume=True)
        submit.assert_not_called()
        self.assertEqual(read_json(self.work/"agent/run.json"),self.record)


class NativePiCommissionResumeTests(OfflineCommissionTestCase):
    def test_native_pi_resume_keeps_budget_without_prepared_project_or_video_submission(self):
        directory = self.root / "native-pi-work"
        calls, recoveries = [], []

        def fail_decision(*args, **kwargs):
            calls.append("offline decision")
            if len(calls) == 1:
                import httpx
                error = httpx.ConnectError("[Errno 2] No such file or directory")
                error.network_dispatched = False
                error.wire_receipt = {"dispatch":"not_sent","phase":"start_tls","error_type":"ConnectError"}
                raise error
            raise ValueError("offline ConnectTimeout")

        original_recover = VideoAutonomy.recover

        def observe_recovery(autonomy, record):
            self.assertEqual(record["steps"], 1)
            self.assertEqual(record["previous_result"]["reason"], "pi_stopped")
            result = original_recover(autonomy, record)
            recoveries.append(result)
            return result

        client = SimpleNamespace(config=TextConfig(api_key="offline-no-network", model="offline-model"),
                                 pi_exchange=fail_decision)
        script = self.project["script_source"]["script"]
        script_ref = {"asset_id": "offline/script", "revision_id": "r1", "sha256": digest_json(script)}
        with patch.object(VideoAutonomy, "recover", observe_recovery), \
             patch("pianchang.video_agent.VideoRunner") as video_runner, \
             patch("pianchang.video_collab_agent.VideoRunner", video_runner), \
             patch("pianchang.video_collab_agent.VideoWorkspace._review") as review:
            for expected_steps, resume in ((1, False), (2, True)):
                result = run_video_agent(
                    script=script, script_ref=script_ref, directory=directory,
                    text_config=client.config, video_config=VideoConfig(api_key="offline-no-network"),
                    template_project=self.project, client=client, shooting_plan=self.plan,
                    max_video_generations=9, max_steps=56, resume=resume,
                )
                checkpoint = read_json(directory / "agent/run.json")
                self.assertEqual(checkpoint["steps"], expected_steps)
                self.assertEqual(checkpoint["limits"], {"max_steps": 56, "max_video_generations": 9})
                self.assertEqual(checkpoint["video_generations"], 0)
                self.assertEqual(checkpoint["external_model_calls"], 0)
                self.assertEqual(checkpoint["shooting_plan"], self.plan)
                self.assertEqual(checkpoint["result"]["reason"], "pi_stopped")
                if expected_steps == 1:
                    self.assertIn("TLS 握手",checkpoint["result"]["summary"])
                    self.assertIn("尚未发送",checkpoint["result"]["summary"])
                else:
                    self.assertIn("offline ConnectTimeout",checkpoint["result"]["summary"])
                self.assertIsNone(result["production"])
                self.assertFalse((directory / "project.json").exists())
            video_runner.assert_not_called()
            review.assert_not_called()
        self.assertEqual(len(calls), 2)
        self.assertEqual(recoveries, [{"preparation_pending": True, "next_action": "plan_production",
                                      "budget_preserved": True, "new_video_posts": 0}])


if __name__ == "__main__":
    unittest.main()
