import tempfile
import sqlite3
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pianchang.app_service import write_json
from pianchang.image_recovery import _review_targets, image_recovery_key, run_image_recovery, verified_image_recovery
from pianchang.story_spec import digest_json
from trace_system import get_tracer, use_context


class ImageRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        tracer = get_tracer(self.root / "traces")
        self.enterContext(tracer.storage_session())
        self.enterContext(use_context(None, tracer=tracer))
        self.enterContext(tracer.trace("test.image_recovery.offline"))
        self.pid, self.jid, self.child = "a" * 16, "b" * 32, "c" * 32
        self.catalog = {"story_id":"story", "project_id":self.pid, "spec":{"sha256":"spec"},
                        "entities":[{"id":"prop:" + str(i), "name":str(i), "references":[]}
                                    for i in range(6)]}
        self.rows = [{"subject_id": e["id"], "purpose":"prop", "job_id":self.jid,
                      "status":"needs_attention", "style_sha256":digest_json("original-style")}
                     for e in self.catalog["entities"]]
        self.work = {"style":"original-style", "items":self.rows}
        self.source = {"id":self.jid,"project_id":self.pid,"trace_id":"d"*32,
                       "kind":"asset_production","status":"failed",
                       "result":{"asset_diagnostics":{"items":[dict(row) for row in self.rows]}}}
        self.jobs = [self.source]
        self.service = SimpleNamespace(root=self.root, lock=threading.RLock(), state={"jobs":self.jobs},
                                       tracer=tracer, _update=lambda *args, **kw: None)
        self.model = "ep-probe-a"
        self.body = {"resume_images_from":self.jid, "model_id":self.model}
        self.enterContext(patch("pianchang.image_recovery.load_routes", side_effect=lambda env:
            SimpleNamespace(resolve=lambda purpose:SimpleNamespace(model_id=self.model, api_key="offline",
                    snapshot=lambda:{"model_id":self.model}))))
        self.enterContext(patch("pianchang.image_recovery._original_basis", return_value="basis"))
        self.enterContext(patch("pianchang.image_recovery._original_attempts", return_value=2))
        self.enterContext(patch("pianchang.image_recovery._matches_basis", return_value=True))
        self.enterContext(patch("pianchang.image_recovery._basis", return_value="basis"))
        self.enterContext(patch("pianchang.image_recovery.asset_work_state", return_value=self.work))
        self.enterContext(patch("pianchang.image_recovery.studio_asset_catalog", return_value=self.catalog))
        self.proven = {self.jid:{r["subject_id"] for r in self.rows}}
        def evidence(root, job):
            return {"human_intervention":{"kind":"reference_image_model_access","items":[
                {"subject_id":r["subject_id"], "purpose":"prop", "job_id":job["id"]} for r in self.rows
                if r["subject_id"] in self.proven.get(job["id"], set()) and r["job_id"] == job["id"]]}}
        self.enterContext(patch("tools.workbench_loop_dependency.recheck_dependency", side_effect=evidence))

    def verify(self, body=None):
        return verified_image_recovery(self.service, self.pid, body or self.body, self.catalog)

    def test_one_rejection_stops_batch_and_next_route_recovers_first_and_five_untouched(self):
        order = self.verify()
        self.assertEqual(len(order["targets"]), 6)
        child = {"id":self.child, "project_id":self.pid, "trace_id":"1"*32, "status":"running",
                 "_image_recovery_from":self.jid, "_image_recovery_model":self.model}
        self.jobs.append(child)
        def reject(service, pid, job, catalog, unit, task, style):
            unit.update(job_id=job["id"], status="needs_attention", error="offline model rejected",
                        rejected_image_code="ModelNotOpen", rejected_image_model=self.model)
            self.proven[job["id"]] = {unit["subject_id"]}
        with patch("pianchang.asset_dispatch._produce_item", side_effect=reject) as made:
            first = run_image_recovery(self.service, child, order, self.catalog)
        self.assertEqual(made.call_count, 1)
        self.assertEqual(first["image_recovery"]["status"], "model_rejected")
        self.assertEqual(first["image_recovery"]["provider_code"], "ModelNotOpen")
        self.assertEqual(first["image_recovery"]["remaining"], 6)
        child.update(status="failed", result=first)
        self.model = "ep-probe-b"
        second = self.verify({**self.body, "model_id":self.model})
        self.assertEqual([r["owner"] for r in second["targets"]], [self.child] + [self.jid]*5)
        job = {"id":"2"*32, "project_id":self.pid,"trace_id":"3"*32}
        def produce(service, pid, job, catalog, unit, task, style):
            self.assertEqual(task["image_route_sha256"], digest_json({"model_id":self.model}))
            unit.update(job_id=job["id"], candidate={"revision_id":"new"}, status="approved")
        with patch("pianchang.asset_dispatch._produce_item", side_effect=produce) as made:
            recovered = run_image_recovery(self.service, job, second, self.catalog)
        self.assertEqual(made.call_count, 6)
        self.assertEqual(recovered["image_recovery"]["selected"], 6)
        self.assertEqual(recovered["image_recovery"]["remaining"], 0)
        with self.assertRaises(ValueError):
            self.verify({**self.body, "model_id":self.model})

    def test_replay_skips_unknown_receipt_later_owner_candidate_and_existing_media(self):
        self.proven[self.jid].remove("prop:0")
        self.rows[1]["job_id"] = "later-manual-job"
        self.rows[2]["candidate"] = {"revision_id":"r1"}
        self.rows[3]["selected_pin"] = {"revision_id":"r1"}
        self.catalog["entities"][4]["references"] = [{"purpose":"prop"}]
        order = self.verify()
        self.assertEqual([t["subject_id"] for t in order["targets"]], ["prop:5"])
        self.rows[5]["job_id"] = "replaced-before-execution"
        with patch("pianchang.asset_dispatch._produce_item") as submit, self.assertRaises(ValueError):
            run_image_recovery(self.service, {"id":self.child,"project_id":self.pid}, order, self.catalog)
        submit.assert_not_called()

    def test_unsent_child_requires_matching_receipt_and_a_reachable_route(self):
        from pianchang.image_recovery import ImageRecoveryConflict
        prior = {"id":self.child, "project_id":self.pid, "status":"failed",
                 "_image_recovery_from":self.jid, "_image_recovery_model":self.model}
        self.jobs.append(prior)
        self.rows[0]["job_id"] = self.child
        body = {**self.body, "after_not_sent":self.child}
        proof = {"subject_id":"prop:0", "purpose":"prop", "request_seq":118, "error_seq":120}
        with patch("pianchang.image_recovery.unsent_image_request", return_value=proof) as receipt, \
                patch("pianchang.image_recovery._original_attempts", return_value=2), \
                patch("tools.workbench_loop_ark.probe_image_transport", return_value={"status":"wait"}) as probe:
            with self.assertRaises(ImageRecoveryConflict) as down:
                self.verify(body)
            self.assertEqual(down.exception.code, "image_transport_unavailable")
            probe.return_value = {"status":"reachable", "model_id":self.model}
            recovered = self.verify(body)
            self.assertEqual([r["owner"] for r in recovered["targets"]], [self.child] + [self.jid]*5)
            self.assertEqual(recovered["targets"][0]["remaining_attempts"], 2)
            probe.assert_called_with(self.root, proof)
            receipt.return_value = None
            with self.assertRaises(ImageRecoveryConflict) as unknown:
                self.verify(body)
            self.assertEqual(unknown.exception.code, "image_scope_changed")
            self.assertEqual(probe.call_count, 2)

    def test_interruption_and_answer_retries_are_scoped_and_idempotent(self):
        prior = {"id":self.child, "project_id":self.pid, "status":"interrupted", "_image_recovery_from":self.jid,
                 "_image_recovery_model":self.model}
        self.jobs.append(prior)
        self.rows[0]["job_id"] = self.child
        continued = {**self.body, "after_interrupted":self.child}
        self.assertEqual(len(self.verify(continued)["targets"]), 5)
        self.assertNotEqual(image_recovery_key(self.pid, continued), image_recovery_key(self.pid, self.body))
        prior["status"] = "running"
        with self.assertRaises(ValueError):
            self.verify(continued)
        rid = "e" * 64
        body = {**self.body, "answer_id": rid}
        file = self.root / "outputs/workbench-loop/watch/answers" / (rid + ".json")
        record = {"request_id":rid, "job_id":self.jid, "project_id":"other", "answers":{"confirm":True}}
        write_json(file, record)
        with self.assertRaises(ValueError):
            self.verify(body)
        prior["status"] = "failed"
        write_json(file, {**record, "project_id":self.pid})
        self.assertEqual(len(self.verify(body)["targets"]), 5)
        self.assertEqual(image_recovery_key(self.pid, body), image_recovery_key(self.pid, dict(reversed(body.items()))))
        self.assertNotEqual(image_recovery_key(self.pid, body), image_recovery_key(self.pid, self.body))

    def test_quality_repair_uses_pinned_report_and_only_the_original_unused_generation(self):
        from pianchang.asset_review import POLICY_VERSION, review_asset_id
        row = self.rows[0]
        pin = {"asset_id":"image", "revision_id":"r1", "sha256":"1"*64, "media_sha256":"2"*64,
               "subject_id":row["subject_id"], "purpose":row["purpose"]}
        child = {"id":self.child, "project_id":self.pid,"trace_id":"c"*32,"status":"failed",
                 "_image_recovery_from":self.jid, "result":{"image_recovery":{"status":"completed"}}}
        self.jobs.append(child)
        report = {"candidate":pin,"trace_id":child["trace_id"],"status":"FAIL","policy_version":POLICY_VERSION,
                  "checks":[{"path":"subject","frame_ids":["candidate"],"status":"FAIL","expected":"blue","actual":"red"}]}
        ref = {"asset_id":review_asset_id(pin), "revision_id":"r1", "sha256":digest_json(report)}
        row.update(job_id=self.child, candidate=pin, review={**report,"review_ref":ref},
                   attempts=[{"pin":pin,"trace_id":child["trace_id"],"review_ref":ref}])
        self.catalog["entities"][0]["references"] = [pin]
        self.catalog["store_path"] = str(self.root / "assets.sqlite3")
        db = sqlite3.connect(self.catalog["store_path"])
        self.addCleanup(db.close)
        db.execute("CREATE TABLE assets(story_id, asset_id, kind, revision_id, sha256, seq)")
        db.execute("INSERT INTO assets VALUES(?,?,?,?,?,1)", ("story",ref["asset_id"],"reference_review","r1",ref["sha256"]))
        db.commit()
        paid = {"request_kind":"image","http_status":200,"response_seq":22}
        calls = [{"request_kind":"image", "dispatch":"not_sent", "phase":"start_tls",
                  "error_type":"ConnectError", "error_seq":12}, paid]
        def read():
            return _review_targets(self.service,self.source,child,self.catalog,self.work,self.jobs)
        with patch("pianchang.image_recovery._original_attempts", return_value=2), \
                patch("tools.workbench_loop_dependency._unit_calls", return_value=calls):
            verified = read()
            self.assertEqual([(r["pin"],r["review_ref"],r["remaining_attempts"]) for r in verified], [(pin,ref,1)])
            calls.append({"request_kind":"image","dispatch":"unknown"})
            self.assertEqual(read(), [])
            calls[-1] = paid
            self.assertEqual(read(), [])
            calls.pop()
            row["review"]["checks"] = []
            self.assertEqual(read(), [])

    def test_old_cross_subject_report_reinspection_never_generates_or_selects_uncertain_media(self):
        from copy import deepcopy
        from pianchang.asset_review import POLICY_VERSION, review_asset_id
        source_row = self.rows[0]
        pin = dict(subject_id=source_row["subject_id"], purpose="prop", asset_id="image", revision_id="r2",
                   sha256="1"*64, media_sha256="2"*64)
        child = dict(id=self.child, project_id=self.pid, trace_id="c"*32, status="failed",
                     _image_recovery_from=self.jid, _image_recovery_model=self.model,
                     result={"image_recovery":{"status":"completed"}})
        self.jobs.append(child)
        report = dict(candidate=pin, trace_id=child["trace_id"], status="FAIL", policy_version=POLICY_VERSION,
                      anchor_pins=[{**pin,"subject_id":"prop:other","asset_id":"other"}],
                      checks=[dict(path="anchor_0", status="FAIL", required=True, frame_ids=["candidate","anchor_0"])])
        ref = dict(asset_id=review_asset_id(pin), revision_id="r1", sha256=digest_json(report))
        previous = {**report,"review_ref":ref}
        base = {**source_row,"job_id":self.child,"candidate":pin,"review":previous,"attempts":[]}
        self.catalog["entities"][0]["references"] = [pin, {**pin,"revision_id":"r1"}]
        self.catalog["store_path"] = str(self.root/"media.sqlite3")
        db = sqlite3.connect(self.catalog["store_path"])
        self.addCleanup(db.close)
        db.execute("CREATE TABLE assets(story_id, asset_id, kind, revision_id, sha256, seq)")
        db.execute("INSERT INTO assets VALUES(?,?,?,?,?,1)", ("story",ref["asset_id"],"reference_review","r1",ref["sha256"]))
        db.commit()
        self.service.sanitize = lambda x:x
        body = {**self.body,"after_reinspect":self.child}
        job = {"id":"9"*32,"kind":"asset_production","trace_id":"8"*32,"project_id":self.pid}
        for verdict in ("PASS","FAIL","UNKNOWN","exception"):
            with self.subTest(verdict=verdict):
                self.rows[0] = deepcopy(base)
                order = self.verify(body)
                self.assertEqual([(t["pin"],t["review_ref"]) for t in order["targets"]], [(pin,ref)])
                newref = {**ref,"revision_id":"r2"}
                fresh = {**previous,"visual_input_version":"reference-visual-input/2",
                         "status":verdict,"trace_id":job["trace_id"],"review_ref":newref,"checks":[]}
                with patch("pianchang.asset_dispatch._store_unit"), \
                     patch("pianchang.asset_review.review_studio_candidate",return_value=fresh,
                           side_effect=OSError("offline") if verdict=="exception" else None) as inspector, \
                     patch("pianchang.asset_review.run_reference_loop") as generate, \
                     patch("pianchang.asset_dispatch.select_verified_asset",return_value={"pin":pin}) as select:
                    result = run_image_recovery(self.service,job,order,self.catalog)["image_recovery"]
                generate.assert_not_called()
                self.assertEqual(inspector.call_args.kwargs["prior_visual_review"],previous)
                self.assertEqual(result["generated"],0)
                self.assertEqual(result["status"],"completed")
                self.assertEqual(result["reinspected"],int(verdict!="exception"))
                self.assertEqual(select.call_count,int(verdict=="PASS"))
                self.assertEqual(self.rows[0]["candidate"],pin)
                if verdict=="exception":
                    self.assertEqual(self.rows[0]["review"],previous)
        self.rows[0] = deepcopy(base)
        self.rows[0]["review"]["visual_input_version"]="reference-visual-input/2"
        with self.assertRaises(ValueError):
            self.verify(body)

    def test_one_explicit_revision_per_frozen_report_and_only_proven_route_retry(self):
        from pianchang.asset_review import POLICY_VERSION, review_asset_id
        from pianchang.image_recovery import _answer_item
        child = {"id":self.child,"project_id":self.pid,"trace_id":"7"*32,"status":"failed",
                 "_image_recovery_from":self.jid,"_image_recovery_model":self.model,
                 "result":{"image_recovery":{"status":"completed"}}}
        self.jobs.append(child)
        self.catalog["store_path"] = str(self.root/"media.sqlite3")
        db = sqlite3.connect(self.catalog["store_path"])
        self.addCleanup(db.close)
        db.execute("CREATE TABLE assets(story_id, asset_id, kind, revision_id, sha256, seq)")
        for i,row in enumerate(self.rows[:2]):
            pin = {"asset_id":"asset"+str(i),"revision_id":"r2","subject_id":row["subject_id"],"purpose":"prop",
                   "sha256":"a"*64,"media_sha256":"b"*64}
            report = {"candidate":pin,"trace_id":child["trace_id"],"status":"FAIL","policy_version":POLICY_VERSION,
                      "visual_input_version":"reference-visual-input/2","checks":[{"path":"subject","status":"FAIL",
                      "required":True,"frame_ids":["candidate"],"expected":"blue","actual":"red"}]}
            ref = {"asset_id":review_asset_id(pin),"revision_id":"r1","sha256":digest_json(report)}
            row.update(job_id=self.child,candidate=pin,review={**report,"review_ref":ref},
                       attempts=[{"pin":{**pin,"revision_id":rev},"review_ref":ref,"trace_id":child["trace_id"]} for rev in ("r1","r2")])
            self.catalog["entities"][i]["references"] = [pin]
            db.execute("INSERT INTO assets VALUES(?,?,?,?,?,1)", ("story",ref["asset_id"],"reference_review","r1",ref["sha256"]))
        db.commit()
        rid = "e"*64
        paid = {"request_kind":"image","http_status":200,"response_seq":7}
        calls = {(self.child,r["subject_id"]):[paid,paid] for r in self.rows[:2]}
        self.enterContext(patch("tools.workbench_loop_dependency._unit_calls",
                                side_effect=lambda root,j,s,p:calls.get((j["id"],s),[])))
        eligible = _review_targets(self.service,self.source,child,self.catalog,self.work,self.jobs,extra_revision=True)
        items = [_answer_item(t) for t in eligible]
        self.assertEqual(len(items),2)
        file = self.root/"outputs/workbench-loop/watch/answers"/(rid+".json")
        request = {"id":rid,"job_id":self.jid,"project_id":self.pid,"kind":"review_asset_candidates","revision_items":items,"items":items}
        saved = {"request_id":rid,"job_id":self.jid,"project_id":self.pid,"request":request,"trace_id":"6"*32,
                 "answers":{"authorize_revision":True}}
        body = {**self.body,"after_authorized_review":rid}
        for changes in [{"answers":{}},{"request":{**request,"kind":"other"}},{"project_id":"different"},
                        {"request":{**request,"revision_items":[{**items[0],"revision_id":"r1"}]}}]:
            write_json(file,{**saved,**changes})
            with self.assertRaises(ValueError):
                self.verify(body)
        write_json(file,saved)
        with self.assertRaises(ValueError):
            image_recovery_key(self.pid,{**body,"answer_id":rid})
        order = self.verify(body)
        self.assertEqual([t["remaining_attempts"] for t in order["targets"]],[1,1])
        self.assertEqual(image_recovery_key(self.pid,body), image_recovery_key(self.pid,{**body,"model_id":"changed"}))
        calls[(self.child,items[1]["subject_id"])].append({"request_kind":"image","dispatch":"unknown"})
        self.assertEqual(len(self.verify(body)["targets"]),1)
        calls[(self.child,items[1]["subject_id"])].pop()
        self.rows[1]["selected_pin"] = self.rows[1]["candidate"]
        self.assertEqual(len(self.verify(body)["targets"]),1)
        self.rows[1]["selected_pin"] = None
        job = {"id":"9"*32,"kind":"asset_production","trace_id":"d"*32,"project_id":self.pid,"status":"running",
               "_image_recovery_from":self.jid,"_image_recovery_model":self.model,"_image_revision_answer_id":rid}
        self.jobs.append(job)
        def denied(service,pid,job,catalog,unit,task,style):
            self.assertEqual(task["remaining_attempts"],1)
            self.assertEqual(task["repair_review_ref"],unit["review"]["review_ref"])
            unit.update(job_id=job["id"],trace_id=job["trace_id"],rejected_image_code="ModelNotOpen",rejected_image_model=self.model)
        with patch("pianchang.asset_dispatch._produce_item",side_effect=denied) as attempts:
            job["result"] = run_image_recovery(self.service,job,order,self.catalog)
        self.assertEqual(attempts.call_count,1)
        receipt = job["result"]["image_recovery"]
        self.assertEqual((receipt["revision_request_id"],receipt["max_generations_per_item"],receipt["generated"]),(rid,1,0))
        self.assertTrue(receipt["revision_attempts"][0]["unchanged"])
        job["status"]="failed"
        self.model="ep-different"
        route_body = {**body,"model_id":self.model,"after_model_rejection":job["id"]}
        with self.assertRaises(ValueError):
            self.verify(route_body)
        calls[(job["id"],items[0]["subject_id"])] = [{"request_kind":"image","provider_code":"ModelNotOpen",
            "http_status":404,"response_seq":11,"model_id":job["_image_recovery_model"]}]
        retry = self.verify(route_body)
        self.assertEqual([t["owner"] for t in retry["targets"]],[job["id"],self.child])
        self.assertTrue(all(t["remaining_attempts"]==1 for t in retry["targets"]))
        calls[(job["id"],items[0]["subject_id"])].append(paid)
        with self.assertRaises(ValueError):
            self.verify(route_body)
        self.model = job["_image_recovery_model"]
        receipt["status"] = "not_sent"
        calls[(job["id"],items[0]["subject_id"])] = [{"request_kind":"image","dispatch":"not_sent","phase":"start_tls",
            "request_seq":12,"error_seq":14,"error_type":"ConnectError","model_id":self.model}]
        with patch("tools.workbench_loop_ark.probe_image_transport",return_value={"status":"reachable","model_id":self.model}):
            reconnected = self.verify({**body,"after_not_sent":job["id"]})
        self.assertEqual([t["owner"] for t in reconnected["targets"]],[job["id"],self.child])
        def deliver(service,pid,job,catalog,unit,task,style):
            self.assertEqual(task["remaining_attempts"],1)
            self.assertEqual(task["parent_revision"],"r2")
            self.assertEqual(task["repair_review_ref"],unit["review"]["review_ref"])
            unit.update(job_id=job["id"],candidate={**unit["candidate"],"revision_id":"r3"},status="approved",
                        rejected_image_code=None)
            unit["selected_pin"] = unit["candidate"]
        latest = {"id":"a"*32,"kind":"asset_production","trace_id":"4"*32,"project_id":self.pid}
        with patch("pianchang.asset_dispatch._produce_item",side_effect=deliver) as sent:
            out = run_image_recovery(self.service,latest,reconnected,self.catalog)["image_recovery"]
        self.assertEqual((sent.call_count,out["generated"],out["selected"]),(2,2,2))
        with self.assertRaises(ValueError):
            self.verify(body)

    def test_v2_name_only_casting_report_reinspects_once_and_never_authorizes_regeneration(self):
        from copy import deepcopy
        from pianchang.asset_review import POLICY_VERSION, review_asset_id
        from pianchang.image_recovery import _unsupported_portrait_identity
        row = self.rows[0]
        row["purpose"] = self.source["result"]["asset_diagnostics"]["items"][0]["purpose"] = "portrait"
        subject = row["subject_id"]
        pin = dict(subject_id=subject,purpose="portrait",asset_id="portrait",revision_id="r2",sha256="3"*64,media_sha256="4"*64)
        child = {"id":self.child,"project_id":self.pid,"status":"failed","trace_id":"1"*32,
                 "_image_recovery_from":self.jid,"_image_recovery_model":self.model,"result":{"image_recovery":{"status":"completed"}}}
        self.jobs.append(child)
        brief = {"subject_id":subject,"name":"安宁","canonical":{"name":"安宁"},"purpose":"portrait","declared_refs":[],
                 "human_feedback":"","source_refs":[],"setup":[],"script":{},"shots":[],"shared_style":"","script_direction":{}}
        note = "设定未给出可辨认的专属标志，无法证实与该角色的身份一致。"
        report = {"candidate":pin,"trace_id":child["trace_id"],"visual_input_version":"reference-visual-input/2",
                  "policy_version":POLICY_VERSION,"status":"FAIL","brief":brief,"anchors":[],"anchor_pins":[],
                  "checks":[{"path":"subject","status":"FAIL","required":True,"frame_ids":["candidate"],"actual":note}] +
                  [{"path":p,"status":"PASS","required":True,"frame_ids":["candidate"]} for p in ("asset_use","style","palette","concept")]}
        self.assertTrue(_unsupported_portrait_identity(report))
        for edit in ("current_protocol","source_constraint","same_subject_anchor","other_failure","real_defect","missing_frame"):
            old = deepcopy(report)
            if edit=="current_protocol": old["visual_input_version"]="reference-visual-input/3"
            if edit=="source_constraint": old["brief"]["canonical"]["appearance"]="戴黑框眼镜"
            if edit=="same_subject_anchor":
                old["anchors"]=[{"pin":{**pin,"revision_id":"r1"}}]
                old["anchor_pins"]=[old["anchors"][0]["pin"]]
            if edit=="other_failure": old["checks"][1]["status"]="FAIL"
            if edit=="real_defect": old["checks"][0]["actual"]="原文明示穿蓝制服，候选实拍为红色。"
            if edit=="missing_frame": old["checks"][0]["frame_ids"]=[]
            self.assertFalse(_unsupported_portrait_identity(old),edit)
        ref = {"asset_id":review_asset_id(pin),"revision_id":"r2","sha256":digest_json(report)}
        row.update(candidate=pin,job_id=self.child,review={**report,"review_ref":ref},attempts=[])
        self.catalog["entities"][0]["references"]=[pin]
        self.catalog["store_path"]=str(self.root/"media.sqlite3")
        db=sqlite3.connect(self.catalog["store_path"])
        self.addCleanup(db.close)
        db.execute("CREATE TABLE assets(story_id, asset_id, kind, revision_id, sha256, seq)")
        db.execute("INSERT INTO assets VALUES(?,?,?,?,?,2)",("story",ref["asset_id"],"reference_review","r2",ref["sha256"]))
        db.commit()
        order=self.verify({**self.body,"after_reinspect":self.child})
        self.assertEqual([(t["pin"],t["review_ref"]) for t in order["targets"]],[(pin,ref)])
        for extra in (True,False):
            self.assertEqual(_review_targets(self.service,self.source,child,self.catalog,self.work,self.jobs,extra_revision=extra),[])
        rid = "a"*64
        item = {"asset_id":pin["asset_id"],"revision_id":"r2","candidate_job_id":self.child,"review_ref":ref,
                "purpose":"portrait","subject_id":subject}
        record = {"request_id":rid,"job_id":self.jid,"project_id":self.pid,"trace_id":"a"*32,"answers":{"authorize_revision":True},
                  "request":{"id":rid,"job_id":self.jid,"project_id":self.pid,"kind":"review_asset_candidates",
                             "items":[item],"revision_items":[item]}}
        write_json(self.root/"outputs/workbench-loop/watch/answers"/(rid+".json"),record)
        with self.assertRaises(ValueError):
            self.verify({**self.body,"after_authorized_review":rid})
