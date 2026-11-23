"""The stopped commission rechecks only a published picture-only beat on its selected film."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pianchang.content_review import VERSION, _selected_sparse_observations
from pianchang.creative_sound import POLICY
from pianchang.legacy_sound_review import _current_edges, _replace_stale_edges, apply_legacy_sound, legacy_sound_scope
from pianchang.story_spec import digest_json


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False),encoding="utf-8")
    return {"path":str(path),"sha256":digest_json(value)}


class LegacySoundReviewTests(unittest.TestCase):
    def setUp(self):
        root = self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.cid, self.original, self.driver = "a"*32,"b"*32,"c"*32
        self.owner = {"id":"d"*32,"kind":"pi_video","status":"succeeded","project_id":"pid"}
        watch = root/"outputs/workbench-loop"
        write(watch/"watch/state.json", {"latest":{self.original:"event"},
              "events":{"event":{"job_id":self.original,"kind":"studio_video","run_id":self.driver}}})
        write(watch/self.driver/"run.json",{"id":self.driver,"role":"video","project_id":"pid",
              "commission_id":self.cid,"origin_job_id":self.original,"submitted":[self.original,self.owner["id"]]})
        config = SimpleNamespace(model="fixture",api_key="offline",adapter="openai",base_url="https://offline.invalid")
        self.service = SimpleNamespace(root=root,lock=threading.RLock(),state={"jobs":[self.owner]},vision_config=config)
        config.continuity_config = config
        shot = {"id":"G001","creative_shots":[{"id":"S001","description":"行情闪烁，财经主播播报市场下跌。"}]}
        self.view = {"project_id":"pid","project":{"shots":[shot]},"script_ref":{"rev":"s"},"spec":{"rev":"p"}}
        self.work = root/"commission"
        self.clip = {"media_sha256":"1"*64,"media_path":str(root/"old.mp4")}
        self.snapshot = {"request_id":self.cid,"job_id":self.original,"retake_stop":"retake limit","script_ref":self.view["script_ref"],
                         "spec_ref":self.view["spec"],"available":{"G001":{"clip":self.clip}},"sha256":"snapshot"}
        self.rows = [{"criterion_id":"creative:S001","status":"FAIL","execution_status":"executed"},
                     {"criterion_id":"continuity:G001->G002","status":"UNKNOWN"}]
        path = self.work/"cv2/G001/model/original/receipt-p4.json"
        call = write(path.parent/"call.json",{"status":"completed","response":{"request_id":"old-receipt"}})
        self.timing = write(path,{"version":VERSION,"execution_status":"executed","call_path":call["path"],"response":{"request_id":"old-receipt"},
                         "call_sha256":call["sha256"],"criteria":[{"criterion_id":"creative:S001"}],
                         "video":{"duration_s":3.0,"binding":{"source":{"sha256":self.clip["media_sha256"],"path":self.clip["media_path"]}}},
                         "rows":self.rows,"context":{"shot":shot}})
        self.evidence = {"rows":self.rows,"receipts":[self.timing]}
        folder = self.work/"cv2/G001/old"
        write(folder/"contract.json",{})
        write(folder/"content.json",self.evidence)
        report = write(folder/"report.json",{"results":self.rows,"content_review_version":VERSION,
                                             "content_evidence_sha256":digest_json(self.evidence)})
        self.record = {"report_sha256":report["sha256"],"report_path":report["path"],"inspection_path":str(folder/"inspection.json")}
        write(folder/"inspection.json",{})
        write(self.work/"reports.json",{"G001":self.record})
        self.reports = {"G001":(report["sha256"],[self.timing],set())}

    def scope(self, remaining=4):
        return legacy_sound_scope(self.service,self.view,self.snapshot,self.owner,self.work,self.reports,remaining)

    def test_takeover_current_footage_closed_jobs_policy_and_paired_budget(self):
        result = self.scope()
        self.assertEqual(result["criteria"],{"G001":["creative:S001"]})
        self.assertEqual(result["key"],self.scope()["key"])
        self.assertIsNone(self.scope(1))
        self.service.state["jobs"].append({"id":"new","project_id":"pid","kind":"studio_video","status":"queued"})
        self.assertIsNone(self.scope())
        self.service.state["jobs"].pop()
        self.snapshot["spec_ref"] = {"rev":"old"}
        self.assertIsNone(self.scope())
        self.snapshot["spec_ref"] = self.view["spec"]
        self.owner["id"] = "unrelated-worker"
        self.assertIsNone(self.scope())
        self.owner["id"] = "d"*32
        self.clip["media_sha256"] = "2"*64
        self.assertIsNone(self.scope())

    def test_scoped_append_preserves_unknown_edge_and_old_report(self):
        scope = self.scope()
        workspace = SimpleNamespace(project=self.view["project"],directory=self.work,shots={"G001":self.clip},reports={"G001":self.record})
        old = Path(self.record["report_path"]).read_bytes()
        observation = {"rows":[{**self.rows[0],"status":"UNKNOWN","creative_audio_policy":POLICY}],
                       "execution_status":"executed","receipt_path":"offline-observation","receipt_sha256":"offline"}
        with patch("pianchang.legacy_sound_review.validate_content_evidence") as validate, \
             patch("pianchang.legacy_sound_review.review_creative_sound",return_value=observation) as model, \
             patch("pianchang.legacy_sound_review.publish_content_report") as publish:
            self.assertEqual(apply_legacy_sound(workspace,scope),1)
        self.assertEqual(Path(self.record["report_path"]).read_bytes(),old)
        self.assertEqual(validate.call_count,2)
        model.assert_called_once()
        evidence = publish.call_args.args[4]
        self.assertEqual(evidence["rows"],[observation["rows"][0],self.rows[1]])
        self.assertEqual(evidence["receipts"][0],self.timing)

    def test_first_take_observations_cannot_replace_the_selected_retake(self):
        contract,inspection = {"project":"same"},{"media_sha256":"retake"}
        binding = {"contract_sha256":digest_json(contract),"inspection_sha256":digest_json(inspection),
                   "media_sha256":"retake"}
        base = self.work/"reviews/shot"
        selected = base/"candidate-retake"
        write(base/"observations.json",{**binding,"inspection_sha256":"old-frame-set","media_sha256":"first"})
        receipt = write(selected/"observations.json",{**binding,"frames":["current"]})
        report = write(self.work/"cv2/published/report.json",{
            **binding,"observations_sha256":receipt["sha256"],"results":[{"criterion_id":"evidence_integrity","status":"PASS"}]})
        record = {"report_path":report["path"],"report_sha256":report["sha256"],
                  "inspection_path":str(selected/"inspection/inspection.json")}
        from trace_system import trace
        with trace("offline.selected_take"):
            self.assertEqual(digest_json(_selected_sparse_observations(record,contract,inspection,base)),receipt["sha256"])
            write(selected/"observations.json",{**binding,"frames":["replaced"]})
            with self.assertRaisesRegex(ValueError,"抽帧回执"):
                _selected_sparse_observations(record,contract,inspection,base)

    def test_completed_receipt_enqueues_local_publication_even_with_zero_budget(self):
        from pianchang.review_recovery import submit_review_scope
        cached = {"path":str(self.work/"creative-sound/model/receipt-p4.json"),"sha256":"4"*64}
        old = self.scope()["key"]
        self.service.vision_config.api_key = ""
        self.service.submit = Mock(return_value={"id":"local-publication"})
        with patch("pianchang.legacy_sound_review._completed_sound_receipt",return_value=cached):
            scope = self.scope(0)
            self.assertEqual(scope["legacy_audio"]["G001"]["cached_receipt"],cached)
            self.assertEqual(scope["urls"],[])
            self.assertNotEqual(scope["key"],old)
            with patch("pianchang.review_recovery._reachable",side_effect=AssertionError("network probe")):
                self.assertEqual(submit_review_scope(self.service,"pid",self.snapshot,scope,[]),("local-publication",False))
                self.assertEqual(submit_review_scope(self.service,"pid",self.snapshot,self.scope(0),
                                 [{"_idempotency_key":scope["key"]}]),(None,False))
        self.service.submit.assert_called_once()

    def test_startup_retake_stop_and_watch_use_same_native_review_and_key(self):
        from pianchang.review_recovery import poll_review_retries
        from pianchang.studio_recovery import enqueue_incomplete_reviews
        origin = {"id":self.original,"kind":"studio_video","status":"failed","project_id":"pid","message_id":self.cid}
        self.service.state["jobs"].insert(0,origin)
        self.service.list_jobs = lambda: list(self.service.state["jobs"])
        self.snapshot.update(active=False)
        self.view["store_path"] = str(self.root/"assets.sqlite3")
        self.view["run_id"] = "1"*32
        result = self.scope()
        def accept(kind,pid,body,key):
            job = {"id":"accepted","kind":kind,"project_id":pid,"_idempotency_key":key,"status":"queued"}
            self.service.state["jobs"].append(job)
            return job
        self.service.submit = Mock(side_effect=accept)
        with patch("pianchang.studio_assets.studio_project_for_video",return_value=self.view), \
             patch("pianchang.studio_recovery.read_incomplete_production",return_value=self.snapshot), \
             patch("pianchang.studio_retakes._latest_request",return_value=(None,None)), \
             patch("pianchang.review_recovery.review_retry",return_value=result), \
             patch("pianchang.review_recovery._reachable",return_value=True):
            self.assertEqual(enqueue_incomplete_reviews(self.service)["jobs"],["accepted"])
            poll_review_retries(self.service,{})
        self.service.submit.assert_called_once_with("studio_review","pid",{"continuation":{"request_id":self.cid,"sha256":"snapshot"},
            "retry_observation":result["token"]},result["key"])

    def test_neighbor_rebinding_requires_selected_versions_manifest_cut_and_same_source_order(self):
        from pianchang.acceptance import file_sha256
        left,right = {"id":"A"},{"id":"B"}
        clips = {sid:{"media_sha256":sid*64,"media_path":str(self.root/(sid+".mp4"))} for sid in ("A","B")}
        segments = [{"shot_id":sid,"source":{"path":c["media_path"],"sha256":c["media_sha256"]},
                     "segment":{"sha256":sid}} for sid,c in clips.items()]
        manifest = self.root/"export.json"
        write(manifest,{"clips":[{"shot_id":s,"sha256":s,"preview_duration_s":3} for s in clips],"preview_sha256":"preview"})
        cid = "continuity:A->B"
        context = {"left":"A","right":"B","left_shot":left,"right_shot":right,"source_segments":segments,
                   "cut_review_scope":{"source_segments":segments},"cut_s":3,"preview":{"sha256":"preview"},
                   "export_manifest":{"path":str(manifest),"sha256":file_sha256(manifest)}}
        data = {"context":context,"rows":[{"criterion_id":cid,"status":"UNKNOWN"}],"video":{"binding":{"source":{"sha256":"preview"}}}}
        path = self.work/"cv2/pair/current.json"
        def replace():
            ref = write(path,data)
            edges = _current_edges(self.work,{"B":("digest",[ref],set())},clips,{"shots":[left,right]})
            return _replace_stale_edges({"selected_media":{"B":{"sha256":"old","path":"old"}},"rows":data["rows"]},clips,edges)
        self.assertEqual(replace()[cid]["path"],str(path))
        context["cut_s"] = 4
        self.assertIsNone(replace())
        context["cut_s"] = 3
        segments[1]["source"]["sha256"] = "previous-candidate"
        self.assertIsNone(replace())
