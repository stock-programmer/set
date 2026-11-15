"""Offline integration of the common trace SDK with real client boundaries."""
import base64
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from trace_system import Tracer
from pianchang.bailian_text import BailianTextClient, TextConfig, TextError
from pianchang.bailian_video import BailianVideoClient, VideoConfig, VideoError
from pianchang.bailian_observer import BailianObserver
from pianchang import media_tools
from pianchang import episode_export


SECRET = "trace-offline-secret-please-never-persist"
MESSAGES = [{"role": "system", "content": "Return one JSON object."},
            {"role": "user", "content": "Create a short scene with exactly three beats."}]
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=")


class Response(io.BytesIO):
    def __init__(self, data, headers=None, status=200):
        super().__init__(data)
        self.headers = headers or {}
        self.status = status


def envelope(content='{"title":"Night shop","beats":["one","two"]}'):
    return {"id": "chatcmpl-independent-body-id", "request_id": "body-request-id",
            "model": "qwen-plus", "extra_provider_field": {"retained": True},
            "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60}}


class ClientTraceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.tracer = Tracer(self.root / "trace")

    def expand(self, value):
        value = self.tracer.resolve(value)
        if isinstance(value, dict):
            return {k: self.expand(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.expand(v) for v in value]
        return value

    def detail(self, trace_id):
        return self.expand(self.tracer.get_trace(trace_id))

    def event(self, trace_id, name):
        return next(e["data"] for e in self.detail(trace_id)["events"] if e["name"] == name)

    def disk_bytes(self):
        return b"\n".join(p.read_bytes() for p in (self.root / "trace").rglob("*") if p.is_file())

    def text_client(self, response):
        client = BailianTextClient(TextConfig(api_key=SECRET))
        client._opener = Mock()
        client._opener.open.return_value = response
        return client

    def test_text_wire_context_full_envelope_ids_and_usage_persist_separately(self):
        body = envelope()
        raw = json.dumps(body).encode()
        client = self.text_client(Response(raw, {"x-request-id": "header-request-id", "Set-Cookie": "session=private-cookie"}))
        with self.tracer.trace("client-test") as root:
            result = client.complete_json(MESSAGES)
        sent = self.event(root.trace_id, "http.request")
        got = self.event(root.trace_id, "http.response")
        self.assertEqual(sent["body"]["messages"], MESSAGES)
        self.assertEqual(sent["body"]["model"], "qwen-plus")
        self.assertEqual(sent["body"]["max_tokens"], 8192)
        self.assertFalse(sent["body"]["enable_thinking"])
        self.assertEqual(sent["wire_body_sha256"], hashlib.sha256(client._opener.open.call_args.args[0].data).hexdigest())
        self.assertEqual(got["body"], body)
        self.assertEqual(got["body_text"], raw.decode())
        self.assertEqual(got["header_request_id"], "header-request-id")
        self.assertEqual(got["body_id"], "chatcmpl-independent-body-id")
        self.assertEqual(got["body_request_id"], "body-request-id")
        self.assertEqual(got["http_status"], 200)
        self.assertEqual(result["usage"], body["usage"])
        self.assertNotIn(SECRET.encode(), self.disk_bytes())
        self.assertNotIn(b"private-cookie", self.disk_bytes())
        self.assertTrue(any(s["name"] == "bailian.text.complete_json" and s["status"] == "OK"
                            for s in self.detail(root.trace_id)["spans"]))

    def test_invalid_json_response_is_preserved_before_parser_fails(self):
        raw = b"provider gateway returned non-JSON"
        client = self.text_client(Response(raw, {"x-request-id": "invalid-json-id"}))
        with self.tracer.trace("invalid-response") as root:
            with self.assertRaises(TextError):
                client.complete_json(MESSAGES)
        got = self.event(root.trace_id, "http.response")
        self.assertEqual(got["body_text"], raw.decode())
        self.assertIsNone(got["body"])
        self.assertTrue(any(s["name"] == "bailian.text.complete_json" and s["status"] == "ERROR"
                            for s in self.detail(root.trace_id)["spans"]))
        client._opener.open.assert_called_once()

    def test_http_error_keeps_envelope_and_redacts_secret_in_raw_body(self):
        client = self.text_client(None)
        raw = json.dumps({"request_id": "provider-error-id", "error": {"code": "Denied", "message": SECRET}}).encode()
        client._opener.open.side_effect = HTTPError("https://example.invalid", 403, "denied",
                      {"x-request-id": "error-header-id"}, io.BytesIO(raw))
        with self.tracer.trace("http-error") as root:
            with self.assertRaises(TextError):
                client.complete_json(MESSAGES)
        got = self.event(root.trace_id, "http.response")
        self.assertEqual(got["http_status"], 403)
        self.assertEqual(got["body_request_id"], "provider-error-id")
        self.assertEqual(got["header_request_id"], "error-header-id")
        self.assertNotIn(SECRET, got["body_text"])
        self.assertNotIn(SECRET.encode(), self.disk_bytes())

    def test_recording_failure_stops_before_paid_transport(self):
        client = self.text_client(Response(json.dumps(envelope()).encode()))
        with self.tracer.trace("trace-storage-failure"):
            with patch("pianchang.bailian_video.event", side_effect=RuntimeError("trace disk unavailable")):
                with self.assertRaisesRegex(RuntimeError, "trace disk unavailable"):
                    client.complete_json(MESSAGES)
        client._opener.open.assert_not_called()

    def test_video_request_and_binary_download_are_both_traced(self):
        client = BailianVideoClient(VideoConfig(api_key=SECRET))
        client._api_opener = Mock()
        body = {"request_id": "video-request", "output": {"task_id": "task-1", "task_status": "PENDING"}}
        client._api_opener.open.return_value = Response(json.dumps(body).encode(), {"x-request-id": "video-header"})
        mp4 = b"\x00\x00\x00\x18ftypisom" + b"media" * 10
        client._media_opener = Mock()
        client._media_opener.open.return_value = Response(mp4, {"Content-Type": "video/mp4", "Content-Length": str(len(mp4))})
        with self.tracer.trace("video") as root:
            client.submit({"model": "wan2.6-t2v", "input": {"prompt": "Short scene"}, "parameters": {"duration": 5}})
            client.download("https://example.invalid/video.mp4?Signature=private-signed-value", self.root / "video.mp4")
        artifact = self.event(root.trace_id, "artifact.downloaded")
        self.assertEqual(artifact["bytes"], len(mp4))
        self.assertEqual(artifact["sha256"], hashlib.sha256(mp4).hexdigest())
        self.assertEqual(self.event(root.trace_id, "http.response")["body_request_id"], "video-request")
        self.assertNotIn(b"private-signed-value", self.disk_bytes())
        self.assertNotIn(SECRET.encode(), self.disk_bytes())

    def test_vision_records_real_frame_fingerprint_without_embedding_base64(self):
        image = self.root / "frame.png"
        image.write_bytes(PNG)
        client = BailianObserver(TextConfig(api_key=SECRET, model="qwen3-vl-plus"))
        client._opener = Mock()
        content = {"observations": [{"criterion_id": "prop", "verdict": "UNKNOWN", "reason": "Too small", "frame_ids": ["F001"]}]}
        response = envelope(json.dumps(content))
        response["model"] = "qwen3-vl-plus"
        client._opener.open.return_value = Response(json.dumps(response).encode())
        with self.tracer.trace("vision") as root:
            client.complete_observations([{"id": "F001", "time_s": 0, "path": str(image)}],
                                         [{"criterion_id": "prop", "expectation": "key visible"}])
        frame = self.event(root.trace_id, "media.reference_read")
        self.assertEqual(frame["sha256"], hashlib.sha256(PNG).hexdigest())
        self.assertEqual(frame["bytes"], len(PNG))
        self.assertEqual(frame["path"], str(image.resolve()))
        sent = self.event(root.trace_id, "http.request")
        self.assertEqual(sent["body"]["model"], "qwen3-vl-plus")
        self.assertNotIn(base64.b64encode(PNG), self.disk_bytes())

    def test_media_process_records_argv_exit_stdout_stderr_and_error_status(self):
        completed = subprocess.CompletedProcess(["ffprobe"], 2, "provider stdout", "bad media")
        with self.tracer.trace("process") as root:
            with patch("pianchang.media_tools.subprocess.run", return_value=completed):
                result = media_tools._run(["ffprobe", "fixture.mp4"], timeout=3)
        self.assertEqual(result.returncode, 2)
        process = next(s for s in self.detail(root.trace_id)["spans"] if s["name"] == "media.process")
        self.assertEqual(process["status"], "ERROR")
        self.assertEqual(process["inputs"]["argv"], ["ffprobe", "fixture.mp4"])
        self.assertEqual(process["output"], {"returncode": 2, "stdout": "provider stdout", "stderr": "bad media"})

    def test_export_timeout_keeps_partial_process_output(self):
        with self.tracer.trace("export-timeout") as root:
            with patch("pianchang.episode_export.subprocess.run", side_effect=subprocess.TimeoutExpired(["ffmpeg"], 3, output=b"partial", stderr=b"timeout detail")):
                with self.assertRaises(episode_export.ExportError):
                    episode_export._run(["ffmpeg", "-version"], timeout=3)
        data = self.event(root.trace_id, "process.timeout")
        self.assertEqual(data["stdout"], "partial")
        self.assertEqual(data["stderr"], "timeout detail")
        self.assertTrue(any(s["name"] == "export.process" and s["status"] == "ERROR"
                            for s in self.detail(root.trace_id)["spans"]))

    def test_download_http_error_keeps_diagnostic_body_without_publishing_video(self):
        client = BailianVideoClient(VideoConfig(api_key=SECRET))
        client._media_opener = Mock()
        client._media_opener.open.side_effect = HTTPError("https://example.invalid/video.mp4", 403, "expired",
                        {"x-request-id": "download-failure"}, io.BytesIO(b"signed link expired"))
        target = self.root / "missing.mp4"
        with self.tracer.trace("download-error") as root:
            with self.assertRaises(VideoError):
                client.download("https://example.invalid/video.mp4", target)
        self.assertFalse(target.exists())
        got = self.event(root.trace_id, "http.response")
        self.assertEqual(got["http_status"], 403)
        self.assertEqual(got["body_text"], "signed link expired")


if __name__ == "__main__":
    unittest.main()
