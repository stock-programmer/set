"""Offline checks for job isolation, shareable diagnostics, and receipt-based completion."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tools.workbench_loop_observer import _local_checkpoint, _trace, linked_recovery_job, observe_job
from trace_system import get_tracer, use_context


class LoopObserverTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.tracer = get_tracer(self.root/"outputs/traces")
        self.job = {"id":"a"*32,"kind":"studio_script","status":"failed"}

    def test_provider_receipt_gap_stops_despite_application_error(self):
        with use_context(None,tracer=self.tracer), self.tracer.trace("test.observer") as ctx:
            from trace_system import current_context, event
            self.job["trace_id"] = current_context()["trace_id"]
            event("http.request",{"body":"private unpublished screenplay"})
            event("pi.model.failure",{"type":"ConnectError","message":"private failure detail"})
        observed=observe_job(self.root,self.job)
        self.assertEqual(observed["reason"],"provider_receipt_unknown")
        self.assertFalse(observed["safe_to_continue"])
        self.assertNotIn("private",json.dumps(observed))

    def test_only_stopped_decisions_exact_presend_error_reconciles_the_wire(self):
        from trace_system import current_context, event, span
        from httpx import ConnectError
        for dispatch, phase, unrelated, known in (("not_sent","start_tls",False,True),
                                                 ("unknown","start_tls",False,False),
                                                 ("not_sent","send_request_body",False,False),
                                                 ("not_sent","start_tls",True,False)):
            with self.subTest(dispatch=dispatch, phase=phase, unrelated=unrelated):
                with use_context(None,tracer=self.tracer), self.tracer.trace("test.pi.observer"):
                    ctx = current_context()
                    if unrelated:
                        with span("unconfirmed.video.generation"):
                            event("http.request",{"method":"POST"},kind="http")
                    try:
                        with span("pi.model.exchange"), span("models.sdk.call"):
                            event("model.route",{"operation":"completion","route":{"model_id":"test-model"}},kind="model")
                            event("http.request",{"method":"POST","url":"https://provider.example/v1/chat/completions",
                                  "body":{"model":"test-model"},"wire_body_sha256":"a"*64},kind="http")
                            event("http.proxy.selected",{"mode":"direct","target_host":"provider.example"},kind="http")
                            event("http.error",{"dispatch":dispatch,"phase":phase,"error_type":"ConnectError"},kind="http")
                            raise ConnectError("unavailable")
                    except ConnectError:
                        event("pi.model.failure",{"purpose":"decision","type":"ConnectError"},kind="error")
                        event("pi.run.stopped",{"type":"ConnectError"},kind="error")
                verified = _trace(self.root,ctx["trace_id"],ctx["span_id"])
                self.assertEqual(not verified["outstanding"],known)
                if known:
                    proof = verified["planner_not_sent"]
                    self.assertLess(proof["request_seq"],proof["error_seq"])
                    self.assertEqual((proof["run_span_id"],proof["phase"]),(ctx["span_id"],"start_tls"))

    def test_scopes_fault_to_this_run_and_only_reports_repo_locations(self):
        from trace_system import current_context,event,span
        module=self.root/"pianchang/test_dummy.py"
        module.parent.mkdir()
        module.touch()
        with use_context(None,tracer=self.tracer), self.tracer.trace("test.observer"):
            self.job["trace_id"]=current_context()["trace_id"]
            with span("unrelated"):
                event("http.request",{"body":"another job"})
            with span("this.job"):
                ctx=current_context()
                event("task.exception",{"type":"KeyError","message":"secret-input",
                      "stack":f'  File "{module}", line 8, in handle\n    secret-input'})
        path=self.root/"outputs/pi/runs"/self.job["id"]/"run.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"trace_context":ctx}),encoding="utf8")
        observed=observe_job(self.root,self.job)
        self.assertEqual(observed["status"],"repairable")
        self.assertEqual(observed["fault"]["frames"],[{"file":"pianchang/test_dummy.py","line":8}])
        self.assertNotIn("secret-input",json.dumps(observed))

    def test_conversation_response_and_other_project_are_not_delivery(self):
        job={**self.job,"kind":"pi_video","project_id":"one","session_id":"current","status":"succeeded",
             "result":{"status":"responded","turn_complete":True,"task_complete":False}}
        self.assertEqual(observe_job(self.root,job,role="video")["status"],"needs_input")
        self.assertEqual(observe_job(self.root,job,session_id="other",role="video")["status"],"unknown")
        self.assertEqual(observe_job(self.root,job,role="video",preview={"ready":True,"project_id":"other"})["status"],"needs_input")
        gate={"ready":True,"project_id":"one","production_ref":{"asset_id":"video","revision_id":"r1","sha256":"b"*64}}
        self.assertEqual(observe_job(self.root,job,role="video",preview=gate)["status"],"needs_input")
        dbfile=self.root/"outputs/collaborations/assets.sqlite3"
        dbfile.parent.mkdir(parents=True)
        commission,delivered="7"*32,"8"*32
        with closing(sqlite3.connect(dbfile)) as db, db:
            db.execute("CREATE TABLE messages(message_id,causation_id,message_type,sender,recipient,payload)")
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?)",(delivered,commission,"production.result","video",
                       "script",json.dumps({"production":gate["production_ref"]})))
        job.update(commission_id=commission,result={"production_ref":gate["production_ref"],"message_id":delivered})
        self.assertEqual(observe_job(self.root,job,role="video",preview=gate)["status"],"succeeded")
        self.assertEqual(observe_job(self.root,{**job,"commission_id":"6"*32},role="video",preview=gate)["status"],"needs_input")

    def write_project(self,path,publication):
        from pianchang.story_spec import digest_json
        script={"idea":"要求总共1个镜头。","episodes":[]}
        project={"script_source":{"script":script,"sha256":digest_json(script)},"shots":[{}]}
        publication["script_ref"]["sha256"]=digest_json(script)
        publication["source_ref"]["sha256"]=digest_json(project)
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(project),encoding="utf8")

    def test_publication_receipt_must_match(self):
        pin={"asset_id":"author/script","revision_id":"r1","sha256":"b"*64}
        saved={"script_ref":pin,"source_ref":{**pin,"asset_id":"author/source-project"},"project_id":"project"}
        job={**self.job,"project_id":"project","status":"succeeded",
             "result":{"task_complete":True,"completion_check":{"complete":True,"publication":saved}}}
        path=self.root/"outputs/web/stories"/job["id"]/"studio.json"
        path.parent.mkdir(parents=True)
        self.write_project(path.with_name("project.json"),saved)
        path.write_text(json.dumps({**saved,"source_ref":None}),encoding="utf8")
        self.assertEqual(observe_job(self.root,job)["status"],"needs_input")
        path.write_text(json.dumps(saved),encoding="utf8")
        self.assertEqual(observe_job(self.root,job)["status"],"succeeded")
        dbfile=self.root/"outputs/collaborations/assets.sqlite3"
        dbfile.parent.mkdir(parents=True)
        with closing(sqlite3.connect(dbfile)) as db, db:
            db.execute("CREATE TABLE messages(story_id,message_type)")
        index=self.root/"outputs/web/state.json"
        index.write_text(json.dumps({"jobs":[{**job,"story_input":{"idea":"要求总共3个镜头。"}}]}),encoding="utf8")
        mismatch=observe_job(self.root,job)
        self.assertEqual(mismatch["status"],"interrupted")
        self.assertTrue(mismatch["safe_to_continue"])
        self.assertEqual(mismatch["issues"],[{"path":"requested.shots","expected":3,"actual":1}])
        with closing(sqlite3.connect(dbfile)) as db, db:
            db.execute("INSERT INTO messages VALUES(?,?)",("story-"+job["id"],"production.requested"))
        self.assertEqual(observe_job(self.root,job)["status"],"needs_input")

    def test_commission_recovery_requires_both_pins_and_one_active_owner(self):
        source={**self.job,"kind":"studio_video","project_id":"one","message_id":"b"*32,
                "session_id":"session","status":"interrupted"}
        next_job={"id":"c"*32,"kind":"pi_video","status":"running","project_id":"one",
                  "session_id":"session","commission_id":source["message_id"],"commission_job_id":source["id"]}
        def find(*other):
            return linked_recovery_job(self.root,source,[source,*other],role="video",session_id="session")
        self.assertEqual(find(next_job),next_job)
        for patch in ({"status":"succeeded"},{"session_id":"other"},{"project_id":"other"},
                      {"commission_job_id":"d"*32},{"commission_id":"e"*32}):
            self.assertIsNone(find({**next_job,**patch}))
        self.assertIsNone(find(next_job,{**next_job,"id":"e"*32}))

    def test_external_story_requires_job_bound_collaboration_and_both_revisions(self):
        from pianchang.app_service import project_id_for_path
        rid="4"*32
        script={"asset_id":"story-"+rid+"/script","revision_id":"r1","sha256":"a"*64}
        project=self.root/"fixtures/project.json"
        pid=project_id_for_path(project)
        pub={"story_id":"story-"+rid,"project_id":pid,"run_id":rid,"script_ref":script,
             "source_ref":{**script,"asset_id":"story-"+rid+"/source-project"}}
        self.write_project(project,pub)
        path=self.root/"outputs/collaborations/runs"/rid/"overview.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(pub),encoding="utf8")
        job={**self.job,"kind":"pi_script","status":"succeeded","project_id":pid,"run_id":rid,
             "result":{"task_complete":True,"completion_check":{"complete":True,"publication":pub}}}
        self.assertEqual(observe_job(self.root,job)["status"],"succeeded")
        path.write_text(json.dumps({**pub,"source_ref":None}),encoding="utf8")
        self.assertNotEqual(observe_job(self.root,job)["status"],"succeeded")

    def test_intermediate_draft_requires_this_run_and_the_saved_content_hash(self):
        from pianchang.story_spec import digest_json
        path=self.root/"outputs/pi/workspaces"/("story-"+self.job["id"])/"script/candidate.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"title":"draft"}',encoding="utf8")
        entry={"tool":"write_draft","ok":True,"type":"tool_result",
               "result":{"candidate_sha256":digest_json({"title":"draft"}),"needs_check":True}}
        run={"messages":[{"role":"user","content":json.dumps(entry)}]}
        result={"status":"needs_input","completion_check":{"scope":"script_and_spec_publication_not_media_acceptance"}}
        self.assertEqual(_local_checkpoint(self.root,self.job,result,run),"continue_from_saved_draft")
        path.write_text('{"title":"other"}',encoding="utf8")
        self.assertIsNone(_local_checkpoint(self.root,self.job,result,run))

    def test_spec_checkpoint_matches_current_script_candidate_and_report(self):
        source=self.root/"outputs/pi/workspaces"/("story-"+self.job["id"])
        (source/"script").mkdir(parents=True)
        pin={"asset_id":"story/script","revision_id":"r1","sha256":"f"*64}
        candidate={"shots":[]}
        report={"structure_ready":False}
        for name,body in (("script/publication.json",pin),("spec-candidate.json",candidate),
                          ("validation-report.json",report)):
            (source/name).write_text(json.dumps(body),encoding="utf8")
        data={"script_ref":pin,"candidate":candidate,"validation":report}
        doc={"status":"needs_input","completion_check":{"scope":"script_and_spec_publication_not_media_acceptance"}}
        def checkpoint(**changes):
            entry={"tool":"read_production_spec","ok":True,"type":"tool_result","result":{**data,**changes}}
            run={"messages":[{"role":"user","content":json.dumps(entry)}]}
            return _local_checkpoint(self.root,self.job,doc,run)
        self.assertEqual(checkpoint(),"continue_from_saved_spec")
        for changes in ({"candidate":{}},{"script_ref":{}},{"validation":{}},{"interruption_evidence":["pending"]}):
            self.assertIsNone(checkpoint(**changes))

    def test_queued_message_rebinds_to_its_persisted_session_owner(self):
        sid,mid,jid="1"*32,"2"*32,"3"*32
        dest=self.root/"outputs/pi/registry.sqlite3"
        dest.parent.mkdir(parents=True)
        db=sqlite3.connect(dest)
        try:
            db.executescript("CREATE TABLE sessions(id,role); CREATE TABLE messages(id,session_id,role,job_id)")
            db.execute("INSERT INTO sessions VALUES(?,?)",(sid,"script"))
            db.execute("INSERT INTO messages VALUES(?,?,?,?)",(mid,sid,"user",jid))
            db.commit()
        finally:
            db.close()
        actual={"id":jid,"kind":"pi_script","status":"succeeded","session_id":sid}
        def find(message):
            return linked_recovery_job(self.root,self.job,[actual],role="script",session_id=sid,message_id=message)
        self.assertEqual(find(mid),actual)
        self.assertIsNone(find("a"*32))


if __name__ == "__main__":
    unittest.main()
