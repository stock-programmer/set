"""Offline selection tests. Raw observations are synthetic and never dispatched."""
from contextlib import nullcontext
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pianchang.internal_review_scope import _cached_cut, _cut_mode, internal_cut_scope
from pianchang.content_review import NORMALIZATION_VERSION, VERSION
from pianchang.openai_review import merge_cut
from pianchang.story_spec import digest_json


class InternalReviewScopeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cid = "internal:L->R"
        self.folder = self.root/("internal-"+digest_json(["L","R"])[:16])
        self.fp = {"context":{"cut_s":2.0},"references":[],"video":{"duration_s":4.0},"criteria":[{"criterion_id":self.cid}]}
        self.d = self.folder/digest_json(self.fp)[:12]
        self.d.mkdir(parents=True)
        def response(modality, components=False):
            row = {"criterion_id":self.cid,"verdict":"PASS","reason":"offline","evidence":[
                {"asset_id":modality,"start_s":1,"end_s":2},{"asset_id":modality,"start_s":2,"end_s":3}]}
            if components:
                row["components"] = [{"name":key,"verdict":"PASS"} for key in ("identity","costume_props","space","action")]
            return {"request_id":"synthetic","data":{"observations":[row]}}
        a = response("A0")
        v = response("V0",True)
        self.audio = {"status":"completed","request":self.fp,"input_sha256":digest_json(self.fp),"response":a}
        req = {**self.fp,"audio_sha256":digest_json(a)}
        self.visual = {"status":"completed","request":req,"input_sha256":digest_json(req),"response":v}
        self.put(self.d/"audio-call.json",self.audio)
        self.put(self.d/"visual-call.json",self.visual)
        self.raw = {**self.fp,"version":VERSION,"normalization_version":NORMALIZATION_VERSION,"request_sha256":digest_json(self.fp),"execution_status":"execution_error",
                    "rows":[{"criterion_id":self.cid,"status":"UNKNOWN","execution_status":"execution_error"}]}
        self.put(self.d/"receipt-p4.json",self.raw)

    @staticmethod
    def put(path, value):
        path.write_text(json.dumps(value),encoding="utf8")

    def test_two_completed_modalities_replay_and_partial_publication_never_resends(self):
        check = lambda: _cached_cut(self.folder,self.fp,self.cid)
        files = {p:p.read_bytes() for p in self.d.iterdir()}
        original = {"path":str(self.d/"receipt-p4.json"),"sha256":digest_json(self.raw)}
        self.assertEqual(check()["mode"],"assemble")
        self.assertEqual(files,{p:p.read_bytes() for p in self.d.iterdir()})
        self.assertIsNone(_cached_cut(self.folder,self.fp,self.cid,{**original,"sha256":"0"*64}))
        aggregate = {"status":"completed","request":self.fp,"input_sha256":digest_json(self.fp),
                     "response":merge_cut(self.visual["response"],self.audio["response"],2)}
        self.put(self.d/"call.json",aggregate)
        self.assertEqual(check()["mode"],"assemble")
        completed = {**self.raw,"execution_status":"executed","normalized_from":original,
                     "call_path":str(self.d/"call.json"),"response":aggregate["response"],"call_sha256":digest_json(aggregate),
                     "rows":[{"criterion_id":self.cid,"execution_status":"executed","status":"PASS"}]}
        self.put(self.d/"receipt-cut-assembly-v2.json",completed)
        self.assertEqual(check()["mode"],"cached")
        for name in ("receipt-p4.json","receipt-cut-assembly-v2.json"):
            (self.d/name).unlink()
        for name in ("audio-call.lock","visual-call.lock"):
            (self.d/name).touch()
        self.assertEqual(check()["mode"],"assemble_parts")
        self.assertIsNone(_cached_cut(self.folder,self.fp,self.cid,original))
        self.put(self.d/"unconfirmed-attempt.json",{})
        self.assertIsNone(check())

    def test_mismatched_or_incomplete_raw_call_cannot_be_replayed(self):
        for bad in ({**self.audio,"status":"submitted_or_unknown"},{**self.audio,"input_sha256":"f"*64},
                    {**self.audio,"response":{"data":{"observations":[]}}}):
            self.put(self.d/"audio-call.json",bad)
            self.assertIsNone(_cached_cut(self.folder,self.fp,self.cid))
        self.put(self.d/"audio-call.json",self.audio)
        self.put(self.d/"visual-call.json",{**self.visual,"request":{**self.visual["request"],"audio_sha256":"f"*64}})
        self.assertIsNone(_cached_cut(self.folder,self.fp,self.cid))

    def test_new_requires_actual_ordered_windows_clean_directory_and_two_calls(self):
        base = self.root/"never-executed"
        rows = [{"criterion_id":"creative:"+sid,"execution_status":"executed","status":"PASS",
                 "evidence":[{"asset_id":"V0","start_s":start,"end_s":end}]} for sid,start,end in (("L",0,1),("R",2,3))]
        timing = {"rows":rows,"response":{},"video":{},"references":[],"criteria":[],"context":{}}
        cut = {"criterion_id":self.cid,"creative_pair":["L","R"]}
        config = SimpleNamespace(api_key="test",continuity_config=SimpleNamespace(api_key="test"))
        shot = {"creative_shots":[{"id":"L"},{"id":"R"}]}
        def mode(n=2):
            return _cut_mode(config,base,shot,{},timing,cut,[],{"execution_status":"not_run"},None,n)
        with patch("pianchang.content_review.normalize_observations",return_value=rows) as normalize:
            self.assertEqual(mode()["mode"],"new")
            self.assertIsNone(mode(1))
            normalize.return_value = rows[:1]
            self.assertIsNone(mode())
            normalize.return_value = rows
            Path(mode()["folder"]).mkdir(parents=True)
            self.assertIsNone(mode())

    def test_local_assembly_precedes_network_and_works_with_zero_remaining(self):
        work = self.root/"commission"
        shot = {"id":"G1"}
        project = {"shots":[shot]}
        view = {"project_id":"project","script_ref":"s","spec":"v","project":project}
        clip = {"media_sha256":"m","media_path":"from-snapshot"}
        snapshot = {"request_id":"c","job_id":"j","retake_stop":True,"spec_ref":"v","script_ref":"s",
                    "project_sha256":digest_json(project),"available":{"G1":{"clip":clip}},"sha256":"scope"}
        criteria = [{"criterion_id":k,"kind":"internal_cut","required":True} for k in ("internal:A->B","internal:B->C")]
        contract = {"requirements":criteria,"shot_sha256":digest_json(shot)}
        inspection = {"media_sha256":"m"}
        evidence = {"rows":[],"receipts":[],"selected_media":{"G1":{"path":"from-snapshot","sha256":"m"}}}
        report = {"inspection_sha256":digest_json(inspection),"content_evidence_sha256":digest_json(evidence),
                  "contract_sha256":digest_json(contract),"media_sha256":"m","project_sha256":digest_json(project),
                  "results":[{**r,"status":"UNKNOWN"} for r in criteria]}
        directory = work/"cv2/G1/report"
        directory.mkdir(parents=True)
        for name,value in (("report",report),("inspection",inspection),("content",evidence),("contract",contract)):
            self.put(directory/(name+".json"),value)
        reports = {"G1":(digest_json(report),[],{r["criterion_id"] for r in criteria})}
        self.put(work/"reports.json",{"G1":{"report_path":str(directory/"report.json"),"report_sha256":digest_json(report),
            "inspection_path":str(directory/"inspection.json"),"content_work_path":str(directory.parent)}})
        route=SimpleNamespace(api_key="test",model="model",base_url="https://example.invalid",adapter="openai")
        route.continuity_config=route
        service=SimpleNamespace(root=self.root,state={"jobs":[]},lock=nullcontext(),vision_config=route)
        owner={"id":"j","kind":"studio_video"}
        def decide(cfg,base,shot,clip,timing,criterion,refs,row,previous,remaining):
            return {"mode":"assemble"} if criterion==criteria[1] else {"mode":"new"} if remaining>=2 else None
        with patch("pianchang.internal_review_scope.sidecar_commission",return_value={"submitted":["j"]}), \
             patch("pianchang.internal_review_scope._timing",return_value=({"sha256":"t"},{})), \
             patch("pianchang.internal_review_scope._cut_mode",side_effect=decide):
            for remaining in (0,2):
                scope = internal_cut_scope(service,view,snapshot,owner,work,reports,remaining)
                self.assertEqual(scope["criteria"],{"G1":["internal:B->C"]})
                self.assertEqual(scope["urls"],[])
            self.assertIsNone(internal_cut_scope(service,view,{**snapshot,"active":True},owner,work,reports,2))
