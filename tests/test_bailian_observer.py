"""Offline Qwen VL protocol tests. No real API calls or credential-file reads."""
import base64
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib import error, request

from pianchang.bailian_observer import BailianObserver, SYSTEM_PROMPT, load_observer_config
from pianchang.bailian_text import TextConfig, TextError
from pianchang.bailian_video import _NoRedirect


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=")
CRITERIA = [{"criterion_id": "PROP_VISIBLE", "description": "铜钥匙可见", "capability": "static_prop"}]
OBSERVATIONS = [{"criterion_id": "PROP_VISIBLE", "verdict": "UNKNOWN",
                 "reason": "当前抽样帧无法辨认钥匙细节。", "frame_ids": ["F000"]}]


class Response(io.BytesIO):
    def __init__(self, body, headers=None):
        super().__init__(body)
        self.headers = headers or {}


def completion(data=None, *, content=None, reason="stop"):
    if content is None:
        content = json.dumps(data if data is not None else {"observations": OBSERVATIONS}, ensure_ascii=False)
    return {"id": "vision-request-1", "model": "qwen3-vl-plus",
            "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": reason}],
            "usage": {"prompt_tokens": 650, "completion_tokens": 100, "total_tokens": 750}}


class ObserverConfigurationTests(unittest.TestCase):
    def test_visual_and_audiovisual_routes_resolve_independent_accounts_without_env_file(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"AUDIT_KEY": "fake-audio-key"}, clear=True):
            path = Path(directory) / ".env"
            config_dir = path.parent / "config"
            config_dir.mkdir()
            extra = {"provider": "aliyun-bailian", "account": "audio", "api_key_env": "AUDIT_KEY",
                     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"}
            models = {"images": {"connection": "bailian.chat", "adapter": "bailian", "model_id": "visual-observer"},
                      "cuts": {"connection": "audio", "adapter": "bailian", "model_id": "audiovisual-observer"}}
            config = {"version": 1, "connections": {"audio": extra}, "models": models,
                      "routes": {"vision.review": "images", "continuity.review": "cuts"}}
            (config_dir / "models.json").write_text(json.dumps(config), encoding="utf8")
            visual = load_observer_config(path, allow_missing=True)
            self.assertEqual((visual.model, visual.api_key), ("visual-observer", ""))
            audio = visual.continuity_config
            self.assertEqual((audio.model, audio.base_url, audio.api_key, audio.route_account),
                             ("audiovisual-observer", extra["base_url"], "fake-audio-key", "audio"))

    def test_default_model_budget_and_workspace_endpoint_derive_from_text_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("DASHSCOPE_API_KEY=sk-offline-key\n"
                            "DASHSCOPE_BASE_URL=https://example.cn-beijing.maas.aliyuncs.com/api/v1\n"
                            "BAILIAN_TEXT_MODEL=qwen-plus\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                config = load_observer_config(path)
            self.assertEqual(config.model, "qwen3-vl-plus")
            self.assertEqual(config.request_timeout, 180)
            self.assertEqual(config.max_tokens, 4096)
            self.assertEqual(config.base_url, "https://example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1")
            self.assertNotIn(config.api_key, repr(config))

    def test_process_vision_settings_override_file_without_changing_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("BAILIAN_VISION_MODEL=qwen-vl-plus\nBAILIAN_VISION_MAX_TOKENS=1024\n",
                            encoding="utf-8")
            env = {"BAILIAN_VISION_MODEL": "qwen3-vl-plus", "BAILIAN_VISION_MAX_TOKENS": "2048",
                   "BAILIAN_VISION_REQUEST_TIMEOUT": "45", "DASHSCOPE_API_KEY": "sk-process-test"}
            with patch.dict(os.environ, env, clear=True):
                before = dict(os.environ)
                config = load_observer_config(path)
                self.assertEqual(dict(os.environ), before)
            self.assertEqual(config.model, "qwen3-vl-plus")
            self.assertEqual(config.max_tokens, 2048)
            self.assertEqual(config.request_timeout, 45)
            self.assertEqual(config.api_key, "sk-process-test")

    def test_invalid_vision_budget_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("", encoding="utf-8")
            for values in ({"BAILIAN_VISION_MAX_TOKENS": "oops"},
                           {"BAILIAN_VISION_REQUEST_TIMEOUT": "nan"},
                           {"BAILIAN_VISION_MAX_TOKENS": "0"}):
                with self.subTest(values=values), patch.dict(os.environ, values, clear=True), \
                        self.assertRaises(TextError):
                    load_observer_config(path)


class ObserverProtocolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "frame.png"
        self.path.write_bytes(PNG)
        self.frames = [{"id": "F000", "time_s": 0.0, "path": self.path,
                        "sha256": hashlib.sha256(PNG).hexdigest()},
                       {"id": "F001", "time_s": 1.25, "path": self.path}]
        self.config = TextConfig(api_key="sk-observer-test-secret", model="qwen3-vl-plus", max_tokens=4096)
        self.client = BailianObserver(self.config)
        self.client._opener = Mock()
        self.respond(completion())

    def respond(self, body):
        raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.client._opener.open.return_value = Response(raw, {"x-request-id": "header-request-id"})

    def test_sends_each_frame_with_timestamp_and_one_json_non_thinking_post(self):
        result = self.client.complete_observations(self.frames, CRITERIA)
        self.client._opener.open.assert_called_once()
        args, kwargs = self.client._opener.open.call_args
        req = args[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.full_url, self.config.base_url + "/chat/completions")
        self.assertEqual(req.get_header("Authorization"), "Bearer " + self.config.api_key)
        self.assertEqual(kwargs["timeout"], 180)
        payload = json.loads(req.data)
        self.assertEqual(payload["model"], "qwen3-vl-plus")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertFalse(payload["enable_thinking"])
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["max_tokens"], 4096)
        parts = payload["messages"][1]["content"]
        self.assertEqual(json.loads(parts[1]["text"]), {"frame_id": "F000", "timestamp_s": 0.0})
        self.assertEqual(json.loads(parts[3]["text"]), {"frame_id": "F001", "timestamp_s": 1.25})
        images = [part["image_url"]["url"] for part in parts if part["type"] == "image_url"]
        self.assertEqual(len(images), 2)
        self.assertEqual(base64.b64decode(images[0].split(",", 1)[1]), PNG)
        self.assertFalse(any(part["type"] in ("video", "video_url", "audio") for part in parts))
        self.assertEqual(result["data"]["observations"], OBSERVATIONS)
        self.assertEqual(result["request_id"], "vision-request-1")
        self.assertEqual(result["usage"]["total_tokens"], 750)
        self.assertEqual(result["evidence_scope"], "sparse_static_frames_only")
        self.assertNotIn("data:image/", json.dumps(result))
        self.assertNotIn(self.config.api_key, json.dumps(result))
        self.assertNotIn("visual_pass", result)

    def test_prompt_limits_observation_capabilities_without_claiming_full_video(self):
        self.assertIn("不是完整视频", SYSTEM_PROMPT)
        self.assertIn("没有音频", SYSTEM_PROMPT)
        self.assertIn("完整动作时序", SYSTEM_PROMPT)
        self.assertIn("身份参考匹配", SYSTEM_PROMPT)
        self.assertIn("一律输出UNKNOWN", SYSTEM_PROMPT)

    def test_frame_key_aliases_are_supported(self):
        frames = [{"frame_id": "ALIAS1", "timestamp_s": 2.5, "path": str(self.path)}]
        self.client.complete_observations(frames, CRITERIA)
        payload = json.loads(self.client._opener.open.call_args.args[0].data)
        label = json.loads(payload["messages"][1]["content"][1]["text"])
        self.assertEqual(label, {"frame_id": "ALIAS1", "timestamp_s": 2.5})

    def test_changed_frame_fails_before_provider_request(self):
        self.path.write_bytes(PNG + b"modified")
        with self.assertRaisesRegex(TextError, "sha256"):
            self.client.complete_observations(self.frames, CRITERIA)
        self.client._opener.open.assert_not_called()

    def test_invalid_frame_count_timestamp_duplicate_and_path_never_send(self):
        cases = ([], self.frames * 13,
                 [dict(self.frames[0], time_s=float("nan"))],
                 [dict(self.frames[0], time_s=True)],
                 [dict(self.frames[0], time_s=-1)],
                 [self.frames[0], self.frames[0]],
                 [dict(self.frames[0], path=self.path.with_name("missing.png"))])
        for frames in cases:
            with self.subTest(count=len(frames)), self.assertRaises(TextError):
                self.client.complete_observations(frames, CRITERIA)
        self.client._opener.open.assert_not_called()

    def test_nonimage_file_is_not_uploaded(self):
        self.path.write_text("not image data", encoding="utf-8")
        with self.assertRaises(TextError):
            self.client.complete_observations(self.frames, CRITERIA)
        self.client._opener.open.assert_not_called()

    def test_per_image_and_overall_request_size_limits_are_enforced_offline(self):
        with patch("pianchang.bailian_observer.MAX_FRAME_BYTES", 10), self.assertRaises(TextError):
            self.client.complete_observations(self.frames, CRITERIA)
        with patch("pianchang.bailian_observer.MAX_REQUEST_BYTES", 256), self.assertRaises(TextError):
            self.client.complete_observations(self.frames, CRITERIA)
        self.client._opener.open.assert_not_called()

    def test_invalid_or_sensitive_criteria_never_send(self):
        for criteria in ([], CRITERIA * 2, [{"criterion_id": ""}],
                         [{"criterion_id": "C1", "value": float("nan")}],
                         [{"criterion_id": "C1", "description": self.config.api_key}]):
            with self.subTest(criteria_count=len(criteria)), self.assertRaises(TextError):
                self.client.complete_observations(self.frames, criteria)
        self.client._opener.open.assert_not_called()

    def test_network_failure_is_not_retried_and_does_not_expose_key(self):
        self.client._opener.open.side_effect = error.URLError("connection lost " + self.config.api_key)
        with self.assertRaises(TextError) as raised:
            self.client.complete_observations(self.frames, CRITERIA)
        self.client._opener.open.assert_called_once()
        self.assertNotIn(self.config.api_key, str(raised.exception))

    def test_http_error_redacts_credentials_and_echoed_image_data(self):
        echoed_uri = "data:image/png;base64," + base64.b64encode(PNG).decode("ascii")
        body = {"error": {"code": "BadRequest", "message": self.config.api_key + " " + echoed_uri}}
        self.client._opener.open.side_effect = error.HTTPError(
            self.config.base_url, 400, "bad request", {"x-request-id": "failed-vision"},
            io.BytesIO(json.dumps(body).encode("utf-8")))
        with self.assertRaises(TextError) as raised:
            self.client.complete_observations(self.frames, CRITERIA)
        self.assertNotIn(self.config.api_key, str(raised.exception))
        self.assertNotIn(echoed_uri, str(raised.exception))
        self.assertNotIn(base64.b64encode(PNG).decode("ascii"), str(raised.exception))
        self.assertEqual(raised.exception.http_status, 400)
        self.assertEqual(raised.exception.request_id, "failed-vision")
        self.client._opener.open.assert_called_once()

    def test_redirect_handler_does_not_forward_authorization(self):
        client = BailianObserver(self.config)
        handler = next(h for h in client._opener.handlers if isinstance(h, _NoRedirect))
        req = request.Request(self.config.base_url, headers={"Authorization": "Bearer " + self.config.api_key})
        self.assertIsNone(handler.redirect_request(req, None, 302, "redirect", {}, "https://other.invalid/"))

    def test_nonstop_and_refusal_results_are_not_accepted(self):
        for reason in ("length", "tool_calls", None):
            with self.subTest(reason=reason):
                self.respond(completion(reason=reason))
                with self.assertRaises(TextError) as raised:
                    self.client.complete_observations(self.frames, CRITERIA)
                self.assertEqual(raised.exception.usage["total_tokens"], 750)
        refused = completion()
        refused["choices"][0]["message"]["refusal"] = "refused"
        self.respond(refused)
        with self.assertRaises(TextError) as raised:
            self.client.complete_observations(self.frames, CRITERIA)
        self.assertEqual(raised.exception.code, "ModelRefusal")

    def test_header_request_id_is_preserved_on_incomplete_output(self):
        body = completion(reason="length")
        body.pop("id")
        self.respond(body)
        with self.assertRaises(TextError) as raised:
            self.client.complete_observations(self.frames, CRITERIA)
        self.assertEqual(raised.exception.request_id, "header-request-id")
        self.assertEqual(raised.exception.code, "OutputTruncated")

    def test_missing_key_never_reads_or_sends_frames(self):
        client = BailianObserver(TextConfig(model="qwen3-vl-plus"))
        client._opener = Mock()
        with self.assertRaises(TextError):
            client.complete_observations(self.frames, CRITERIA)
        client._opener.open.assert_not_called()

    def test_malformed_or_nonfinite_json_is_not_repaired(self):
        for content in ("```json\n{}\n```", "not-json", "{\"observations\":NaN}", "[]", "{}"):
            with self.subTest(content=content):
                self.respond(completion(content=content))
                with self.assertRaises(TextError):
                    self.client.complete_observations(self.frames, CRITERIA)

    def test_invalid_provider_envelope_and_oversized_response_are_rejected(self):
        for body in (b"not json", [], {}, {"choices": []}):
            with self.subTest(body_type=type(body).__name__):
                self.respond(body)
                with self.assertRaises(TextError):
                    self.client.complete_observations(self.frames, CRITERIA)
        self.respond(completion())
        with patch("pianchang.bailian_observer.MAX_RESPONSE_BYTES", 32), self.assertRaises(TextError):
            self.client.complete_observations(self.frames, CRITERIA)

    def test_invalid_observation_schema_is_rejected(self):
        for row in ({"criterion_id": "C1"},
                    {"criterion_id": "C1", "verdict": "YES", "reason": "visible", "frame_ids": ["F000"]},
                    {"criterion_id": "C1", "verdict": "PASS", "reason": "", "frame_ids": ["F000"]},
                    {"criterion_id": "C1", "verdict": "UNKNOWN", "reason": "unclear", "frame_ids": "F000"}):
            self.respond(completion({"observations": [row]}))
            with self.assertRaises(TextError) as raised:
                self.client.complete_observations(self.frames, CRITERIA)
            self.assertEqual(raised.exception.code, "ObservationSchema")

    def test_unknown_frame_references_are_left_for_callers_evidence_gate(self):
        row = {"criterion_id": "PROP_VISIBLE", "verdict": "PASS", "reason": "模型声称可见。",
               "frame_ids": ["F_MADE_UP"], "visual_pass": True}
        self.respond(completion({"observations": [row], "visual_pass": True}))
        result = self.client.complete_observations(self.frames, CRITERIA)
        self.assertEqual(result["data"]["observations"][0]["frame_ids"], ["F_MADE_UP"])
        self.assertNotIn("visual_pass", result["data"])
        self.assertNotIn("visual_pass", result["data"]["observations"][0])

    def test_model_cannot_return_credentials_or_base64(self):
        for reason in (self.config.api_key, "data:image/png;base64," + base64.b64encode(PNG).decode("ascii")):
            row = {"criterion_id": "PROP_VISIBLE", "verdict": "UNKNOWN", "reason": reason, "frame_ids": []}
            self.respond(completion({"observations": [row]}))
            with self.assertRaises(TextError) as raised:
                self.client.complete_observations(self.frames, CRITERIA)
            self.assertNotIn(reason, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
