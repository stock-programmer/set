"""Offline transport/configuration tests. No paid provider requests are made."""
import io
import json
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import Mock, patch
from urllib import error, request

from pianchang.bailian_video import (BailianVideoClient, VideoConfig, VideoError,
                           _NoRedirect, build_request, load_config)


class Response(io.BytesIO):
    def __init__(self, body, headers=None):
        super().__init__(body)
        self.headers = headers or {}


class ConfigurationTests(unittest.TestCase):
    def test_dotenv_bom_quotes_comments_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                '\ufeff# 环境配置\nexport DASHSCOPE_API_KEY="file-secret" # 注释\n'
                'BAILIAN_VIDEO_DURATION=6\nBAILIAN_VIDEO_RATIO=16:9\n'
                'BAILIAN_VIDEO_OUTPUT_DIR="output media"\n', encoding="utf-8")
            with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "process-secret"}, clear=True):
                before = dict(os.environ)
                config = load_config(path)
                self.assertEqual(config.api_key, "process-secret")
                self.assertEqual(config.duration, 6)
                self.assertEqual(config.ratio, "16:9")
                self.assertEqual(config.output_dir, Path(directory) / "output media")
                self.assertEqual(dict(os.environ), before)
                self.assertNotIn("process-secret", repr(config))

    def test_missing_explicit_config_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(VideoError, "无法读取"):
                load_config(Path(directory) / "missing.env")

    def test_invalid_configuration_fails_without_echoing_secret_line(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text('DASHSCOPE_API_KEY="secret-with-unclosed-quote\n', encoding="utf-8")
            with self.assertRaises(VideoError) as raised:
                load_config(path)
            self.assertNotIn("secret-with", str(raised.exception))

    def test_invalid_timeout_and_endpoint_rejected_before_any_request(self):
        for key, value in (("wait_timeout", 0), ("poll_interval", float("nan")),
                           ("request_timeout", float("inf")),
                           ("base_url", "http://dashscope.aliyuncs.com/api/v1"),
                           ("base_url", "https://user:password@example.com/api/v1"),
                           ("base_url", "https://example.com/api/v1?token=secret")):
            with self.subTest(key=key, value=value), self.assertRaises(VideoError):
                VideoConfig(**{key: value})

    def test_empty_key_allowed_for_planning_but_not_requests(self):
        client = BailianVideoClient(VideoConfig())
        client._api_opener = Mock()
        payload = build_request(client.config, "夜晚办公室，成年女性拿出铜钥匙。")
        with self.assertRaisesRegex(VideoError, "DASHSCOPE_API_KEY"):
            client.submit(payload)
        client._api_opener.open.assert_not_called()


class RequestTests(unittest.TestCase):
    def test_26_vertical_resolution_and_actual_shot_duration(self):
        result = build_request(VideoConfig(), "手部近景：交出钥匙。", duration=8, seed=42)
        self.assertEqual(result["parameters"]["size"], "720*1280")
        self.assertEqual(result["parameters"]["duration"], 8)
        self.assertEqual(result["parameters"]["seed"], 42)
        self.assertEqual(result["parameters"]["shot_type"], "single")
        self.assertNotIn("resolution", result["parameters"])
        self.assertNotIn("audio", result["parameters"])

    def test_27_uses_new_protocol_and_has_no_shot_type(self):
        for model in ("wan2.7-t2v", "wan2.7-t2v-2026-06-12"):
            with self.subTest(model=model):
                config = VideoConfig(model=model, ratio="4:3", resolution="1080P")
                result = build_request(config, "生成单镜头视频。" + "雨" * 1600)
                self.assertEqual(result["parameters"]["resolution"], "1080P")
                self.assertEqual(result["parameters"]["ratio"], "4:3")
                self.assertNotIn("size", result["parameters"])
                self.assertNotIn("shot_type", result["parameters"])

    def test_inactive_shot_type_not_sent_and_audio_is_input_url(self):
        config = VideoConfig(prompt_extend=False)
        result = build_request(config, "海岸。", audio_url="https://example.com/voice.mp3",
                               negative_prompt="模糊、畸变")
        self.assertEqual(result["input"]["audio_url"], "https://example.com/voice.mp3")
        self.assertEqual(result["input"]["negative_prompt"], "模糊、畸变")
        self.assertNotIn("audio", result["parameters"])
        self.assertNotIn("shot_type", result["parameters"])

    def test_parameter_errors_are_never_silently_coerced(self):
        config = VideoConfig()
        cases = [dict(duration=0), dict(duration=16), dict(duration=6.0), dict(duration=True),
                 dict(duration=float("nan")), dict(seed=-1), dict(seed=2147483648),
                 dict(seed=True), dict(image_url="https://example.com/ref.jpg"),
                 dict(audio_url="C:/private/voice.mp3"), dict(negative_prompt="x" * 501)]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(VideoError):
                build_request(config, "镜头", **values)
        for prompt in ("", "   ", "x" * 1501):
            with self.subTest(prompt_length=len(prompt)), self.assertRaises(VideoError):
                build_request(config, prompt)
        with self.assertRaises(VideoError):
            VideoConfig(model="wan2.6-i2v")


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.config = VideoConfig(api_key="sk-unit-test-secret")
        self.client = BailianVideoClient(self.config)
        self.client._api_opener = Mock()
        self.client._media_opener = Mock()

    def test_submit_serializes_unicode_with_required_headers_once(self):
        body = {"output": {"task_id": "task-123", "task_status": "PENDING"}, "request_id": "req-1"}
        self.client._api_opener.open.return_value = Response(json.dumps(body).encode())
        payload = build_request(self.config, "成年女性在办公室拿出钥匙。")
        self.assertEqual(self.client.submit(payload), body)
        self.client._api_opener.open.assert_called_once()
        req = self.client._api_opener.open.call_args.args[0]
        self.assertEqual(req.method, "POST")
        self.assertEqual(req.full_url, self.config.base_url + "/services/aigc/video-generation/video-synthesis")
        self.assertEqual(req.get_header("Authorization"), "Bearer " + self.config.api_key)
        self.assertEqual(req.get_header("X-dashscope-async"), "enable")
        self.assertEqual(json.loads(req.data.decode("utf-8")), payload)

    def test_get_keeps_provider_failed_task_payload_for_runner(self):
        body = {"output": {"task_id": "task-123", "task_status": "FAILED", "code": "InvalidParameter"}}
        self.client._api_opener.open.return_value = Response(json.dumps(body).encode())
        self.assertEqual(self.client.get_task("task-123"), body)
        req = self.client._api_opener.open.call_args.args[0]
        self.assertEqual(req.method, "GET")
        self.assertEqual(req.full_url, self.config.base_url + "/tasks/task-123")
        self.assertIsNone(req.get_header("X-dashscope-async"))
        self.assertIsNone(req.data)

    def test_submit_timeout_is_not_retried_and_key_is_redacted(self):
        self.client._api_opener.open.side_effect = error.URLError("timeout " + self.config.api_key)
        with self.assertRaises(VideoError) as raised:
            self.client.submit(build_request(self.config, "镜头"))
        self.assertIn("请勿自动重提", str(raised.exception))
        self.assertNotIn(self.config.api_key, str(raised.exception))
        self.client._api_opener.open.assert_called_once()

    def test_http_error_retains_code_request_id_and_redacts_key(self):
        body = {"code": "InvalidApiKey", "request_id": "trace-42", "message": self.config.api_key}
        self.client._api_opener.open.side_effect = error.HTTPError(
            self.config.base_url, 401, "Unauthorized", {}, io.BytesIO(json.dumps(body).encode()))
        with self.assertRaises(VideoError) as raised:
            self.client.get_task("task-123")
        self.assertEqual(raised.exception.code, "InvalidApiKey")
        self.assertEqual(raised.exception.request_id, "trace-42")
        self.assertEqual(raised.exception.http_status, 401)
        self.assertNotIn(self.config.api_key, str(raised.exception))

    def test_200_business_error_and_invalid_json_fail_cleanly(self):
        for raw in (b'{"code":"Throttling","request_id":"req-1"}', b"<html>error</html>", b"[]"):
            with self.subTest(raw=raw):
                self.client._api_opener.open.return_value = Response(raw)
                with self.assertRaises(VideoError):
                    self.client.get_task("task-123")

    def test_task_id_cannot_change_request_path(self):
        for task_id in ("", "../foo", "task?x=1", "task/other", None):
            with self.subTest(task_id=task_id), self.assertRaises(VideoError):
                self.client.get_task(task_id)
        self.client._api_opener.open.assert_not_called()

    def test_download_without_api_key_and_atomic_mp4(self):
        video = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32
        self.client._media_opener.open.return_value = Response(
            video, {"Content-Type": "video/mp4", "Content-Length": str(len(video))})
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "shot.mp4"
            self.assertEqual(self.client.download("https://example.com/video.mp4", destination), destination)
            self.assertEqual(destination.read_bytes(), video)
            self.assertEqual(list(Path(directory).glob("*.part")), [])
        req = self.client._media_opener.open.call_args.args[0]
        self.assertIsNone(req.get_header("Authorization"))
        self.client._api_opener.open.assert_not_called()

    def test_download_error_does_not_overwrite_previous_video_or_leave_partial(self):
        samples = [(b"", {}), (b"<html>expired</html>", {"Content-Type": "text/html"}),
                   (b"\x00\x00\x00\x18ftypisom", {"Content-Length": "100"})]
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "shot.mp4"
            destination.write_bytes(b"previous-video")
            for body, headers in samples:
                with self.subTest(body=body):
                    self.client._media_opener.open.return_value = Response(body, headers)
                    with self.assertRaises(VideoError):
                        self.client.download("https://example.com/video.mp4", destination)
                    self.assertEqual(destination.read_bytes(), b"previous-video")
                    self.assertEqual(list(Path(directory).glob("*.part")), [])


class RedirectTests(unittest.TestCase):
    def test_authenticated_api_redirect_is_not_followed(self):
        seen_paths = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen_paths.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/destination")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        worker.start()
        try:
            req = request.Request(f"http://127.0.0.1:{server.server_port}/original",
                                  headers={"Authorization": "Bearer not-a-real-key"})
            with self.assertRaises(error.HTTPError) as raised:
                request.build_opener(_NoRedirect()).open(req, timeout=2)
            self.assertEqual(raised.exception.code, 302)
            raised.exception.close()
            self.assertEqual(seen_paths, ["/original"])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
