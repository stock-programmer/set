"""Exercise the pinned SDK on a local HTTP transport, including independent Trace evidence."""
import base64
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from unittest.mock import patch

from pianchang.bailian_video import build_request, config_for_references, config_for_shooting_plan, load_config, reference_pin, VideoError
from pianchang.model_routes import SDK_VERSION
from pianchang.reference_usage import verify_reference_exchange
from pianchang.video_agent import VideoRunner, verify_reference_receipt
from trace_system import Tracer, use_context


def has_sdk():
    try:
        return version("litellm") == SDK_VERSION
    except PackageNotFoundError:
        return False


class VideoRouteTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.env = self.root / ".env"
        self.env.write_text("ARK_VIDEO_TEST_KEY=offline-ark-key\nDASHSCOPE_API_KEY=offline-bailian-key\n", encoding="utf8")
        self.cfg = {"version":1, "connections":{"ark":{"provider":"volcengine","account":"video-team",
                     "base_url":"https://ark.example.invalid/api/v3","api_key_env":"ARK_VIDEO_TEST_KEY"}},
                    "models":{"test-video":{"connection":"ark","adapter":"litellm","sdk_provider":"volcengine",
                                           "model_id":"doubao-seedance-2-0-260128"}},
                    "routes":{"video.reference_to_video":"test-video"}}
        (self.root / "config").mkdir()
        self.write_config()
        self.settings = load_config(self.env)
        scope = use_context(None, tracer=Tracer(self.root / "traces"))
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)

    def write_config(self):
        (self.root / "config/models.json").write_text(json.dumps(self.cfg), encoding="utf8")

    def test_switches_by_purpose_and_freezes_account_model_and_endpoint(self):
        self.assertFalse(config_for_references(self.settings, []).sdk_video)
        old = config_for_references(self.settings, [True])
        self.assertTrue(old.sdk_video)
        self.assertFalse(config_for_shooting_plan(old,[],{"plan_id":"legacy"}).sdk_video)
        plan = {"video_routes":{"video.reference_to_video":old.route.snapshot()}}
        self.cfg["models"]["test-video"]["model_id"] = "another-endpoint"
        self.write_config()
        self.assertEqual(config_for_references(self.settings, [True]).model, "another-endpoint")
        self.assertEqual(config_for_shooting_plan(self.settings, [True], plan).model, old.model)
        self.cfg["connections"]["ark"]["account"] = "different-team"
        self.write_config()
        with self.assertRaisesRegex(ValueError, "冻结"):
            config_for_shooting_plan(self.settings, [True], plan)

    def test_seedance_reference_limits_are_checked_before_dispatch(self):
        from pianchang.bailian_video import validate_reference_capabilities
        config = config_for_references(self.settings, [True])
        pins = [{"asset_id":f"prop-{i}", "revision_id":"r1","sha256":"b"*64,"media_sha256":"a"*64,
                 "subject_id":f"obj-{i}","purpose":"prop"} for i in range(10)]
        validate_reference_capabilities(config,pins[:9])
        with self.assertRaisesRegex(VideoError, "1–9"):
            validate_reference_capabilities(config,pins)
        with self.assertRaisesRegex(VideoError,"4 到 15"):
            build_request(config,"scene",duration=2)

    def test_old_bailian_candidates_and_unknown_requests_keep_the_original_slot(self):
        from tests.test_video_agent import FakeClient
        historical_config = replace(self.settings, env_file=None)
        old = VideoRunner(historical_config, FakeClient())
        payload = build_request(self.settings, "previous shot")
        submitted = old.submit(payload)
        fresh_client = FakeClient()
        migrated = VideoRunner(config_for_references(self.settings, []), fresh_client)
        self.assertEqual(migrated.submit(payload)["manifest_path"], submitted["manifest_path"])
        pending_payload = build_request(self.settings, "uncertain shot")
        with self.assertRaises(VideoError):
            VideoRunner(historical_config, FakeClient(submit_error=VideoError("lost reply"))).submit(pending_payload)
        with self.assertRaisesRegex(VideoError, "迁移前"):
            migrated.submit(pending_payload)
        self.cfg["routes"]["video.text_to_video"] = "test-video"
        self.write_config()
        switched = VideoRunner(config_for_references(load_config(self.env), []), fresh_client)
        with self.assertRaisesRegex(VideoError, "不能自动再次提交"):
            switched.submit(build_request(switched.config, "uncertain shot"))
        self.assertEqual(fresh_client.submissions, [])

    def test_workbench_rejects_a_displayed_plan_after_switching_the_video_route(self):
        from pianchang.studio_jobs import _approved_plan, studio_video_plan
        from pianchang.story_spec import digest_json
        fixture = Path(__file__).resolve().parents[1] / "fixtures/story_project.json"
        project = json.loads(fixture.read_text(encoding="utf-8-sig"))
        view = {"project":project,"project_id":"story","production_ref":None,"script_ref":None,
                "spec":{"revision_id":None,"sha256":digest_json(project)},
                "store_path":str(self.root / "outputs/collaborations/assets.sqlite3")}
        with patch("pianchang.studio_retakes.extra_retake_proposal", return_value=None), \
             patch("pianchang.studio_recovery.read_incomplete_production", return_value=None):
            old = studio_video_plan(view, None, incomplete_snapshot=None)
            self.cfg["routes"]["video.text_to_video"] = "test-video"
            self.write_config()
            new = studio_video_plan(view, None, incomplete_snapshot=None)
            self.assertNotEqual(old["plan_id"], new["plan_id"])
            with self.assertRaisesRegex(ValueError, "计划已更新"):
                _approved_plan(view, None, {"plan_id":old["plan_id"]})

    def test_proved_local_failure_can_retry_without_destroying_the_first_receipt(self):
        from pianchang.video_agent import verified_local_failure
        self.cfg["routes"]["video.text_to_video"] = "test-video"
        self.write_config()
        runner = VideoRunner(config_for_references(self.settings, []))
        payload = build_request(runner.config, "Actor walks.")
        missing = RuntimeError("SDK not installed")
        missing.network_dispatched = False
        with patch("pianchang.sdk_transport.call_sdk",side_effect=missing):
            with self.assertRaises(VideoError):
                runner.submit(payload)
        original = next(runner.config.output_dir.glob("*/job.json"))
        record = json.loads(original.read_text(encoding="utf8"))
        self.assertTrue(verified_local_failure(record,original))
        result = {"id":"encoded","_provider_task_id":"corrected-task","size":"720x1280",
                  "_wire_receipt":{"wire_body_sha256":"e"*64}}
        with patch("pianchang.sdk_transport.call_sdk",return_value=result) as adapter:
            done = runner.submit(payload)
            cached = runner.submit(payload)
        self.assertEqual(adapter.call_count, 1)
        self.assertEqual(done["manifest_path"],cached["manifest_path"])
        self.assertNotEqual(Path(done["manifest_path"]),original)
        self.assertEqual(json.loads(original.read_text(encoding="utf8")),record)

    @unittest.skipUnless(has_sdk(), "requires pinned optional LiteLLM SDK")
    def test_actual_ark_reference_submit_restore_and_download(self):
        import httpx
        refs = []
        for purpose, mime in (("portrait","image/jpeg"), ("voice","audio/wav")):
            data = ("offline-" + purpose).encode()
            refs.append({"asset_id":purpose,"revision_id":"r1","sha256":"a"*64,
                         "media_sha256":hashlib.sha256(data).hexdigest(),"subject_id":"actor","purpose":purpose,
                         "provider_url":"data:"+mime+";base64,"+base64.b64encode(data).decode(),
                         "mime_type":mime,"bytes":len(data),"duration_s":3,"width":320,"height":320})
        config = config_for_references(self.settings, refs)
        payload = build_request(config, "Actor opens the door.", resolved_refs=refs)
        reqs = []
        result_url = "https://cdn.example.invalid/clip?signature=private-test-signature"
        media_bytes = b"\x00\x00\x00\x14ftypisomoffline-test-content"
        def provider(request):
            reqs.append(request)
            if request.method == "POST":
                return httpx.Response(200, json={"id":"task-seedance-test"},headers={"x-request-id":"req-ark"})
            if request.url.host == "cdn.example.invalid":
                if sum(r.url.host == "cdn.example.invalid" for r in reqs) == 1:
                    return httpx.Response(200,content=b"<html>temporary error</html>")
                return httpx.Response(200, content=media_bytes)
            return httpx.Response(200,json={"id":"task-seedance-test","status":"succeeded",
                                  "model":config.model,"duration":5,"content":{"video_url":result_url}})

        with patch("pianchang.sdk_transport._upstream_transport", side_effect=lambda:httpx.MockTransport(provider)):
            runner = VideoRunner(config)
            job = runner.submit(payload, {"required_refs":[reference_pin(r) for r in refs]},resolved_refs=refs)
            # Change today's default, then reopen the manifest exactly as a fresh CLI process does.
            self.cfg["routes"].clear()
            self.write_config()
            with self.assertRaisesRegex(VideoError, "MP4"):
                VideoRunner(load_config(self.env)).resume(job["manifest_path"])
            failed = json.loads(Path(job["manifest_path"]).read_text(encoding="utf8"))
            self.assertNotIn("download_sha256",failed)
            self.assertFalse(Path(job["manifest_path"]).with_name("video.mp4").exists())
            done = VideoRunner(load_config(self.env)).resume(job["manifest_path"])
        self.assertEqual(done["task_id"], "task-seedance-test")
        self.assertEqual([r.method for r in reqs], ["POST"]+["GET"]*6)
        self.assertTrue(all(str(r.url).endswith("/task-seedance-test") for r in reqs[1:3]+reqs[4:6]))
        self.assertEqual(str(reqs[-1].url), result_url)
        self.assertNotIn("authorization", reqs[-1].headers)
        request = json.loads(reqs[0].content)
        self.assertEqual([v.get("role") for v in request["content"]], [None, "reference_image","reference_audio"])
        self.assertEqual(request["content"][2]["audio_url"]["url"], refs[1]["provider_url"])
        self.assertEqual(Path(done["local_video_path"]).read_bytes(), media_bytes)
        receipt = done["reference_receipt"]
        self.assertEqual(receipt["submitted_refs"][0]["payload_path"],"content[1].image_url.url")
        self.assertEqual(verify_reference_receipt(done)["status"],"PASS")
        self.assertEqual(verify_reference_exchange(done,receipt,receipt["trace_id"])["status"],"MATCHED_RESPONSE")
        serialized = Path(job["manifest_path"]).read_text(encoding="utf8")
        for secret in (result_url, config.api_key, refs[0]["provider_url"]):
            self.assertNotIn(secret, serialized)

    @unittest.skipUnless(has_sdk(), "requires pinned optional LiteLLM SDK")
    def test_unknown_paid_post_cannot_be_retried(self):
        import httpx
        self.cfg["routes"]["video.text_to_video"] = "test-video"
        self.write_config()
        runner = VideoRunner(config_for_references(self.settings, []))
        payload = build_request(runner.config, "An empty room.")
        calls = []
        def offline_timeout(request):
            calls.append(request)
            raise httpx.ReadTimeout("simulated lost response",request=request)
        with patch("pianchang.sdk_transport._upstream_transport", side_effect=lambda:httpx.MockTransport(offline_timeout)):
            with self.assertRaises(VideoError):
                runner.submit(payload)
            with self.assertRaisesRegex(VideoError, "不能自动再次提交"):
                runner.submit(payload)
        self.cfg["routes"].clear()
        self.write_config()
        from tests.test_video_agent import FakeClient
        client = FakeClient()
        replacement = VideoRunner(config_for_references(load_config(self.env), []), client)
        with self.assertRaisesRegex(VideoError, "不能自动再次提交"):
            replacement.submit(build_request(replacement.config, "An empty room."))
        self.assertEqual(client.submissions, [])
        self.assertEqual(len(calls), 1)

    @unittest.skipUnless(has_sdk(), "requires pinned optional LiteLLM SDK")
    def test_remote_rejection_is_bound_to_its_actual_http_response(self):
        import httpx
        self.cfg["routes"]["video.text_to_video"] = "test-video"
        self.write_config()
        runner = VideoRunner(config_for_references(self.settings, []))
        payload = build_request(runner.config, "An empty room.")
        def rejected(request):
            return httpx.Response(400,json={"error":{"code":"InvalidParameter","message":"bad model"}},
                                  headers={"x-request-id":"rejected-ark"})
        with patch("pianchang.sdk_transport._upstream_transport", side_effect=lambda:httpx.MockTransport(rejected)):
            with self.assertRaises(VideoError):
                runner.submit(payload)
        path = next(runner.config.output_dir.glob("*/job.json"))
        job = json.loads(path.read_text(encoding="utf8"))
        receipt = job["reference_receipt"]
        self.assertEqual(receipt["status"], "PROVIDER_REJECTED")
        self.assertEqual(verify_reference_exchange(job,receipt,receipt["trace_id"])["status"],"MATCHED_RESPONSE")

    @unittest.skipUnless(has_sdk(), "requires pinned optional LiteLLM SDK")
    def test_wrong_task_on_content_lookup_cannot_download_another_tasks_file(self):
        import httpx
        from pianchang.sdk_video import SDKVideoClient
        client = SDKVideoClient(config_for_references(self.settings, [True]))
        calls = []
        def wrong_task(request):
            calls.append(request)
            return httpx.Response(200,json={"id":"other-task","status":"succeeded",
                                  "content":{"video_url":"https://cdn.example.invalid/wrong.mp4"}})
        dest = self.root / "download.mp4"
        with patch("pianchang.sdk_transport._upstream_transport", side_effect=lambda:httpx.MockTransport(wrong_task)):
            with self.assertRaises(VideoError):
                client.download_task("original-task", dest)
        self.assertEqual(len(calls), 1)
        self.assertFalse(dest.exists())
