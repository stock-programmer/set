"""Offline recovery of a content review under the original workbench commission."""
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pianchang.app_service import write_json
from pianchang.content_review import VERSION, STATE_AUDIT_VERSION
from pianchang.review_dispatch import ReviewNotDispatched
from pianchang.review_recovery import owner_for, poll_review_retries, review_retry
from pianchang.studio_recovery import run_review_job
from pianchang.story_spec import digest_json
from trace_system import current_context, event, get_tracer, span, use_context


class ReviewRecoveryTests(unittest.TestCase):
    def failed_wire(self, binding):
        with self.assertRaises(ReviewNotDispatched), span("bailian.vision.http",kind="http"):
            ctx = current_context()
            wire = {**binding, "trace_id":ctx["trace_id"],"span_id":ctx["span_id"]}
            event("review.http_bound",binding,kind="http")
            event("http.request",{"method":"POST","url":binding["url"],"wire_body_sha256":binding["wire_body_sha256"],
                                  "body":{"model":"eye"}},kind="http")
            event("http.connect_started",{"connection":1},kind="http")
            event("http.error",{"connections":1,"dispatch":"not_sent","phase":"start_tls","origin_write_started":False,
                                "review":binding},kind="http")
            raise ReviewNotDispatched("offline")
        return wire

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        tracer = get_tracer(self.root/"traces")
        self.enterContext(use_context(None,tracer=tracer))
        self.enterContext(tracer.trace("test.review.recovery"))
        store = self.root/"outputs/collaborations/assets.sqlite3"
        pid, cid, run_id, sid = "1"*16, "2"*32, "3"*32, "S001"
        self.view = {"store_path":str(store),"project_id":pid,"run_id":run_id,"project":{"script_source":{"script":{}}},
                     "script_ref":{"revision_id":"r1"},"story_id":"story-"+run_id}
        self.work = store.parent/"runs"/run_id/("video-spec-"+cid)
        self.work.mkdir(parents=True)
        media = self.work/"clip.mp4"
        media.write_bytes(b"selected fixture")
        sha = hashlib.sha256(media.read_bytes()).hexdigest()
        self.clip = {"media_sha256":sha,"media_path":str(media),"shot_id":sid}
        self.snapshot = {"active":False,"available":{sid:{"clip":self.clip}},"request_id":cid,"job_id":"a"*32,
                         "project_sha256":digest_json(self.view["project"]),"sha256":"c"*64}
        self.pointer = {"request_id":cid,"sha256":self.snapshot["sha256"]}
        self.owner = {"id":"b"*32,"kind":"studio_review","project_id":pid,"status":"succeeded"}
        self.order = {"id":cid,"body":{"shooting_plan":{"plan_id":"frozen"},"execution_limits":{"max_model_calls":6}}}
        self.run = {"shooting_plan":self.order["body"]["shooting_plan"],"steps":3,"limits":{"max_steps":6}}
        write_json(self.work/"agent/run.json",self.run)
        self.service = SimpleNamespace(tracer=tracer,lock=threading.RLock(),state={"jobs":[self.owner]},submit=Mock(),
                                       text_config=None,video_config=None,
                                       vision_config=SimpleNamespace(model="eye",base_url="https://review.invalid/v1",api_key="offline"))
        self.enterContext(patch("pianchang.studio_retakes._latest_request",return_value=(self.order,None)))
        self.base = {"video":{"sha256":sha,"path":str(media)},"references":[],"criteria":[{"criterion_id":"look"}],
                     "context":{"shot_id":sid},"model":"eye","endpoint":"https://review.invalid/v1"}
        self.folder = self.work/"cv2"/sid/"model"/digest_json(self.base)[:12]
        self.call = self.folder/"call.json"
        attempt = {"id":"e"*32,"input_sha256":digest_json(self.base),"commission_id":cid,
                   "call_path":str(self.call),"purpose":"vision.review"}
        url = self.base["endpoint"]+"/chat/completions"
        binding = {**attempt,"method":"POST","url":url,"wire_body_sha256":"d"*64}
        with use_context(None,tracer=tracer), tracer.trace("agent.task.studio_review",inputs={"body":{"continuation":self.pointer}},
                attributes={"job_id":self.owner["id"]}):
            self.owner["trace_id"] = current_context()["trace_id"]
            self.wire = self.failed_wire(binding)
        self.record = {"status":"submitted_or_unknown","attempt":attempt,"request":self.base,"wire":self.wire,
                       "input_sha256":digest_json(self.base)}
        write_json(self.call,self.record)
        self.receipt = {**{k:self.base[k] for k in ("video","references","criteria","context")},"execution_status":"execution_error",
                        "rows":[{"criterion_id":"look","status":"UNKNOWN"}],"request_sha256":digest_json(self.base),
                        "dispatch_evidence":{"status":"not_sent",**self.wire},"trace_id":self.owner["trace_id"]}
        self.receipt_path = self.folder/("undispatched-"+"f"*16+".json")
        self.report_path = self.work/"cv2"/sid/"report-1/report.json"
        self.save_report()

    def save_report(self, status="UNKNOWN"):
        sha = self.clip["media_sha256"]
        write_json(self.receipt_path,self.receipt)
        source = {"project_sha256":self.snapshot["project_sha256"],"media_sha256":sha,
                  "selected_media":{"S001":{"path":self.clip["media_path"],"sha256":sha}},
                  "receipts":[{"path":str(self.receipt_path),"sha256":digest_json(self.receipt)}]}
        report = {"content_review_version":VERSION,"state_audit_version":STATE_AUDIT_VERSION,
                  "project_sha256":self.snapshot["project_sha256"],"shot_id":"S001","media_sha256":sha,
                  "content_evidence_sha256":digest_json(source),"results":[{"criterion_id":"look","status":status,"required":True}]}
        write_json(self.report_path.parent/"content.json",source)
        write_json(self.report_path,report)
        write_json(self.work/"reports.json",{"S001":{"report_path":str(self.report_path),"report_sha256":digest_json(report)}})

    def scope(self):
        return review_retry(self.service,self.view,self.snapshot,self.owner)

    def internal_fixture(self):
        pid,cid = self.view["project_id"],self.snapshot["request_id"]
        self.service.root = self.root
        self.view["spec"] = {"revision_id":"spec-1"}
        self.snapshot.update(retake_stop={"reason":"acceptance_retake_limit"},
                             spec_ref=self.view["spec"],script_ref=self.view["script_ref"])
        self.order["body"].update(script=self.view["script_ref"],template=self.view["spec"])
        self.run["shooting_plan"]["acceptance_loop"] = {"autonomy_version":"frozen-policy"}
        write_json(self.work/"agent/run.json",self.run)
        self.receipt.pop("dispatch_evidence")
        self.save_report()
        source = {"id":self.snapshot["job_id"],"kind":"studio_video","status":"failed",
                  "project_id":pid,"message_id":cid,"run_id":self.view["run_id"]}
        self.service.state["jobs"].insert(0,source)
        folder = self.root/"outputs/workbench-loop"
        driver_id = "4"*32
        write_json(folder/"watch/state.json",{"latest":{source["id"]:"origin"},"events":{
            "origin":{"job_id":source["id"],"kind":"studio_video","run_id":driver_id}}})
        driver_file = folder/driver_id/"run.json"
        write_json(driver_file,{"id":driver_id,"role":"video","project_id":pid,"commission_id":cid,
                                "origin_job_id":source["id"],"submitted":[source["id"]]})
        scope = {"key":"review-internal-cuts-fixture","token":"4"*64,"criteria":{"S001":["look"]},
                 "internal_cuts":{"S001":{"cut":"fixture"}},"urls":[],"allowed":{}}
        legacy = self.enterContext(patch("pianchang.legacy_sound_review.legacy_sound_scope",return_value=None))
        selector = self.enterContext(patch("pianchang.internal_review_scope.internal_cut_scope",return_value=scope))
        self.enterContext(patch("pianchang.studio_assets.studio_project_for_video",return_value=self.view))
        self.enterContext(patch("pianchang.studio_recovery.read_incomplete_production",return_value=self.snapshot))
        return source,driver_file,legacy,selector,scope

    def test_internal_cut_requires_current_terminal_sidecar_order_and_prioritizes_sound(self):
        source,driver_path,legacy,selector,scope = self.internal_fixture()
        self.assertEqual(self.scope(),scope)
        self.assertEqual(selector.call_args.args[-1],3)
        legacy.return_value = {"key":"prior-sound"}
        self.assertEqual(self.scope(),legacy.return_value)
        legacy.return_value = None
        selector.reset_mock()
        for changes in ({"cancel_requested":True},{"trace_incomplete":True},{"status":"running"},{"run_id":"other"}):
            with patch.dict(source,changes):
                self.assertIsNone(self.scope())
        with patch.dict(self.snapshot,{"unknown_shots":["S001"]}):
            self.assertIsNone(self.scope())
        with patch.dict(self.view,{"pending_script_ref":{"revision_id":"new"}}):
            self.assertIsNone(self.scope())
        with patch("pianchang.review_recovery.current_reports",side_effect=[None,{"S001":("old",[],set())}]):
            self.assertIsNone(self.scope())
        driver_path.unlink()
        self.assertIsNone(self.scope())
        selector.assert_not_called()

    def test_internal_cut_poll_and_worker_revalidate_before_local_or_paid_review(self):
        source,driver_path,legacy,selector,scope = self.internal_fixture()
        pid,cid = self.view["project_id"],self.snapshot["request_id"]
        self.run["steps"] = self.run["limits"]["max_steps"]
        write_json(self.work/"agent/run.json",self.run)
        with patch.dict(scope,{"urls":["https://review.invalid/v1/chat/completions"]}):
            self.assertIsNone(self.scope())
        self.assertEqual(selector.call_args.args[-1],0)
        probe = self.enterContext(patch("pianchang.review_recovery._reachable"))
        recovery = {"id":"9"*32,"kind":"studio_review","project_id":pid,"status":"queued",
                    "review_request_id":cid,"_idempotency_key":scope["key"]}
        self.service.submit.return_value = recovery
        body = {"continuation":self.pointer,"retry_observation":scope["token"]}
        other = {"id":"3"*32,"kind":"asset_regenerate","project_id":pid,"status":"running"}
        self.service.state["jobs"].append(other)
        seen = {}
        poll_review_retries(self.service,seen)
        self.service.submit.assert_not_called()
        other["status"] = "succeeded"
        poll_review_retries(self.service,seen)
        self.service.submit.assert_called_once_with("studio_review",pid,body,scope["key"])
        self.service.state["jobs"].append(recovery)
        poll_review_retries(self.service,{})
        self.service.submit.assert_called_once()
        probe.assert_not_called()
        self.enterContext(patch("pianchang.studio_recovery.prepare_continuation",return_value={"shots":{"S001":self.clip}}))
        self.enterContext(patch("pianchang.collab_store.CollabStore"))
        self.enterContext(patch("pianchang.video_collab_agent.VideoWorkspace"))
        self.enterContext(patch("pianchang.video_collab_agent.analyze_impact"))
        apply = self.enterContext(patch("pianchang.internal_review_recovery.apply_internal_cuts",return_value=1))
        with patch.dict(recovery,{"review_request_id":"different"}),self.assertRaisesRegex(ValueError,"原制作委托"):
            run_review_job(self.service,recovery,body)
        with patch.dict(scope,{"token":"new-fingerprint"}),self.assertRaisesRegex(ValueError,"已更新"):
            run_review_job(self.service,recovery,body)
        apply.assert_not_called()
        result = run_review_job(self.service,recovery,body)
        apply.assert_called_once()
        self.assertEqual(result["review_retry"]["completed_observations"],1)
        self.assertTrue(result["action_required"])

    def test_sequence_assembly_uses_existing_observations_without_model_budget(self):
        source,_,_,cuts,cut_scope = self.internal_fixture()
        assembly = {"key":"review-sequence-fixture","token":"5"*64,"criteria":{"S001":["look"]},
                    "allowed":{},"urls":[],"sequence_assembly":{"S001":{"bound":"fixture"}}}
        select = self.enterContext(patch("pianchang.sequence_review_recovery.sequence_assembly_scope",return_value=assembly))
        self.assertEqual(self.scope(),cut_scope)
        select.assert_not_called()
        cuts.return_value = None
        self.run["steps"] = self.run["limits"]["max_steps"]
        write_json(self.work/"agent/run.json",self.run)
        self.assertEqual(self.scope(),assembly)
        self.assertEqual(select.call_args.args[-1],0)
        with patch.dict(assembly,{"urls":["https://review.invalid/v1/chat/completions"]}):
            self.assertIsNone(self.scope())
        pid,cid = self.view["project_id"],self.snapshot["request_id"]
        body = {"continuation":self.pointer,"retry_observation":assembly["token"]}
        job = {"id":"5"*32,"kind":"studio_review","project_id":pid,"status":"running",
               "review_request_id":cid,"_idempotency_key":assembly["key"]}
        self.service.submit.return_value = job
        with patch("pianchang.review_recovery._reachable") as probe:
            poll_review_retries(self.service,{})
        self.service.submit.assert_called_once_with("studio_review",pid,body,assembly["key"])
        probe.assert_not_called()
        self.service.state["jobs"].append(job)
        self.enterContext(patch("pianchang.studio_recovery.prepare_continuation",return_value={"shots":{"S001":self.clip}}))
        self.enterContext(patch("pianchang.collab_store.CollabStore"))
        self.enterContext(patch("pianchang.video_collab_agent.VideoWorkspace"))
        self.enterContext(patch("pianchang.video_collab_agent.analyze_impact"))
        def assemble(workspace,scope):
            self.assertEqual(scope,assembly)
            with self.assertRaisesRegex(ValueError,"不占用模型额度"):
                workspace.reserve_content_review("fixture")
            return 1
        with patch("pianchang.sequence_review_recovery.apply_sequence_assembly",side_effect=assemble) as local:
            with patch.dict(job,{"review_request_id":"wrong"}),self.assertRaisesRegex(ValueError,"原制作委托"):
                run_review_job(self.service,job,body)
            local.assert_not_called()
            result = run_review_job(self.service,job,body)
            local.assert_called_once()
        self.assertEqual(result["review_retry"]["completed_observations"],1)

    def test_only_current_unsent_unresolved_observation_can_queue_within_budget(self):
        self.assertEqual(self.scope()["allowed"],{str(self.call):"e"*32})
        for change in ({"status":"completed"},{"attempt":{**self.record["attempt"],"call_path":"wrong"}},
                       {"wire":{**self.wire,"trace_id":"0"*32}}):
            with self.subTest(change=change):
                write_json(self.call,{**self.record,**change})
                self.assertIsNone(self.scope())
        write_json(self.call,self.record)
        self.save_report("PASS")
        self.assertIsNone(self.scope())
        self.save_report()
        self.service.vision_config.model = "different-observer"
        self.assertIsNone(self.scope())
        self.service.vision_config.model = "eye"
        for change in ({"steps":6},{"pending_tool":{"tool":"generate_shot"}},{"trace_incomplete":True}):
            write_json(self.work/"agent/run.json",{**self.run,**change})
            self.assertIsNone(self.scope())
        write_json(self.work/"agent/run.json",self.run)
        original = self.receipt.pop("dispatch_evidence")
        self.save_report()
        self.assertIsNone(self.scope())
        self.receipt["dispatch_evidence"] = original
        self.save_report()
        self.owner.update(cancel_requested=True)
        self.assertIsNone(self.scope())
        self.owner.pop("cancel_requested")
        Path(self.clip["media_path"]).write_bytes(b"unrelated")
        self.assertIsNone(self.scope())

    def test_workbench_poll_claims_once_and_worker_revalidates_current_report(self):
        scope = self.scope()
        body = {"continuation":self.pointer,"retry_observation":scope["token"]}
        recovery = {"id":"9"*32,"kind":"studio_review","project_id":self.view["project_id"],"status":"queued",
                    "_idempotency_key":scope["key"]}
        self.enterContext(patch("pianchang.studio_assets.studio_project_for_video",return_value=self.view))
        self.enterContext(patch("pianchang.studio_recovery.read_incomplete_production",return_value=self.snapshot))
        probe = self.enterContext(patch("pianchang.review_recovery._reachable",side_effect=[False,True]))
        seen = {}
        poll_review_retries(self.service,seen)
        poll_review_retries(self.service,seen)
        self.service.submit.assert_not_called()
        self.assertEqual(probe.call_count,1)
        signature, _ = seen[self.view["project_id"]]
        seen[self.view["project_id"]] = (signature,0)
        poll_review_retries(self.service,seen)
        self.service.submit.assert_called_once_with("studio_review",self.view["project_id"],body,scope["key"])
        self.service.state["jobs"].append(recovery)
        poll_review_retries(self.service,{})
        self.service.submit.assert_called_once()
        self.assertIsNone(owner_for(self.service.state["jobs"],self.view["project_id"]))
        self.enterContext(patch("pianchang.studio_recovery.prepare_continuation",return_value={"shots":{"S001":self.clip}}))
        self.enterContext(patch("pianchang.collab_store.CollabStore"))
        self.enterContext(patch("pianchang.video_collab_agent.VideoWorkspace"))
        self.enterContext(patch("pianchang.video_collab_agent.analyze_impact"))
        summary = self.enterContext(patch("pianchang.content_review.run_content_reviews"))
        summary.side_effect = lambda ws,**kwargs:write_json(self.call,{**self.record,"status":"completed"})
        result = run_review_job(self.service,recovery,body)
        self.assertEqual(result["review_retry"],{"completed_observations":1,"failed_criteria":{},"pending_criteria":{"S001":["look"]}})
        self.assertTrue(result["action_required"])
        with self.assertRaisesRegex(ValueError,"未发送回执已更新"):
            run_review_job(self.service,recovery,body)
        summary.assert_called_once()
        self.assertEqual(summary.call_args.kwargs["publish_shots"],["S001"])

    def test_original_production_and_resumed_pi_worker_use_the_same_scoped_job(self):
        tracer = self.service.tracer
        cid = self.snapshot["request_id"]
        binding = {k:v for k,v in self.wire.items() if k not in {"trace_id","span_id"}}
        with use_context(None,tracer=tracer), tracer.trace("agent.task.video",inputs={"request":{"message_id":cid}},
                                                           attributes={"correlation_id":self.view["run_id"]}):
            wire = self.failed_wire(binding)
        write_json(self.call,{**self.record,"wire":wire})
        self.receipt.update(dispatch_evidence={"status":"not_sent",**wire},trace_id=wire["trace_id"])
        self.save_report()
        self.owner.update(id=self.snapshot["job_id"],kind="studio_video",message_id=cid,status="failed",
                          worker_trace_id=wire["trace_id"])
        self.assertIsNotNone(self.scope())
        self.owner.update(kind="pi_video",id="7"*32,commission_id=cid,commission_job_id=self.snapshot["job_id"])
        self.assertIsNotNone(self.scope())
        self.owner.update(commission_job_id="0"*32)
        self.assertIsNone(self.scope())
