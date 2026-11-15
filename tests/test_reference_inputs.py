"""Offline compiler integration: real files/FFmpeg/Trace; explicitly mocked providers."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

from pianchang.bailian_text import TextConfig
from pianchang.bailian_video import VideoConfig,BailianVideoClient
from pianchang.collab_store import CollabStore
from pianchang.reference_assets import _blob_path,import_reference
from pianchang.reference_inputs import ReferenceInputs,plan_reference_inputs,verify_reference_bundle,reserve_external_review,external_review_usage
from pianchang.reference_transport import prepare_shot_request
from pianchang.reference_usage import query_reference_usage
from pianchang.story_spec import digest_json
from tests.test_bailian_video import Response
from tests.test_reference_integration import portrait_fixture
from trace_system import Tracer,use_context
from pianchang.video_agent import VideoRunner,verify_reference_receipt


class ReferenceInputTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.tracer=Tracer(self.root/"trace");self.enterContext(use_context(None,tracer=self.tracer))
        self.enterContext(self.tracer.storage_session());self.enterContext(self.tracer.trace("offline.reference_input_compiler",kind="test",inputs={"label":"explicit offline providers"}))
        self.store=CollabStore(self.root/"assets.sqlite3")
        self.config=VideoConfig(api_key="offline-placeholder",model="wan2.7-r2v",output_dir=self.root/"videos")

    def test_plans_preserve_all_eight_pins_and_explain_unrepresentable_voice_count(self):
        pins=[{"asset_id":"offline/"+str(i),"revision_id":"r1","sha256":"a"*64,"media_sha256":"b"*64,
               "subject_id":"char:"+str(i),"purpose":"portrait"} for i in range(8)]
        project={"shots":[{"id":"S001","required_refs":pins}]};original=copy.deepcopy(project)
        for strategy in ("identity_first","balanced_boards"):
            plan=plan_reference_inputs(project,"S001",self.config,strategy)
            self.assertEqual(plan["provider_image_count"],5)
            self.assertEqual(sorted(digest_json(p) for p in pins),sorted(digest_json(p) for g in plan["groups"] for p in g))
        voiced=[{**p,"asset_id":p["asset_id"]+"/voice","purpose":"voice"} for p in pins[:6]]
        plan=plan_reference_inputs({"shots":[{"id":"S001","required_refs":pins+voiced}]},"S001",self.config)
        self.assertTrue(plan["blockers"]);self.assertEqual(plan["next_action"],"request_specification_change")
        self.assertEqual(project,original)

    def test_real_boards_wire_receipt_recovery_and_tamper_rejection(self):
        pins=[]
        for i in range(8):
            path=self.root/("source-"+str(i)+".png");path.write_bytes(portrait_fixture(20+i*20))
            pins.append(import_reference(self.store,story_id="offline-story",asset_id="offline-story/ref/"+str(i),
                subject_id=("char:" if i<3 else "prop:")+str(i),purpose="portrait" if i<3 else "prop",path=path))
        project={"shots":[{"id":"S001","required_refs":pins}]};original=copy.deepcopy(project);reservations=[]
        compiler=ReferenceInputs(store=self.store,story_id="offline-story",project=project,directory=self.root/"inputs",config=self.config,
            vision_config=TextConfig(api_key="offline-placeholder"),reserve_review=lambda:reservations.append("offline review reserved"))
        def review(frames,criteria):
            return {"data":{"observations":[{"criterion_id":c["criterion_id"],"verdict":"PASS","reason":"explicit offline matched-panel fixture",
                    "frame_ids":c["required_frames"]} for c in criteria]},"model":"offline-no-network","request_id":"offline-board-review","usage":{"total_tokens":0}}
        with patch("pianchang.reference_inputs.BoardObserver.complete_observations",side_effect=review) as observer:
            ready=compiler.prepare("S001")
            self.assertTrue(ready["ready"]);self.assertEqual(observer.call_count,1)
            again=compiler.prepare("S001");self.assertEqual(observer.call_count,1);self.assertEqual(len(reservations),1)
        bundle=ready["selected"]["bundle"]
        manifest=verify_reference_bundle(bundle,source_refs=pins,story_id="offline-story",project_sha256=digest_json(project),shot_id="S001",config=self.config)
        self.assertEqual(len(manifest["slots"]),5)
        self.assertEqual(project,original)
        self.assertEqual(len([p for s in manifest["slots"] for p in s["source_refs"]]),8)
        # A crash after the actual review receipt but before final publication must not re-bill it.
        plan=compiler._plan("S001","identity_first");state_path=compiler._path(plan);state=json.loads(state_path.read_text("utf-8"))
        state.update(status="review_requested");state.pop("review");state.pop("bundle");state_path.write_text(json.dumps(state),encoding="utf-8")
        with patch("pianchang.reference_inputs.BoardObserver.complete_observations",side_effect=AssertionError("must reconcile original response")):
            self.assertTrue(compiler.prepare("S001")["ready"])
        metadata={"shot_id":"S001","required_refs":pins,"project_sha256":digest_json(project),"reference_story_id":"offline-story","story_id":"offline-story","reference_bundle":bundle}
        compiled={"prompt":"explicit offline video fixture","duration":5,"metadata":metadata}
        payload,resolved=prepare_shot_request(self.config,compiled,store=self.store,story_id="offline-story")
        self.assertEqual(len(payload["input"]["media"]),5);self.assertEqual(len(metadata["required_refs"]),8)
        client=BailianVideoClient(self.config);opener=Mock()
        response=Response(json.dumps({"output":{"task_id":"offline-derived-video","task_status":"PENDING"},"request_id":"offline-task"}).encode())
        response.status=200;opener.open.return_value=response;client._api_opener=opener
        runner=VideoRunner(self.config,client=client)
        submitted=runner.submit(payload,metadata,resolved_refs=resolved)
        receipt=submitted["reference_receipt"]
        self.assertEqual(receipt["schema_version"],"video-reference-receipt/2")
        self.assertEqual(len(receipt["required_refs"]),8);self.assertEqual(len(receipt["submitted_refs"]),5)
        self.assertEqual(verify_reference_receipt(submitted)["status"],"PASS")
        derived=next(p for p in pins if not any(p==s["provider_ref"] for s in manifest["slots"]))
        usage=query_reference_usage(self.store,derived["asset_id"],derived["revision_id"],jobs_dir=self.config.output_dir,story_id="offline-story")
        self.assertEqual(usage["derived_accepted_count"],1)
        self.assertEqual(usage["submitted_count"],0,"A source inside a board is never claimed as an original direct submission")
        self.assertTrue(usage["candidates"][0]["derived_submitted_refs"])
        # Required source pins must never be relabelled as directly submitted bytes.
        self.assertNotEqual(receipt["required_refs"],receipt["submitted_refs"])
        wrong=copy.deepcopy(submitted);wrong["metadata"]["required_refs"]=pins[:-1]
        self.assertEqual(verify_reference_receipt(wrong)["status"],"FAIL")
        with self.assertRaises(ValueError):verify_reference_bundle(bundle,source_refs=pins,story_id="other-story",project_sha256=digest_json(project),shot_id="S001")
        board=next(s["provider_ref"] for s in manifest["slots"] if s["board"])
        file=_blob_path(self.store,board["media_sha256"]);data=file.read_bytes();file.write_bytes(data+b"tamper")
        try:self.assertEqual(verify_reference_receipt(submitted)["status"],"FAIL")
        finally:file.write_bytes(data)

    def test_unconfirmed_review_never_replays_and_other_shots_remain_plannable(self):
        # Reconciliation is keyed by the original task/span, not any other review.
        pins=[{"asset_id":"offline/"+str(i),"revision_id":"r1","sha256":"a"*64,"media_sha256":"b"*64,
               "subject_id":"prop:"+str(i),"purpose":"prop"} for i in range(6)]
        project={"shots":[{"id":"S001","required_refs":pins},{"id":"S002","required_refs":pins}]}
        compiler=ReferenceInputs(store=self.store,story_id="offline-story",project=project,directory=self.root/"inputs",config=self.config)
        plan=compiler._plan("S001","identity_first");path=compiler._path(plan);path.parent.mkdir(parents=True)
        from trace_system import current_context
        path.write_text(json.dumps({"plan":plan,"status":"review_requested","review_context":current_context()}),encoding="utf-8")
        self.assertEqual(compiler.prepare("S001")["status"],"unconfirmed_review")
        self.assertEqual(compiler.read("S002")["strategies"][0]["status"],"unprepared")

    def test_reference_review_reservations_share_the_original_production_model_limit(self):
        folder=self.root/"commission/reference-inputs";checkpoint=folder.parent/"agent/run.json"
        checkpoint.parent.mkdir(parents=True);checkpoint.write_text(json.dumps({"steps":2}),encoding="utf-8")
        reserve_external_review(folder,3)
        self.assertEqual(len(external_review_usage(folder)["reservations"]),1)
        with self.assertRaisesRegex(ValueError,"额度不足"):reserve_external_review(folder,3)
        from pi_bridge.runtime import PiLoop
        from types import SimpleNamespace
        loop=PiLoop(SimpleNamespace(config=TextConfig()),[],self.root/"loop",snapshot={},finish_check=lambda:{"complete":False},
                    external_model_usage=lambda:len(external_review_usage(folder)["reservations"]))
        loop.record={"steps":2,"limits":{"max_steps":3}}
        with self.assertRaisesRegex(ValueError,"额度已用完"):loop.before_model({})
        self.assertEqual(loop.record["steps"],2)

    def test_visual_pass_without_both_actual_images_is_not_accepted(self):
        from pianchang.reference_inputs import _review_checks
        result={"response":{"data":{"observations":[{"criterion_id":"preserved_1","verdict":"PASS","reason":"offline claim without original image","frame_ids":["board_0"]}]}},
            "frame_hashes":{"board_0":"a"*64,"source_0":"b"*64},
            "criteria":[{"criterion_id":"preserved_1","required_frames":["board_0","source_0"],"expected":"compare both actual images"}]}
        self.assertEqual(_review_checks(result)[0]["status"],"UNKNOWN")

    def test_failed_strategy_is_retained_while_other_shot_and_changed_strategy_proceed(self):
        pins=[]
        for i in range(6):
            file=self.root/("alternate-"+str(i)+".png");file.write_bytes(portrait_fixture(30+20*i))
            pins.append(import_reference(self.store,story_id="offline-story",asset_id="offline-story/alternate/"+str(i),
                subject_id="prop:"+str(i),purpose="prop",path=file))
        project={"shots":[{"id":"S001","required_refs":pins},{"id":"S002","required_refs":pins}]}
        compiler=ReferenceInputs(store=self.store,story_id="offline-story",project=project,directory=self.root/"alternates",config=self.config,
            vision_config=TextConfig(api_key="offline-placeholder"),reserve_review=lambda:None)
        calls=[]
        def observe(frames,criteria):
            calls.append([f["sha256"] for f in frames])
            return {"data":{"observations":[{"criterion_id":c["criterion_id"],"verdict":"FAIL" if len(calls)==1 else "PASS",
                "reason":"explicit offline unreadable panel" if len(calls)==1 else "explicit offline actual panel matched",
                "frame_ids":c["required_frames"]} for c in criteria]},"request_id":"offline-alternate-review","model":"offline-no-network"}
        with patch("pianchang.reference_inputs.BoardObserver.complete_observations",side_effect=observe):
            self.assertFalse(compiler.prepare("S001")["ready"])
            self.assertFalse(compiler.prepare("S001")["ready"]);self.assertEqual(len(calls),1,"A failed strategy is not blindly rebilled")
            self.assertTrue(compiler.prepare("S002")["ready"],"Other shots continue independently")
            ready=compiler.prepare("S001","balanced_boards");self.assertTrue(ready["ready"])
            self.assertEqual([p["status"] for p in ready["strategies"]],["FAIL","ready"])
            self.assertNotEqual(calls[0][0],calls[2][0],"The alternate really changes board pixels, not only the strategy name")


if __name__=="__main__":unittest.main()
