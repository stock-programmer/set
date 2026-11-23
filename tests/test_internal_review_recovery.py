import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pianchang.internal_review_recovery import apply_internal_cuts
from pianchang.story_spec import digest_json
from trace_system import Tracer,trace,use_context


class InternalCutPublicationTests(unittest.TestCase):
    def setUp(self):
        root = self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(use_context(None,tracer=Tracer(root/"trace")))
        self.enterContext(trace("offline.internal_review.publish"))
        def write(path,data):
            path.parent.mkdir(exist_ok=True,parents=True)
            path.write_text(json.dumps(data),encoding="utf-8")
            return {"path":str(path),"sha256":digest_json(data)}
        self.write = write
        self.shot = {"id":"G1"}
        self.cid = "internal:S1->S2"
        self.other = {"criterion_id":"continuity:G1->G2","status":"UNKNOWN","required":True,
                      "evidence":[],"execution_status":"execution_error"}
        self.work = root/"cv2/G1"
        source = {"path":str(root/"clip.mp4"),"sha256":"original"}
        timing = write(self.work/"model/timing/receipt-p4.json",{"context":{"shot":self.shot},
                       "video":{"binding":{"source":source}}})
        ref = write(self.work/"neighbors/receipt.json",{"context":{"left":"G1","right":"G2"},"rows":[self.other]})
        target = {"criterion_id":self.cid,"status":"UNKNOWN","required":True,"evidence":[]}
        self.evidence = {"selected_media":{"G1":source},"receipts":[timing,ref],"rows":[self.other]}
        folder = self.work/"old"
        self.report = {"results":[target,self.other],"content_evidence_sha256":digest_json(self.evidence)}
        old = write(folder/"report.json",self.report)
        write(folder/"contract.json",{"requirements":[{"kind":"internal_cut","criterion_id":self.cid,"creative_pair":["S1","S2"]}]})
        write(folder/"content.json",self.evidence)
        frame = write(folder/"inspection.json",{})
        self.record = {"report_path":old["path"],"report_sha256":old["sha256"],"inspection_path":frame["path"]}
        self.index = root/"reports.json"
        write(self.index,{"G1":self.record})
        self.ws = SimpleNamespace(project={"shots":[self.shot]},shots={"G1":{"media_sha256":"original","media_path":source["path"]}},
                                  reports={"G1":self.record},directory=root)
        self.scope = {"criteria":{"G1":[self.cid]},"internal_cuts":{"G1":{"report":old["sha256"],"timing":timing,
                      "criteria":{self.cid:{"mode":"new","folder":str(self.work/"internal-test")}}}}}
        self.result = {"receipt_path":"new-cut","receipt_sha256":"new-hash","execution_status":"executed",
                       "rows":[{**target,"status":"FAIL","evidence":[{"asset_id":"A0"}]}]}

    def test_only_the_selected_internal_row_and_receipt_are_appended(self):
        def publish(ws,sid,contract,inspection,evidence,work):
            self.assertEqual(evidence["rows"],[self.other,self.result["rows"][0]])
            self.assertEqual(evidence["receipts"][:-1],self.evidence["receipts"])
            self.assertEqual(evidence["receipts"][-1]["path"],self.result["receipt_path"])
            report = self.write(work/"new/report.json",{"results":evidence["rows"]})
            ws.reports[sid] = {"report_path":report["path"],"report_sha256":report["sha256"]}
        with patch("pianchang.internal_review_recovery.validate_content_evidence"), \
             patch("pianchang.internal_review_recovery.review_internal_cut",return_value=self.result) as observe, \
             patch("pianchang.internal_review_recovery.publish_content_report",side_effect=publish):
            self.assertEqual(apply_internal_cuts(self.ws,self.scope),1)
        self.assertEqual(observe.call_args.kwargs["bound_cut"],self.scope["internal_cuts"]["G1"]["criteria"][self.cid])
        self.assertEqual(json.loads(self.index.read_bytes())["G1"],self.ws.reports["G1"])
        self.assertEqual(json.loads(Path(self.record["report_path"]).read_bytes()),self.report)

    def test_stale_selected_media_or_new_unrelated_evidence_cannot_change_the_index(self):
        original = self.index.read_bytes()
        self.ws.shots["G1"]["media_sha256"]="different"
        with patch("pianchang.internal_review_recovery.review_internal_cut") as call, self.assertRaisesRegex(ValueError,"范围"):
            apply_internal_cuts(self.ws,self.scope)
        call.assert_not_called()
        self.ws.shots["G1"]["media_sha256"]="original"
        modified = self.write(self.work/"draft/report.json",{"results":[self.result["rows"][0],{**self.other,"evidence":["other"]}]})
        def publish(ws,*args):
            ws.reports["G1"]={"report_path":modified["path"],"report_sha256":modified["sha256"]}
        with patch("pianchang.internal_review_recovery.validate_content_evidence"), \
             patch("pianchang.internal_review_recovery.review_internal_cut",return_value=self.result), \
             patch("pianchang.internal_review_recovery.publish_content_report",side_effect=publish), \
             self.assertRaisesRegex(ValueError,"范围外"):
            apply_internal_cuts(self.ws,self.scope)
        self.assertEqual(self.index.read_bytes(),original)
