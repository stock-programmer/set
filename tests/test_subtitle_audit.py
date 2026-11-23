"""Explicit offline protocol/evidence fixtures; no billable model calls.

The fake JPEG/video bytes test binding and transport, not visual correctness.
The live project must separately perform the actual frame transcription.
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

from pianchang.acceptance import file_sha256
from pianchang.bailian_text import TextConfig
from pianchang.content_review import VERSION,_asset,_write,validate_content_evidence
from pianchang.story_spec import digest_json
from pianchang.subtitle_audit import SubtitleFrameObserver,audit_subtitle_frames,prepare_subtitle_frames
from trace_system import TraceError,Tracer,use_context


class SubtitleAuditTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        tracer=Tracer(self.root/"trace");binding=use_context(None,tracer=tracer);binding.__enter__();self.addCleanup(binding.__exit__,None,None,None)
        source=self.root/"offline-fixture.mp4";source.write_bytes(b"explicit offline source bytes")
        self.clip={"media_path":str(source),"media_sha256":file_sha256(source)}
        self.video={**_asset(source),"duration_s":5.0,"binding":{"source":_asset(source)}}
        self.criterion={"criterion_id":"script_subtitle","kind":"on_screen_text","required":True,
                        "expected_text":"离线字幕。","expectation":"离线测试：实际上屏文字须准确。"}
        previous={"video":self.video,"rows":[{"criterion_id":"script_subtitle","status":"UNKNOWN","execution_status":"evidence_invalid"}]}
        prior=self.root/"offline-invalid-whole-video.json";_write(prior,previous)
        self.previous={**previous,"receipt_path":str(prior),"receipt_sha256":digest_json(previous)}
        self.original=prior.read_bytes()
        self.client=Mock();self.client.transcribe.return_value={"data":{"observed_text":"离线字幕。","readable":True},
            "model":"explicit-offline-frame-ocr","request_id":"offline-frame-request","input_modality":"images"}

    def ffmpeg(self,arguments):
        Path(arguments[-1]).write_bytes(b"\xff\xd8\xff"+str(arguments[arguments.index("-ss")+1]).encode()+b" explicit offline JPEG fixture")

    def audit(self,criterion=None,previous=None):
        with patch("pianchang.content_review._ffmpeg",side_effect=self.ffmpeg):
            return audit_subtitle_frames(TextConfig(model="offline"),self.clip,self.video,criterion or self.criterion,
                {"shot_id":"S005"},self.root/"audit",previous or self.previous,client=self.client)

    def test_valid_transcription_is_cached_without_replaying_whole_video_or_mutating_its_error(self):
        first=self.audit();second=self.audit()
        self.assertEqual(first["rows"][0]["status"],"PASS")
        self.assertEqual(first["input_modality"],"images")
        self.assertEqual(first["frames"][0]["time_s"],4.9)
        self.assertEqual(len(first["frames"]),1)
        self.assertEqual(first["receipt_path"],second["receipt_path"])
        self.client.transcribe.assert_called_once()
        self.assertEqual(Path(self.previous["receipt_path"]).read_bytes(),self.original)

    def test_actual_ocr_payload_does_not_supply_expected_subtitle_or_a_reasoning_task(self):
        with patch("pianchang.content_review._ffmpeg",side_effect=self.ffmpeg):frames=prepare_subtitle_frames(self.clip,self.video,self.root/"frames")["frames"][-1:]
        client=SubtitleFrameObserver(TextConfig(api_key="sk-explicit-offline-fixture",model="offline"));captured=[]
        body={"id":"offline-transport","model":"offline","choices":[{"finish_reason":"stop","message":{"content":json.dumps({"observed_text":"离线字幕。","readable":True})}}]}
        with patch.object(client,"_post",side_effect=lambda req:(captured.append(json.loads(req.data)) or body,"offline-header")):
            result=client.transcribe(frames)
        payload=captured[0]
        self.assertNotIn(self.criterion["expected_text"],json.dumps(payload,ensure_ascii=False))
        self.assertTrue(any(part["type"]=="image_url" for part in payload["messages"][1]["content"]))
        self.assertFalse(any(part["type"]=="video_url" for part in payload["messages"][1]["content"]))
        self.assertLessEqual(payload["max_tokens"],320)
        self.assertEqual(result["data"]["observed_text"],"离线字幕。")

    def test_valid_failure_is_never_replaced_by_a_frame_pass(self):
        previous=copy.deepcopy(self.previous);previous["rows"][0].update(status="FAIL",execution_status="executed")
        self.assertIs(self.audit(previous=previous),previous)
        self.client.transcribe.assert_not_called()

    def test_scoped_review_keeps_original_when_subtitle_fallback_has_never_run(self):
        from pianchang.review_dispatch import retry_review_scope
        with retry_review_scope({}):
            self.assertIs(self.audit(), self.previous)
        self.client.transcribe.assert_not_called()
        self.assertFalse(list(self.root.rglob("call.json")))
        completed = self.audit()
        with retry_review_scope({}):
            self.assertEqual(self.audit()["receipt_sha256"], completed["receipt_sha256"])
        self.client.transcribe.assert_called_once()

    def test_trace_persistence_failure_stops_before_any_ocr_call(self):
        with patch("pianchang.subtitle_audit.prepare_subtitle_frames",side_effect=TraceError("offline trace store failure")):
            with self.assertRaises(TraceError):self.audit()
        self.client.transcribe.assert_not_called()

    def test_unreadable_or_wrong_frame_text_does_not_claim_full_video_absence(self):
        self.client.transcribe.return_value["data"]={"observed_text":"","readable":False}
        row=self.audit()["rows"][0]
        self.assertEqual(row["status"],"UNKNOWN")
        self.assertIn("不能证明其他时段",row["reason"])
        self.assertIn("frames_only",row["sampling_scope"])

    def test_invalid_frame_response_is_preserved_and_not_repeated(self):
        self.client.transcribe.return_value["data"]={"readable":True}
        first=self.audit();self.audit()
        self.assertEqual(first["rows"][0]["status"],"UNKNOWN")
        self.assertEqual(first["rows"][0]["execution_status"],"execution_error")
        self.client.transcribe.assert_called_once()

    def test_changed_requirement_recompares_received_text_without_another_model_call(self):
        first=self.audit();modified={**self.criterion,"expected_text":"另一段离线文字。"}
        second=self.audit(criterion=modified)
        self.assertEqual(first["rows"][0]["status"],"PASS")
        self.assertNotEqual(second["rows"][0]["status"],"PASS")
        self.assertNotEqual(first["receipt_path"],second["receipt_path"])
        self.client.transcribe.assert_called_once()

    def test_partial_local_frame_extraction_is_retained_and_recovered_without_payment(self):
        directory=self.root/"partial";directory.mkdir();partial=directory/"F001.jpg";partial.write_bytes(b"offline unconfirmed partial")
        with patch("pianchang.content_review._ffmpeg",side_effect=self.ffmpeg) as ffmpeg:
            frames=prepare_subtitle_frames(self.clip,self.video,directory)
            same=prepare_subtitle_frames(self.clip,self.video,directory)
        self.assertEqual(partial.read_bytes(),b"offline unconfirmed partial")
        self.assertEqual(Path(frames["manifest"]["path"]).parent.name,"recovery-1")
        self.assertEqual(frames,same);self.assertEqual(ffmpeg.call_count,2)

    def test_frame_evidence_is_bound_to_actual_call_and_tampering_is_rejected(self):
        receipt=self.audit();project={"shots":[{"id":"S005","required_refs":[]}]}
        contract={"shot_id":"S005","requirements":[self.criterion]};inspection={"media_sha256":self.clip["media_sha256"]}
        evidence={"version":VERSION,"project_sha256":digest_json(project),"contract_sha256":digest_json(contract),
            "inspection_sha256":digest_json(inspection),"media_sha256":self.clip["media_sha256"],"selected_media":{"S005":_asset(self.clip["media_path"])},
            "rows":receipt["rows"],"receipts":[{"path":receipt["receipt_path"],"sha256":receipt["receipt_sha256"]}]}
        self.assertEqual(validate_content_evidence(project,contract,inspection,evidence)["script_subtitle"]["status"],"PASS")
        Path(receipt["frames"][0]["path"]).write_bytes(b"offline deliberately altered evidence")
        with self.assertRaises(ValueError):validate_content_evidence(project,contract,inspection,evidence)
