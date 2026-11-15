"""Offline wire-level checks: all actual HTTP transports in these tests are simulated."""
import base64
import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from pianchang.bailian_video import (BailianVideoClient, VideoConfig, VideoError, build_request,
                           config_for_references, load_config)
from tests.test_bailian_video import Response
from trace_system import Tracer, trace, use_context
from pianchang.video_agent import VideoRunner, verify_reference_receipt


def sha(data):
    return hashlib.sha256(data).hexdigest()


def offline_voice_upload(config, item, prefix="first"):
    """Run the real upload boundary with explicit offline HTTP responses in its own Trace."""
    from pianchang.reference_transport import upload_voice
    sample = b"offline timbre"
    policy = {"upload_host":"https://offline.oss-cn-beijing.aliyuncs.com",
              "upload_dir":"dashscope-instant/offline/"+prefix, "x_oss_object_acl":"private",
              "x_oss_forbid_overwrite":"true", "oss_access_key_id":"offline-id",
              "signature":"offline-signature", "policy":"offline-policy"}
    opener = Mock()
    credential = Response(json.dumps({"data":policy,"request_id":"offline-policy-request"}).encode())
    uploaded = Response(b"", {"x-oss-request-id":"offline-simulated-upload"})
    credential.status = uploaded.status = 200
    opener.open.side_effect = [credential, uploaded]
    with trace("offline.simulated_voice_upload",kind="test"), \
         patch("pianchang.reference_transport.request.build_opener",return_value=opener):
        receipt = upload_voice(config,{**item, "provider_url":"data:audio/wav;base64,"+base64.b64encode(sample).decode()})
    return {**item, "provider_url": receipt["provider_url"], "upload_receipt": receipt}


class VideoReferenceReceiptTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        tracer = Tracer(self.root / "trace")
        scope = use_context(None, tracer=tracer)
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        config = VideoConfig(api_key="offline-fixture", model="wan2.7-r2v", output_dir=self.root / "jobs")
        self.client = BailianVideoClient(config)
        self.client._api_opener = Mock()
        self.runner = VideoRunner(config, self.client)
        image = b"explicit offline adapter bytes"
        sample = b"offline timbre"
        self.picture = {"asset_id":"portrait-lin","revision_id":"r3","sha256":sha(b"image manifest"),
                        "media_sha256":sha(image),"subject_id":"char:lin","purpose":"portrait",
                        "provider_url":"data:image/jpeg;base64," + base64.b64encode(image).decode(),
                        "bytes":len(image),"width":240,"height":320,"mime_type":"image/jpeg"}
        self.voice = {"asset_id":"voice-lin","revision_id":"r1","sha256":sha(b"voice manifest"),
                      "media_sha256":sha(sample),"subject_id":"char:lin","purpose":"voice","duration_s":2,
                      "mime_type":"audio/wav","bytes":len(sample)}
        self.set_voice_locator("first")
        self.refs = [self.picture,self.voice]
        self.required = [{k:r[k] for k in ("asset_id","revision_id","sha256","media_sha256","subject_id","purpose")}
                         for r in self.refs]
        self.metadata = {"required_refs":self.required,"shot_id":"S003","reference_media_used":False}
        self.client._api_opener.open.return_value = Response(json.dumps({"request_id":"offline-request",
            "output":{"task_id":"offline-task","task_status":"PENDING"}}).encode())

    def set_voice_locator(self, prefix):
        self.voice.update(offline_voice_upload(self.client.config, self.voice, prefix))

    def payload(self):
        return build_request(self.client.config, "林岚打开房门。", resolved_refs=self.refs)

    def test_paired_wire_fields_and_accepted_receipt_survive_redaction_and_locator_renewal(self):
        payload = self.payload()
        job = self.runner.submit(payload,self.metadata,resolved_refs=self.refs)
        actual = self.client._api_opener.open.call_args.args[0]
        self.assertEqual(json.loads(actual.data), payload)
        self.assertEqual(actual.get_header("X-dashscope-ossresourceresolve"), "enable")
        self.assertIn("图1对应char:lin", payload["input"]["prompt"])
        media = json.loads(actual.data)["input"]["media"][0]
        self.assertEqual(media, {"type":"reference_image", "url":self.picture["provider_url"],
                                 "reference_voice":self.voice["provider_url"]})
        receipt = job["reference_receipt"]
        self.assertEqual(receipt["status"], "ACCEPTED")
        self.assertEqual(receipt["wire_body_sha256"], sha(actual.data))
        self.assertEqual([p["payload_path"] for p in receipt["submitted_refs"]],
                         ["input.media[0].url","input.media[0].reference_voice"])
        manifest = Path(job["manifest_path"])
        saved = json.loads(manifest.read_text(encoding="utf8"))
        self.assertEqual(verify_reference_receipt(saved)["status"], "PASS")
        self.assertNotIn(self.picture["provider_url"], manifest.read_text(encoding="utf8"))
        self.assertNotIn("offline-fixture", manifest.read_text(encoding="utf8"))
        self.set_voice_locator("refreshed")
        cached = self.runner.submit(self.payload(),self.metadata,resolved_refs=self.refs)
        self.assertEqual(cached["task_id"], job["task_id"])
        self.assertEqual(self.client._api_opener.open.call_count, 1)
        saved["request"]["input"]["media"][0]["reference_voice"] = self.voice["provider_url"]
        self.assertEqual(verify_reference_receipt(saved)["status"], "FAIL")

    def test_ambiguous_post_preserves_intent_and_blocks_resubmission_with_new_locator(self):
        self.client._api_opener.open.side_effect = TimeoutError("simulated offline lost reply")
        with self.assertRaises(VideoError):
            self.runner.submit(self.payload(), self.metadata, resolved_refs=self.refs)
        manifest, = (self.root / "jobs").glob("*/job.json")
        saved = json.loads(manifest.read_text(encoding="utf8"))
        self.assertEqual(saved["reference_receipt"]["status"], "SUBMISSION_UNKNOWN")
        self.assertEqual(saved["reference_receipt"]["submitted_refs"], [])
        self.assertEqual(len(saved["reference_receipt"]["prepared_refs"]), 2)
        self.set_voice_locator("refreshed")
        with self.assertRaises(VideoError):
            self.runner.submit(self.payload(), self.metadata, resolved_refs=self.refs)
        self.assertEqual(self.client._api_opener.open.call_count, 1)

    def test_wrong_reference_never_reaches_transport(self):
        payload = self.payload()
        for key in ("url","reference_voice"):
            bad = copy.deepcopy(payload)
            bad["input"]["media"][0][key] = "https://example.invalid/wrong"
            with self.subTest(key=key), self.assertRaises(VideoError):
                self.runner.submit(bad,self.metadata,resolved_refs=self.refs)
        bad = copy.deepcopy(payload)
        bad["input"]["media"][0].pop("reference_voice")
        with self.assertRaises(VideoError):
            self.runner.submit(bad,self.metadata,resolved_refs=self.refs)
        with self.assertRaises(VideoError):
            self.runner.submit(payload, {"required_refs":self.required[::-1]},resolved_refs=self.refs)
        self.picture["provider_url"] = "data:image/jpeg;base64,YQ=="
        with self.assertRaises(VideoError):
            self.payload()
        self.client._api_opener.open.assert_not_called()

    def test_upload_pointer_is_verified_before_post_and_after_persistence(self):
        payload = self.payload()
        orig = self.voice["upload_receipt"].copy()
        self.voice["upload_receipt"]["span_id"] = "0"*32
        with self.assertRaises(VideoError):
            self.runner.submit(payload, self.metadata, resolved_refs=self.refs)
        self.client._api_opener.open.assert_not_called()
        self.voice["upload_receipt"] = orig
        job = self.runner.submit(payload, self.metadata, resolved_refs=self.refs)
        saved = json.loads(Path(job["manifest_path"]).read_text(encoding="utf8"))
        self.assertEqual(verify_reference_receipt(saved)["status"],"PASS")
        forged = copy.deepcopy(saved)
        forged["reference_receipt"]["voice_uploads"][0]["bytes"] += 1
        self.assertEqual(verify_reference_receipt(forged)["status"],"FAIL")
        missing = copy.deepcopy(saved)
        missing["reference_receipt"]["voice_uploads"][0]["trace_id"] = "9"*32
        self.assertNotEqual(verify_reference_receipt(missing)["status"],"PASS")

    def test_provider_rejection_records_posted_refs_without_accepted_task(self):
        self.client._api_opener.open.side_effect = HTTPError(
            "https://example.invalid/offline", 400, "offline", {},
            io.BytesIO(b'{"code":"InvalidParameter","request_id":"offline-reject"}'))
        with self.assertRaises(VideoError):
            self.runner.submit(self.payload(), self.metadata, resolved_refs=self.refs)
        manifest, = (self.root / "jobs").glob("*/job.json")
        saved = json.loads(manifest.read_text(encoding="utf8"))
        receipt = saved["reference_receipt"]
        self.assertEqual(receipt["status"], "PROVIDER_REJECTED")
        self.assertEqual(receipt["provider_request_id"], "offline-reject")
        self.assertEqual(len(receipt["submitted_refs"]), 2)
        self.assertIsNone(saved["task_id"])
        self.assertEqual(verify_reference_receipt(saved)["status"], "PASS")

    def test_voice_requires_matching_portrait_and_t2v_cannot_consume_references(self):
        with self.assertRaises(VideoError):
            build_request(VideoConfig(), "test", resolved_refs=self.refs)
        self.voice["subject_id"] = "char:other"
        with self.assertRaises(VideoError):
            self.payload()
        self.voice["subject_id"] = self.picture["subject_id"]
        self.voice["upload_receipt"]["media_sha256"] = "0"*64
        with self.assertRaises(VideoError):
            self.payload()

    def test_optional_reference_model_only_applies_to_pinned_shots(self):
        config_file = self.root / "offline.env"
        config_file.write_text("BAILIAN_VIDEO_MODEL=wan2.6-t2v\nBAILIAN_REFERENCE_VIDEO_MODEL=wan2.7-r2v\n",
                               encoding="utf8")
        config = load_config(config_file)
        self.assertIs(config_for_references(config, []), config)
        locked = config_for_references(config, self.required)
        self.assertEqual(locked.model, "wan2.7-r2v")
        self.assertIn("media", build_request(locked, "参考镜头", resolved_refs=self.refs)["input"])
        without_mapping = VideoConfig()
        self.assertIs(without_mapping,config_for_references(without_mapping,self.required))
        with self.assertRaises(VideoError):
            build_request(without_mapping,"参考镜头",resolved_refs=self.refs)
        with self.assertRaises(VideoError):
            VideoConfig(reference_model="wan2.6-t2v")

