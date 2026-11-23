import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pianchang.app_service import read_json, write_json
from tools import workbench_loop_watch as watch
from tools.workbench_loop_input import answer_request
from trace_system import get_tracer, use_context


class FeedbackResumeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        tracer = get_tracer(self.root / "traces")
        self.enterContext(use_context(None, tracer=tracer))
        self.enterContext(tracer.trace("test.workbench.feedback.offline"))
        self.pid = "b" * 16
        self.source = {"id": "f"*32, "trace_id": "e"*32, "kind": "asset_production",
                       "project_id": self.pid, "status": "failed"}
        self.path = self.root / "outputs/web/state.json"
        self.items = [{"subject_id": f"object:{i}", "purpose": "prop", "job_id": self.source["id"],
                       "model_id": "old-model", "selected_pin": None} for i in range(6)]
        self.index = {"jobs": [self.source], "asset_work": {self.pid: {"items": {str(i):row for i,row in enumerate(self.items)}}}}
        write_json(self.path, self.index)
        self.dependency = {"status":"needs_input", "reason":"reference_image_model_access", "receipt_unknown":False,
                           "human_intervention":{"kind":"reference_image_model_access", "items":self.items,
                                                 "provider_code":"InvalidEndpointOrModel.NotFound"}}
        self.inspect = self.enterContext(patch("tools.workbench_loop_images.inspect_ark_image_route"))
        self.apply = self.enterContext(patch("tools.workbench_loop_images.apply_ark_image_route"))
        self.submit = self.enterContext(patch("tools.workbench_loop_voice.request_image_recovery"))
        self.enterContext(patch("tools.workbench_loop_dependency.recheck_dependency",return_value=self.dependency))
        self.watcher = self.daemon()
        self.watcher.register(self.source, include=True)

    def daemon(self):
        watcher = watch.Watcher(self.root)
        self.addCleanup(watcher.close)
        return watcher

    def advance(self):
        jobs = {j["id"]:j for j in self.index["jobs"]}
        key = self.watcher.state["latest"][self.source["id"]]
        self.watcher.process(key, self.watcher.state["events"][key], self.source, jobs)
        return watch.watch_status(self.root, self.source["id"])

    def test_successor_rejection_falls_back_without_six_submissions_or_prompt(self):
        first, second = "1"*32, "2"*32
        self.inspect.return_value = {"status":"candidate"}
        self.apply.side_effect = [{"status":"ready","model_id":"pro"},{"status":"ready","model_id":"flash"}]
        self.submit.side_effect = [first, second]
        self.assertEqual(self.advance()["image_recovery_job_id"], first)
        child = {"id":first, "kind":"asset_production","project_id":self.pid, "_image_recovery_from":self.source["id"],
                 "status":"failed","result":{"image_recovery":{"source_job_id":self.source["id"], "model_id":"pro",
                         "status":"model_rejected","provider_code":"ModelNotOpen"}}}
        self.index["jobs"].append(child)
        self.watcher = self.daemon()
        self.assertNotIn("human_request", self.advance())
        self.assertEqual(self.advance()["image_recovery_job_id"],second)
        self.assertEqual(self.inspect.call_args.args[1]["pro"], "ModelNotOpen")
        child2 = {**child,"id":second,"status":"succeeded","result":{"image_recovery":{"source_job_id":self.source["id"],
                      "model_id":"flash","status":"completed"}}}
        self.index["jobs"].append(child2)
        for item in self.items:
            item["selected_pin"]={"revision_id":"passed"}
        write_json(self.path,self.index)
        self.assertEqual(self.advance()["status"],"succeeded")
        self.advance()
        self.assertEqual(self.submit.call_count,2)

    def test_missing_permission_is_answered_once_for_exact_scope(self):
        self.inspect.side_effect = [{"status":"needs_input","reason":"provider_access"},{"status":"candidate"}]
        self.apply.return_value = {"status":"ready","model_id":"ep-fixed"}
        self.submit.return_value = "1"*32
        req = self.advance()["human_request"]
        self.advance()
        self.submit.assert_not_called()
        self.inspect.assert_called_once()
        body = {"job_id":self.source["id"],"request_id":req["id"],"answers":{"endpoint_id":"ep-fixed","confirm":False}}
        with self.assertRaises(KeyError):
            answer_request(self.root,"c"*16,body)
        for wrong in ({**body, "request_id":"0"*64}, {**body,"answers":{}}, {**body,"answers":{"endpoint_id":"https://other"}}):
            with self.assertRaises(ValueError):
                answer_request(self.root,self.pid,wrong)
        self.assertEqual(answer_request(self.root,self.pid,body)["status"],"accepted")
        self.watcher=self.daemon()
        self.assertNotIn("human_request",self.advance())
        self.assertEqual(self.inspect.call_args.kwargs["supplied_model"],"ep-fixed")
        self.submit.assert_called_once_with(self.root,self.source,"ep-fixed",answer_id=req["id"])
        self.assertEqual(answer_request(self.root,self.pid,body)["status"],"accepted")
        with self.assertRaises(ValueError):
            answer_request(self.root,self.pid,{**body,"answers":{"confirm":True}})
        self.assertEqual(read_json(self.watcher.folder/"answers"/(req["id"]+".json"))["answers"],{"endpoint_id":"ep-fixed"})

    def test_transient_directory_outage_does_not_request_human_or_generation(self):
        self.inspect.return_value = {"status":"retry_later","reason":"catalog_unavailable"}
        state = self.advance()
        self.assertEqual(state["status"],"handling")
        self.assertNotIn("human_request",state)
        self.watcher=self.daemon()
        self.advance()
        self.inspect.assert_called_once()
        self.submit.assert_not_called()

    def test_script_reply_reconciles_saved_answer_after_watcher_restart(self):
        from tools.workbench_loop_input import human_request
        job = {**self.source, "kind":"pi_script", "status":"interrupted", "story_job_id":"d"*32,
               "result":{"backend":"pi", "status":"needs_input", "reason":"script_creative_input"}}
        self.index["jobs"] = [job]
        write_json(self.path, self.index)
        self.watcher.register(job, include=True)
        question = human_request(job, "script_creative_input", {"topic":"name"}, "请补充名字", "收到后继续。",
                                 fields=[{"name":"answer","label":"姓名","type":"text"}])
        key = watch._signature(job)
        def process():
            self.watcher.process(key,self.watcher.state["events"][key],job,{job["id"]:job})
            return watch.watch_status(self.root, job["id"])
        with patch("tools.workbench_loop_script.script_question",return_value=question) as inspect, \
                patch("tools.workbench_loop_script.submit_script_reply") as submit:
            self.assertEqual(process()["human_request"]["id"],question["id"])
            submit.assert_not_called()
            answer_request(self.root,self.pid,{"job_id":job["id"],"request_id":question["id"],
                                                "answers":{"answer":"小林"}})
            self.watcher = self.daemon()
            inspect.return_value = None
            submit.return_value = {"message_id":"4"*32,"job_id":"6"*32,"session_id":"7"*32}
            self.assertEqual(process()["reason"],"script_input_delivered")
            self.assertNotIn("human_request",process())
            submit.assert_called_once_with(self.root,job,question,"小林")

    def test_old_unknown_job_only_retries_on_exact_not_sent_proof_and_reachable_route(self):
        first, second = "1"*32, "2"*32
        self.inspect.return_value = {"status":"candidate"}
        self.apply.return_value = {"status":"ready","model_id":"pro"}
        self.submit.side_effect = [first, second]
        self.advance()
        child = {"id":first, "kind":"asset_production","project_id":self.pid,"_image_recovery_from":self.source["id"],
                 "status":"failed","result":{"image_recovery":{"source_job_id":self.source["id"],"model_id":"pro",
                                                              "status":"stopped_unknown"}}}
        self.index["jobs"].append(child)
        record = self.watcher.state["events"][watch._signature(self.source)]
        with patch("pianchang.image_recovery.unsent_image_request") as evidence, patch("tools.workbench_loop_ark.probe_image_transport") as probe:
            evidence.return_value=None
            self.assertEqual(self.advance()["status"],"unknown")
            self.submit.assert_called_once()
            probe.assert_not_called()
            record["image_flow"]["next_check"]=0
            evidence.return_value={"dispatch":"not_sent"}
            probe.return_value={"status":"retry_later"}
            self.assertNotIn("human_request",self.advance())
            self.submit.assert_called_once()
            record["image_flow"]["next_check"]=0
            probe.return_value={"status":"reachable"}
            self.advance()
            self.advance()
            self.assertEqual(self.submit.call_count,2)
            self.submit.assert_called_with(self.root,self.source,"pro",after_not_sent=first)

    def test_wrong_subject_review_precedes_regeneration_or_human_prompt(self):
        original_child, inspection = "1"*32, "2"*32
        self.inspect.return_value = {"status":"candidate"}
        self.apply.return_value = {"status":"ready","model_id":"pro"}
        self.submit.side_effect = [original_child, inspection]
        self.advance()
        self.index["jobs"].append({"id":original_child, "kind":"asset_production", "project_id":self.pid,
            "_image_recovery_from":self.source["id"], "status":"failed",
            "result":{"image_recovery":{"source_job_id":self.source["id"],"model_id":"pro","status":"completed"}}})
        self.items[0].update(job_id=original_child,status="needs_attention")
        for row in self.items[1:]:
            row["selected_pin"] = {"revision_id":"passed"}
        write_json(self.path,self.index)
        with patch("pianchang.image_recovery.reinspectable_image_reviews",return_value=[{"revision_id":"r2"}]), \
                patch("pianchang.image_recovery.recoverable_image_reviews") as revisions:
            self.assertEqual(self.advance()["reason"],"image_review_reinspect")
            self.assertEqual(self.advance()["image_recovery_job_id"],inspection)
            self.submit.assert_called_with(self.root,self.source,"pro",after_reinspect=original_child)
            revisions.assert_not_called()

    def test_batch_revision_waits_for_explicit_consent_bound_to_reviewed_versions(self):
        child, next_job = "1"*32, "2"*32
        self.inspect.return_value = {"status":"candidate"}
        self.apply.return_value = {"status":"ready","model_id":"pro"}
        self.submit.side_effect = [child,next_job]
        self.advance()
        self.index["jobs"].append({"id":child, "kind":"asset_production", "project_id":self.pid,
            "_image_recovery_from":self.source["id"], "status":"failed",
            "result":{"image_recovery":{"source_job_id":self.source["id"],"model_id":"pro","status":"completed"}}})
        review_ref = {"asset_id":"review/a","revision_id":"r2","sha256":"0"*64}
        row = self.items[0]
        pin = {"asset_id":"object/a","revision_id":"r2"}
        row.update(job_id=child,status="needs_attention",candidate=pin,
                   review={"review_ref":review_ref,"status":"FAIL","checks":[{"status":"FAIL"}]})
        bound = {**pin,"subject_id":row["subject_id"],"purpose":row["purpose"],"candidate_job_id":child,
                 "review_ref":review_ref}
        for row in self.items[1:]:
            row["selected_pin"]={"revision_id":"passed"}
        write_json(self.path,self.index)
        with patch("pianchang.image_recovery.reinspectable_image_reviews",return_value=[]), \
                patch("pianchang.image_recovery.recoverable_image_reviews",return_value=[]), \
                patch("pianchang.image_recovery.authorizable_image_reviews",return_value=[bound]):
            request=self.advance()["human_request"]
            self.assertEqual(request["revision_items"],[bound])
            self.advance()
            self.submit.assert_called_once()
            body={"job_id":self.source["id"],"request_id":request["id"],"answers":{"authorize_revision":False}}
            with self.assertRaises(ValueError):
                answer_request(self.root,self.pid,body)
            answer_request(self.root,self.pid,{**body,"answers":{"authorize_revision":True}})
            self.watcher=self.daemon()
            self.assertEqual(self.advance()["reason"],"image_revision_authorized")
            self.assertEqual(self.advance()["image_recovery_job_id"],next_job)
            self.submit.assert_called_with(self.root,self.source,"pro",after_authorized_review=request["id"])
