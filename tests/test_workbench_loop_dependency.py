import json
import base64
import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tools.workbench_loop_dependency import recheck_dependency, _stale_source
from pianchang.story_spec import digest_json
from trace_system import get_tracer, use_context


class DependencyReplayTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        tracer=get_tracer(self.root/"test-trace")
        self.enterContext(use_context(None,tracer=tracer))
        self.enterContext(tracer.trace("test.dependency.offline"))
        self.pid="a"*16
        self.batch={"id":"b"*32,"trace_id":"c"*32,"kind":"asset_production",
                    "status":"failed","project_id":self.pid,"result":{"asset_diagnostics":{"items":[
                        {"subject_id":"char:one","purpose":"voice"}]}}}
        self.video={"id":"a"*32,"kind":"studio_video","status":"failed","project_id":self.pid,
                    "trace_id":"a"*32,"error":"以下镜头缺少已选用的参考资产："+ "、".join(
                        f"S00{i} · char:one · voice" for i in (1,2,3))}
        web=self.root/"outputs/web"
        web.mkdir(parents=True)
        (web/"state.json").write_text(json.dumps({"jobs":[self.batch,self.video]}))
        (web/"studio.json").write_text(json.dumps({"source_ref":{"revision_id":"r2"}}))
        self.unit={"subject_id":"char:one","purpose":"voice","job_id":self.batch["id"],"selected_pin":None}
        self.shot={"id":"S001","scene_id":"scene:one","entity_ids":["char:one"],"required_refs":[]}
        catalog=self.catalog={"project_id":self.pid,"spec":{"revision_id":"r3"},"project_path":str(web/"project.json"),
                 "project":{"shots":[self.shot]}}
        self.enterContext(patch("tools.workbench_loop_dependency.studio_asset_catalog",return_value=catalog))
        work=self.work={"production_started":True,"items":[self.unit]}
        self.enterContext(patch("tools.workbench_loop_dependency.asset_work_state",return_value=work))
        self.enterContext(patch("pianchang.asset_dispatch.asset_work_state",return_value=work))
        tr=self.root/"outputs/traces";tr.mkdir()
        self.db=sqlite3.connect(tr/"trace.sqlite3")
        self.addCleanup(self.db.close)
        self.db.execute("CREATE TABLE events(trace_seq INTEGER PRIMARY KEY, trace_id,span_id,parent_span_id,name,kind,data)")
        self.record("item","","studio.assets.produce_item",inputs={"subject_id":"char:one","purpose":"voice",
                    "project_id":self.pid,"job_id":self.batch["id"]})
        self.record("recipe","item","models.sdk.call")
        self.record("recipe","item","http.request",method="POST",body={"messages":[]})
        self.record("recipe","item","http.response",http_status=200)
        self.record("audio","item","models.sdk.call")
        self.record("audio","item","http.request",method="POST",body={"audio":{},"modalities":["text","audio"]})
        self.record("audio","item","http.error",error_type="ConnectError")
        self.record("other","","studio.assets.produce_item",inputs={"subject_id":"char:other","purpose":"voice",
                    "project_id":self.pid,"job_id":self.batch["id"]})
        self.record("other","", "http.response",http_status=200)

    def record(self,span,parent,name,**data):
        self.db.execute("INSERT INTO events(trace_id,span_id,parent_span_id,name,kind,data) VALUES(?,?,?,?,?,?)",
                        (self.batch["trace_id"],span,parent,name,"http" if name.startswith("http.") else "span.start",
                         json.dumps(data)))
        self.db.commit()

    def test_original_three_shots_rechecked_against_one_and_exact_voice_span(self):
        evidence=recheck_dependency(self.root,self.video)
        self.assertEqual((evidence["original_missing_count"],evidence["current_shot_count"],evidence["missing_count"]),(3,1,1))
        self.assertEqual(evidence["source_revision"],"r2")
        self.assertEqual(evidence["related_job_id"],self.batch["id"])
        self.assertEqual(evidence["status"],"awaiting_evidence")
        self.assertEqual(evidence["missing_references"],[{"shot_id":"S001","subject_id":"char:one","purpose":"voice"}])
        self.assertTrue(evidence["receipt_unknown"])
        recipe,audio=evidence["provider_calls"]
        self.assertEqual(recipe["http_status"],200)
        self.assertEqual((audio["span_id"],audio["request_kind"],audio["response_seq"]),("audio","audio",None))
        self.assertEqual(recheck_dependency(self.root,self.batch)["provider_calls"],evidence["provider_calls"])

    def test_selection_does_not_erase_unknown_charge_and_shot_must_pin_selection(self):
        self.unit["selected_pin"]={"revision_id":"r7"}
        self.assertEqual(recheck_dependency(self.root,self.video)["missing_count"],1)
        self.shot["required_refs"]=[self.unit["selected_pin"]]
        evidence=recheck_dependency(self.root,self.video)
        self.assertEqual(evidence["status"],"cleared")
        self.assertTrue(evidence["receipt_unknown"])
        self.assertFalse(evidence["safe_to_continue"])
        self.assertEqual(evidence["missing_count"],0)
        self.assertIsNone(recheck_dependency(self.root,{**self.video,"error":"some other failure"}))

    def add_rejected_image(self, *, mixed=False, operation="image_generation", url_path="images/generations",
                           model="fixture-image", code="InvalidEndpointOrModel.NotFound", status=404):
        diag = {"subject_id":"prop:cup", "purpose":"prop", "stage":"unknown", "verdict":"UNKNOWN"}
        item = {**diag, "status":"needs_attention", "job_id":self.batch["id"], "selected_pin":None}
        self.work["items"].append(item)
        issues = self.batch["result"]["asset_diagnostics"]["items"]
        if not mixed:
            issues.clear()
        issues.append(diag)
        (self.root/"outputs/web/state.json").write_text(json.dumps({"jobs":[self.batch,self.video]}))
        self.record("cup", "", "studio.assets.produce_item", inputs={**diag, "project_id":self.pid, "job_id":self.batch["id"]})
        self.record("generator", "cup", "models.sdk.call")
        self.record("generator", "cup", "model.route", operation=operation, route={"model_id":"fixture-image"})
        self.record("generator", "cup", "http.request", method="POST", url="https://fixture/v3/"+url_path, body={"model":model})
        self.record("generator", "cup", "http.response", http_status=status, body={"error":{"code":code}})
        return item

    def test_image_model_rejection_prompts_for_current_missing_asset_from_historical_trace(self):
        item = self.add_rejected_image()
        evidence = recheck_dependency(self.root,self.batch)
        self.assertEqual((evidence["status"],evidence["reason"]), ("needs_input","reference_image_model_access"))
        action = evidence["human_intervention"]
        self.assertEqual((action["http_status"],action["provider_code"],action["model_id"]),
                         (404,"InvalidEndpointOrModel.NotFound","fixture-image"))
        self.assertEqual(action["items"], [{"subject_id":"prop:cup","purpose":"prop","model_id":"fixture-image",
                                            "job_id":self.batch["id"],"http_status":404,"remaining_attempts":1,
                                            "provider_code":"InvalidEndpointOrModel.NotFound"}])
        self.assertFalse(evidence["safe_to_continue"])
        self.assertFalse(evidence["receipt_unknown"])
        item["job_id"] = "d"*32
        self.assertNotIn("human_intervention", recheck_dependency(self.root,self.batch))
        item.update(job_id=self.batch["id"], selected_pin={"revision_id":"r2"})
        resolved = recheck_dependency(self.root,self.batch)
        self.assertEqual(resolved["status"], "cleared")
        self.assertNotIn("human_intervention", resolved)

    def test_image_rejection_in_mixed_batch_keeps_unknown_receipt_gate(self):
        self.add_rejected_image(mixed=True)
        mixed = recheck_dependency(self.root,self.batch)
        self.assertEqual(mixed["reason"], "provider_receipt_unknown")
        self.assertTrue(mixed["receipt_unknown"])
        self.assertEqual([row["subject_id"] for row in mixed["human_intervention"]["items"]],["prop:cup"])
        self.record("earlier_unknown", "cup", "models.sdk.call")
        self.record("earlier_unknown", "cup", "http.request", method="POST", body={})
        self.assertNotIn("human_intervention",recheck_dependency(self.root,self.batch))

    def test_exact_access_rejection_codes_survive_original_wire(self):
        for status, code in ((403,"OperationDenied.PermissionDenied"), (403,"OperationDenied.ServiceNotOpen"),
                             (404,"ModelNotOpen"), (404,"InvalidEndpointOrModel.ModelIDAccessDisabled")):
            with self.subTest(code=code):
                self.db.execute("DELETE FROM events WHERE span_id IN ('cup','generator')")
                self.add_rejected_image(code=code, status=status)
                intervention = recheck_dependency(self.root, self.batch)["human_intervention"]
                self.assertEqual(intervention["provider_code"], code)
                self.assertEqual(intervention["items"][0]["http_status"], status)

    def test_image_retry_needs_exact_presend_stage_and_no_candidate(self):
        from pianchang.image_recovery import unsent_image_request
        self.add_rejected_image()
        self.db.execute("DELETE FROM events WHERE name='http.response' AND span_id='generator'")
        self.db.commit()
        child = {**self.batch, "_image_recovery_from":"d"*32, "_image_recovery_model":"fixture-image",
                 "result":{"image_recovery":{"status":"stopped_unknown", "source_job_id":"d"*32,"model_id":"fixture-image"},
                           "asset_diagnostics":{"items":[{"subject_id":"prop:cup", "purpose":"prop", "trace_id":self.batch["trace_id"]}]}}}
        for dispatch, phase, eligible in (("unknown","start_tls",False), ("not_sent","send_request_body",False),
                                           ("not_sent","start_tls",True)):
            self.db.execute("DELETE FROM events WHERE name='http.error' AND span_id='generator'")
            self.record("generator","cup","http.error",error_type="ConnectError",phase=phase,dispatch=dispatch)
            proof = unsent_image_request(self.root, child)
            self.assertEqual(bool(proof), eligible)
        self.assertEqual(proof["phase"], "start_tls")
        self.assertEqual(proof["span_id"], "generator")
        child["result"]["asset_diagnostics"]["items"][0]["candidate"] = {"revision_id":"r1"}
        self.assertIsNone(unsent_image_request(self.root, child))

    def test_404_at_other_stages_or_other_model_is_not_image_model_rejection(self):
        for args in ({"operation":"chat"}, {"url_path":"chat/completions"}, {"model":"other-model"},
                     {"code":"UnknownFailure"}):
            with self.subTest(args=args):
                self.db.execute("DELETE FROM events WHERE span_id IN ('cup','generator')")
                self.add_rejected_image(**args)
                self.assertNotIn("human_intervention",recheck_dependency(self.root,self.batch))

    def test_voice_recovery_requires_original_pre_send_evidence(self):
        for dispatch,phase,expected in (("unknown","start_tls",False),("not_sent","send_request_body",False),
                                        ("not_sent","start_tls",True)):
            with self.subTest(dispatch=dispatch,phase=phase):
                self.db.execute("UPDATE events SET data=? WHERE span_id='audio' AND name='http.error'",
                    (json.dumps({"error_type":"ConnectError","dispatch":dispatch,"phase":phase}),))
                self.db.commit()
                evidence=recheck_dependency(self.root,self.batch)
                self.assertEqual(evidence["reason"]=="voice_request_not_sent",expected)
                self.assertEqual(evidence["receipt_unknown"],not expected)
                self.assertEqual(evidence["transport_failure"]["error_type"],"ConnectError")
                self.assertEqual(evidence["transport_failure"]["request_kind"],"audio")
        self.record("second_audio","item","models.sdk.call")
        self.record("second_audio","item","http.request",method="POST",body={"audio":{}})
        self.assertTrue(recheck_dependency(self.root,self.batch)["receipt_unknown"])

    def test_connectivity_probe_matches_original_voice_endpoint_and_reuses_credential_free_head(self):
        from tools.workbench_loop_connectivity import check_voice_route
        route=SimpleNamespace(base_url="https://voice.example/v1",provider="openai",adapter="litellm",sdk_provider="openai")
        failure={"trace_id":"c"*32,"span_id":"a"*32,"request_seq":6,
                 "url_digest":digest_json(route.base_url+"/chat/completions")}
        transport=Mock()
        transport.handle_request.return_value=SimpleNamespace(status_code=401,headers={},close=lambda:None)
        with patch("tools.workbench_loop_connectivity.load_routes") as routes, \
                patch("tools.workbench_loop_connectivity._upstream_transport",return_value=transport):
            routes.return_value.resolve.return_value=route
            first=check_voice_route(self.root,failure)
            self.assertEqual(first,check_voice_route(self.root,failure))
            self.assertEqual(first["http_status"],401)
            route.base_url="https://changed.example/v1"
            self.assertEqual(check_voice_route(self.root,failure)["status"],"original_route_unavailable")
        transport.handle_request.assert_called_once()
        req=transport.handle_request.call_args.args[0]
        self.assertEqual((req.method,str(req.url)),("HEAD","https://voice.example/v1/models"))
        self.assertEqual(set(req.headers),{"host"})

    def test_reinspect_only_proven_observer_input_failure_on_same_candidate(self):
        from pianchang import asset_voice
        from tools.workbench_loop_voice_review import FORMAT,review_evidence
        audio=b"RIFF-fixture-waveform"
        digest=hashlib.sha256(audio).hexdigest()
        pin={"asset_id":"character/audio","revision_id":"r2","sha256":"f"*64,"media_sha256":digest,
             "subject_id":"char:one","purpose":"voice"}
        root_span,listen,http="2"*32,"3"*32,"4"*32
        usage={"prompt_tokens_details":{"audio_tokens":66}}
        report={"candidate":pin,"pin":pin,"verdict":"UNKNOWN","span_id":root_span,"trace_id":self.batch["trace_id"],
                "provider":{"request_id":"audio-chat","evidence_scope":"candidate_audio","media_sha256":digest,"usage":usage}}
        ref={"asset_id":"review","revision_id":"r1","sha256":digest_json(report)}
        store=self.root/"assets.sqlite"
        with closing(sqlite3.connect(store)) as db,db:
            db.execute("CREATE TABLE assets(story_id,asset_id,revision_id,sha256,content)")
            db.execute("INSERT INTO assets VALUES(?,?,?,?,?)",("story",ref["asset_id"],"r1",ref["sha256"],json.dumps(report)))
        blob=self.root/"reference-blobs"/digest[:2]/digest
        blob.parent.mkdir(parents=True)
        blob.write_bytes(audio)
        self.record(listen,root_span,"asset.voice.listen")
        self.record(http,listen,"bailian.vision.http")
        encoded=base64.b64encode(audio)
        parts=[{"type":"input_audio","input_audio":{"format":"wav","data":{"encoded_bytes":len(encoded),
                 "sha256":hashlib.sha256(encoded).hexdigest(),"omitted":"inline_media"}}},{"type":"text","text":"fixture"}]
        self.record(http,listen,"http.request",method="POST",url="https://voice.example/v1/chat/completions",
                    body={"model":"listener","messages":[{"role":"user","content":parts}]})
        self.record(http,listen,"http.response",http_status=200)
        diag={"trace_id":self.batch["trace_id"],"span_id":root_span,"candidate":pin,"review_ref":ref,
              "purpose":"voice","stage":"review","verdict":"UNKNOWN"}
        unit={"candidate":pin,"job_id":self.batch["id"]}
        catalog={"store_path":str(store),"story_id":"story"}
        def evidence(verdict="UNKNOWN",reason="No audio was provided.",tokens=66):
            response={"id":"audio-chat","usage":{"prompt_tokens_details":{"audio_tokens":tokens}},"choices":[{
                "finish_reason":"stop","message":{"content":json.dumps({"observations":[{"verdict":verdict,"reason":reason}]})}}]}
            self.db.execute("UPDATE events SET data=? WHERE span_id=? AND name='http.response'",
                            (json.dumps({"http_status":200,"body":response}),http))
            self.db.commit()
            return review_evidence(self.root,self.batch,diag,unit,catalog)
        with patch.object(asset_voice,"OPENAI_LISTEN_FORMAT",FORMAT,create=True), \
                patch("tools.workbench_loop_voice_review.load_routes") as routes:
            route=SimpleNamespace(provider="openai",adapter="openai",model_id="listener",base_url="https://voice.example/v1")
            routes.return_value.resolve.return_value=route
            self.assertEqual(evidence()["candidate"],pin)
            self.assertEqual(evidence()["corrected_input_format"],FORMAT)
            route.adapter="bailian"
            self.assertIsNone(evidence()["corrected_input_format"])
            route.adapter="openai"
            self.assertIsNone(evidence(verdict="FAIL"))
            self.assertIsNone(evidence(reason="Cannot distinguish the performance."))
            self.assertIsNone(evidence(tokens=0))
            blob.write_bytes(b"other")
            self.assertIsNone(evidence())

    def frozen_order(self):
        self.unit["selected_pin"]={"revision_id":"r7"}
        self.shot["required_refs"]=[self.unit["selected_pin"]]
        self.record("audio","item","http.response",http_status=200)
        run,cid="d"*32,"e"*32
        store=self.root/"outputs/collaborations/assets.sqlite3"
        store.parent.mkdir(parents=True)
        script={"asset_id":"script","revision_id":"r1","sha256":"f"*64}
        spec={"asset_id":"spec","revision_id":"r3","sha256":digest_json(self.catalog["project"])}
        self.catalog.update(run_id=run,story_id="story-"+run,store_path=str(store),spec=spec,script_ref=script)
        limits={"max_model_calls":30,"max_video_generations":3}
        plan={"project_id":self.pid,"script_ref":script,"spec_ref":spec,"new_shots":["S001"],"retake_shots":[],
              "reuse_shots":[],"execution_limits":limits,
              "acceptance_loop":{"autonomy_version":"1","shot_ids":["S001"],"max_repairs_per_shot":2}}
        plan["plan_id"]=digest_json(plan)
        self.video.update(message_id=cid,run_id=run,spec_ref=spec,script_ref=script,plan_id=plan["plan_id"],
                          _autonomy_body={"plan_id":plan["plan_id"],"script_ref":script,"spec_ref":spec})
        (self.root/"outputs/web/state.json").write_text(json.dumps({"jobs":[self.batch,self.video]}))
        with closing(sqlite3.connect(store)) as db, db:
            db.execute("CREATE TABLE messages(message_id,story_id,correlation_id,recipient,message_type,payload)")
            db.execute("CREATE TABLE deliveries(message_id,agent,status)")
            db.execute("CREATE TABLE receipts(message_id,agent,status,trace_context)")
            payload={"script":script,"template":spec,"shooting_plan":plan,"max_video_generations":3,
                     "execution_limits":limits}
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?)",(cid,"story-"+run,run,"video","production.requested",json.dumps(payload)))
            db.execute("INSERT INTO deliveries VALUES(?,?,?)",(cid,"video","accepted"))
            db.execute("INSERT INTO receipts VALUES(?,?,?,?)",(cid,"video","accepted",json.dumps({"trace_id":self.video["trace_id"]})))
        context={"trace_id":self.video["trace_id"],"span_id":"1"*16}
        self.db.execute("INSERT INTO events(trace_id,span_id,name,kind,data) VALUES(?,?,?,?,?)",
                        (context["trace_id"],context["span_id"],"pi.loop","span.start","{}"))
        self.db.commit()
        path=store.parent/"runs"/run/("video-spec-"+cid)/"agent/run.json"
        path.parent.mkdir(parents=True)
        record={"shooting_plan":plan,"limits":{"max_video_generations":3,"max_steps":30},"steps":2,
                "video_generations":0,"reserved_shots":[],"repair_reservations":{},"status":"interrupted","trace_context":context}
        path.write_text(json.dumps(record))
        return path,record

    def test_current_frozen_order_and_all_reservations_required_for_continuation(self):
        path,record=self.frozen_order()
        result=recheck_dependency(self.root,self.video)
        self.assertTrue(result["safe_to_continue"],result)
        self.assertEqual(result["commission_id"],self.video["message_id"])
        self.assertEqual(result["remaining_video_generations"],3)
        for changed in ({"steps":30},{"pending_tool":{"effect":"video"}},{"reserved_shots":["S001"],"video_generations":1}):
            with self.subTest(changed=changed):
                path.write_text(json.dumps({**record,**changed}))
                self.assertFalse(recheck_dependency(self.root,self.video)["safe_to_continue"])
        path.write_text(json.dumps(record))
        self.catalog["spec"]={**self.catalog["spec"],"revision_id":"r4"}
        result=recheck_dependency(self.root,self.video)
        self.assertTrue(result["requires_plan_reconfirmation"])
        self.assertFalse(result["safe_to_continue"])

    def test_only_reachable_proven_planner_route_releases_frozen_checkpoint(self):
        failure={"request_kind":"model","dispatch":"not_sent","request_seq":14,"error_seq":16}
        ready={"status":"cleared","reason":"frozen_commission_ready","safe_to_continue":True,"receipt_unknown":False,
               "transport_failure":failure,"continuation_checkpoint":"3"*64}
        with patch("tools.workbench_loop_dependency._recheck_dependency",side_effect=lambda *_:dict(ready)), \
                patch("tools.workbench_loop_connectivity.check_planner_route") as probe:
            probe.return_value={"status":"retry_later"}
            waiting=recheck_dependency(self.root,self.video)
            self.assertFalse(waiting["safe_to_continue"])
            self.assertEqual(waiting["reason"],"planner_route_unavailable")
            probe.return_value={"status":"reachable"}
            self.assertTrue(recheck_dependency(self.root,self.video)["safe_to_continue"])
            probe.assert_called_with(self.root,failure)
            ready.update(safe_to_continue=False,receipt_unknown=True)
            self.assertFalse(recheck_dependency(self.root,self.video)["safe_to_continue"])
            self.assertEqual(probe.call_count,2)

    def test_uncertain_video_post_blocks_continuation_with_selected_assets_and_remaining_budget(self):
        _,record=self.frozen_order()
        context=record["trace_context"]
        self.db.execute("INSERT INTO events(trace_id,span_id,name,kind,data) VALUES(?,?,?,?,?)",
                        (context["trace_id"],context["span_id"],"http.request","http","{}"))
        self.db.commit()
        result=recheck_dependency(self.root,self.video)
        self.assertFalse(result["safe_to_continue"],result)
        self.assertTrue(result["receipt_unknown"])
        self.assertEqual(result["missing_count"],0)

    def test_newer_unit_owner_cannot_clear_original_unknown_receipt(self):
        self.unit["selected_pin"]={"revision_id":"r7"}
        self.shot["required_refs"]=[self.unit["selected_pin"]]
        newer={**self.batch,"id":"9"*32,"trace_id":"8"*32,"status":"succeeded","result":{}}
        self.unit["job_id"]=newer["id"]
        (self.root/"outputs/web/state.json").write_text(json.dumps({"jobs":[self.batch,self.video,newer]}))
        rows=[("newer","", "studio.assets.produce_item","span.start",{"inputs":{"job_id":newer["id"],
               "project_id":self.pid,"subject_id":"char:one","purpose":"voice"}}),
              ("n-audio","newer","models.sdk.call","span.start",{}),
              ("n-audio","newer","http.request","http",{"method":"POST","body":{"audio":{}}}),
              ("n-audio","newer","http.response","http",{"http_status":200})]
        for span,parent,name,kind,data in rows:
            self.db.execute("INSERT INTO events(trace_id,span_id,parent_span_id,name,kind,data) VALUES(?,?,?,?,?,?)",
                            (newer["trace_id"],span,parent,name,kind,json.dumps(data)))
        self.db.commit()
        evidence=recheck_dependency(self.root,self.video)
        self.assertEqual(evidence["missing_count"],0)
        self.assertEqual(evidence["related_job_id"],self.batch["id"])
        self.assertTrue(evidence["receipt_unknown"])
        self.assertFalse(evidence["safe_to_continue"])

    def test_pending_order_requires_positive_unclaimed_delivery(self):
        path,record=self.frozen_order()
        path.unlink()
        path.parent.rmdir()
        path.parent.parent.rmdir()
        with closing(sqlite3.connect(self.catalog["store_path"])) as db, db:
            db.execute("UPDATE deliveries SET status='pending'")
        self.assertFalse(recheck_dependency(self.root,self.video)["safe_to_continue"])
        with closing(sqlite3.connect(self.catalog["store_path"])) as db, db:
            db.execute("DELETE FROM receipts")
        result=recheck_dependency(self.root,self.video)
        self.assertTrue(result["safe_to_continue"],result)
        self.assertEqual(result["remaining_video_generations"],3)

    def test_resumed_partial_requires_exact_terminal_receipt_for_each_reserved_task(self):
        path,record=self.frozen_order()
        record.update(reserved_shots=["S001"],video_generations=1)
        path.write_text(json.dumps(record))
        folder=self.root/"outputs/videos/original"
        folder.mkdir(parents=True)
        metadata={"shooting_plan_id":self.video["plan_id"],"script_asset_ref":self.video["script_ref"],
                  "shot_id":"S001","execution_key":digest_json({"order":str(path.parent.parent),"shot_id":"S001","repair_id":None})}
        receipt={"status":"SUCCEEDED","task_id":"original-task","metadata":metadata,
                 "provider_response":{"output":{"task_status":"SUCCEEDED","task_id":"original-task"}}}
        target=folder/"job.json"
        target.write_text(json.dumps(receipt))
        result=recheck_dependency(self.root,self.video)
        self.assertTrue(result["safe_to_continue"],result)
        self.assertEqual(result["remaining_video_generations"],2)
        receipt["provider_response"]["output"]["task_id"]="different-task"
        target.write_text(json.dumps(receipt))
        self.assertFalse(recheck_dependency(self.root,self.video)["safe_to_continue"])

    def test_blocked_paid_planner_uses_original_checkpoint_and_shared_receipt_check(self):
        path, record = self.frozen_order()
        record.update(reserved_shots=["S001"], video_generations=1, status="needs_input", result={"reason":"pi_stopped"})
        path.write_text(json.dumps(record))
        self.video.update(error="offline decision", worker_trace_id=record["trace_context"]["trace_id"])
        (self.root/"outputs/web/state.json").write_text(json.dumps({"jobs":[self.batch,self.video]}))
        with closing(sqlite3.connect(self.catalog["store_path"])) as db, db:
            db.execute("UPDATE deliveries SET status='blocked'")
        proof = {"request_kind":"model","dispatch":"not_sent","commission_id":self.video["message_id"],
                 "run_id":self.video["run_id"],"trace_id":self.video["worker_trace_id"]}
        trace = {"outstanding":False,"trace_failed":False,"planner_not_sent":proof}
        with patch("tools.workbench_loop_observer._planner_not_sent",return_value=proof), \
                patch("tools.workbench_loop_observer._trace",return_value=trace), \
                patch("tools.workbench_loop_connectivity.check_planner_route",return_value={"status":"reachable"}), \
                patch("pianchang.video_autonomy.can_resume_paid_pi_order") as confirmed:
            confirmed.return_value = False
            result = recheck_dependency(self.root,self.video)
            self.assertFalse(result["safe_to_continue"],result)
            self.assertTrue(result["receipt_unknown"],result)
            confirmed.assert_called_once()
            self.assertEqual(confirmed.call_args.args,(path.parent.parent,record["shooting_plan"],record,proof))
            confirmed.return_value = True
            result = recheck_dependency(self.root,self.video)
            self.assertTrue(result["safe_to_continue"],result)
            self.assertEqual(result["remaining_video_generations"],2)
            trace["outstanding"] = True
            self.assertFalse(recheck_dependency(self.root,self.video)["safe_to_continue"])
            self.assertEqual(confirmed.call_count,2)

    def test_stale_spec_requires_both_exact_revisions_and_an_observable_live_mismatch(self):
        source={"shots":[{"id":"S001","duration_s":15}]}
        prior={"shots":[{"id":"S001","duration_s":5},{"id":"S002","duration_s":5},{"id":"S003","duration_s":5}]}
        bound={"shots":[{**s,"required_refs":[]} for s in prior["shots"]]}
        path=self.root/"project.json"
        path.write_text(json.dumps(source))
        store=self.root/"assets.sqlite3"
        with closing(sqlite3.connect(store)) as db, db:
            db.execute("CREATE TABLE assets(story_id,asset_id,revision_id,seq,kind,sha256,content)")
            for name,revision,num,kind,value in [
                    ("input","r1",1,"input_project",prior),("input","r2",2,"input_project",source),
                    ("spec","r3",3,"production_spec",bound)]:
                db.execute("INSERT INTO assets VALUES(?,?,?,?,?,?,?)",
                           ("story",name,revision,num,kind,digest_json(value),json.dumps(value)))
        catalog={"story_id":"story","spec":{"asset_id":"spec","revision_id":"r3","sha256":digest_json(bound)},
                 "project_path":str(path),"project":bound,"store_path":str(store)}
        publication={"source_ref":{"asset_id":"input","revision_id":"r2","sha256":digest_json(source)}}
        self.assertEqual(_stale_source(catalog,publication)["actual_source_revision"],"r1")
        self.assertIsNone(_stale_source({**catalog,"project":source},publication))
        self.assertIsNone(_stale_source({**catalog,"story_id":"other"},publication))
