"""Offline tests for structured Bailian text generation; no real API calls."""
import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib import error

from pianchang.bailian_text import BailianTextClient, TextConfig, TextError, load_text_config
from pianchang.bailian_video import _NoRedirect


MESSAGES = [{"role": "system", "content": "将简报写成 JSON 对象，不要附加其他文本。"},
            {"role": "user", "content": "两名成年角色在办公室交接一把钥匙。"}]


class Response(io.BytesIO):
    def __init__(self, body, headers=None):
        super().__init__(body)
        self.headers = headers or {}


def completion(content='{"title":"最后一把钥匙","shots":[]}', *, reason="stop"):
    return {"id": "chatcmpl-test-42", "model": "qwen-plus",
            "choices": [{"message": {"role": "assistant", "content": content},
                         "finish_reason": reason, "index": 0}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125}}


class ConfigurationTests(unittest.TestCase):
    def test_dotenv_loads_independently_and_process_values_win(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text('\ufeffDASHSCOPE_API_KEY="sk-file-test"\n'
                            'BAILIAN_VIDEO_MODEL=unknown-video-model\n'
                            'BAILIAN_TEXT_MODEL=qwen-plus-2025-12-01\n'
                            'BAILIAN_TEXT_MAX_TOKENS=4096\n'
                            'BAILIAN_TEXT_REQUEST_TIMEOUT=45\n', encoding="utf-8")
            with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "sk-process-test"}, clear=True):
                before = dict(os.environ)
                config = load_text_config(path)
                self.assertEqual(config.api_key, "sk-process-test")
                self.assertEqual(config.model, "qwen-plus-2025-12-01")
                self.assertEqual(config.max_tokens, 4096)
                self.assertEqual(config.request_timeout, 45)
                self.assertNotIn("sk-process-test", repr(config))
                self.assertEqual(dict(os.environ), before)

    def test_derives_workspace_host_or_honors_explicit_text_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text('DASHSCOPE_BASE_URL=https://workspace-id.cn-beijing.maas.aliyuncs.com/api/v1/\n',
                            encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(load_text_config(path).base_url,
                                 "https://workspace-id.cn-beijing.maas.aliyuncs.com/compatible-mode/v1")
            with patch.dict(os.environ, {"BAILIAN_TEXT_BASE_URL":
                                         "https://dashscope.aliyuncs.com/compatible-mode/v1/"}, clear=True):
                self.assertEqual(load_text_config(path).base_url,
                                 "https://dashscope.aliyuncs.com/compatible-mode/v1")

    def test_missing_and_malformed_explicit_env_are_text_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            with self.assertRaises(TextError):
                load_text_config(path)
            path.write_text('DASHSCOPE_API_KEY="sk-unclosed-secret\n', encoding="utf-8")
            with self.assertRaises(TextError) as raised:
                load_text_config(path)
            self.assertNotIn("sk-unclosed-secret", str(raised.exception))

    def test_invalid_config_fails_without_request(self):
        for args in ({"max_tokens": True}, {"max_tokens": 0}, {"request_timeout": float("nan")},
                     {"request_timeout": 0}, {"model": ""}, {"api_key": "sk-test\ninjected"},
                     {"base_url": "http://example.com/compatible-mode/v1"},
                     {"base_url": "https://name:secret@example.com/compatible-mode/v1"},
                     {"base_url": "https://example.com/compatible-mode/v1?key=secret"}):
            with self.subTest(args=args), self.assertRaises(TextError):
                TextConfig(**args)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.config = TextConfig(api_key="sk-offline-test-secret")
        self.client = BailianTextClient(self.config)
        self.client._opener = Mock()

    def respond(self, body, headers=None):
        self.client._opener.open.return_value = Response(json.dumps(body, ensure_ascii=False).encode("utf-8"), headers)

    def test_real_wire_contract_and_trace_metadata(self):
        expected = completion()
        self.respond(expected)
        original = copy.deepcopy(MESSAGES)
        result = self.client.complete_json(MESSAGES)
        req = self.client._opener.open.call_args.args[0]
        payload = json.loads(req.data.decode("utf-8"))
        self.assertEqual(req.method, "POST")
        self.assertEqual(req.full_url, self.config.base_url + "/chat/completions")
        self.assertEqual(req.get_header("Authorization"), "Bearer " + self.config.api_key)
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertIs(payload["enable_thinking"], False)
        self.assertIs(payload["stream"], False)
        self.assertEqual(payload["max_tokens"], 8192)
        self.assertEqual(payload["model"], "qwen-plus")
        self.assertNotIn("extra_body", payload)
        self.assertEqual(MESSAGES, original)
        self.assertEqual(result["data"]["title"], "最后一把钥匙")
        self.assertEqual(result["request_id"], expected["id"])
        self.assertEqual(result["usage"], expected["usage"])
        self.assertEqual(result["finish_reason"], "stop")
        self.client._opener.open.assert_called_once()

    def test_truncated_response_rejected_even_if_json_is_valid(self):
        body = completion('{"complete":false}', reason="length")
        self.respond(body)
        with self.assertRaises(TextError) as raised:
            self.client.complete_json(MESSAGES)
        self.assertEqual(raised.exception.code, "OutputTruncated")
        self.assertEqual(raised.exception.request_id, body["id"])
        self.assertEqual(raised.exception.usage, body["usage"])
        self.assertIn("截断", str(raised.exception))
        self.client._opener.open.assert_called_once()

    def test_invalid_or_nonobject_json_is_never_repaired(self):
        for content in ('```json\n{"ok":true}\n```', '{"value":NaN}', '{"partial":',
                        '[]', 'null', '"text"', 'true'):
            with self.subTest(content=content):
                self.respond(completion(content))
                with self.assertRaises(TextError):
                    self.client.complete_json(MESSAGES)

    def test_refusal_and_nonstop_finish_rejected(self):
        refused = completion()
        refused["choices"][0]["message"]["refusal"] = "Cannot comply."
        for body in (refused, completion(reason="content_filter"), completion(reason="tool_calls"),
                     completion(reason=None)):
            with self.subTest(body=body):
                self.respond(body)
                with self.assertRaises(TextError):
                    self.client.complete_json(MESSAGES)

    def test_missing_empty_and_malformed_response_fields_rejected(self):
        bodies = [{}, {"choices": []}, {"choices": [None]}, {"choices": [{"finish_reason": "stop"}]},
                  completion(""), completion("   "), completion(None), []]
        for body in bodies:
            with self.subTest(body=body):
                self.respond(body)
                with self.assertRaises(TextError):
                    self.client.complete_json(MESSAGES)
        self.client._opener.open.return_value = Response(b"<html>error</html>")
        with self.assertRaisesRegex(TextError, "有效 JSON"):
            self.client.complete_json(MESSAGES)

    def test_message_validation_is_local_and_requires_user_or_system_json_guidance(self):
        invalid = [[], {}, [None], [{"role": "user", "content": "No format specified"}],
                   [{"role": "assistant", "content": "JSON"}],
                   [{"role": "user", "content": "Hello"}, {"role": "assistant", "content": "JSON"}],
                   [{"role": "tool", "content": "JSON"}], [{"role": "user", "content": []}],
                   [{"role": "user", "content": "   "}],
                   [{"role": "user", "content": "JSON", "tool_calls": []}]]
        for messages in invalid:
            with self.subTest(messages=messages), self.assertRaises(TextError):
                self.client.complete_json(messages)
        self.client._opener.open.assert_not_called()

    def test_empty_api_key_rejected_before_network(self):
        client = BailianTextClient(TextConfig())
        client._opener = Mock()
        with self.assertRaisesRegex(TextError, "DASHSCOPE_API_KEY"):
            client.complete_json(MESSAGES)
        client._opener.open.assert_not_called()

    def test_network_failure_not_retried_and_key_is_redacted(self):
        self.client._opener.open.side_effect = error.URLError("timeout " + self.config.api_key)
        with self.assertRaises(TextError) as raised:
            self.client.complete_json(MESSAGES)
        self.assertNotIn(self.config.api_key, str(raised.exception))
        self.assertIn("未自动重试", str(raised.exception))
        self.client._opener.open.assert_called_once()

    def test_http_and_200_business_errors_preserve_trace_without_key(self):
        body = {"error": {"code": "InvalidApiKey", "message": self.config.api_key}, "request_id": "req-error"}
        self.client._opener.open.side_effect = error.HTTPError(
            self.config.base_url, 401, "Unauthorized", {}, io.BytesIO(json.dumps(body).encode()))
        with self.assertRaises(TextError) as raised:
            self.client.complete_json(MESSAGES)
        self.assertEqual(raised.exception.code, "InvalidApiKey")
        self.assertEqual(raised.exception.http_status, 401)
        self.assertEqual(raised.exception.request_id, "req-error")
        self.assertNotIn(self.config.api_key, str(raised.exception))
        self.client._opener.open.side_effect = None
        self.respond(body)
        with self.assertRaises(TextError) as raised:
            self.client.complete_json(MESSAGES)
        self.assertNotIn(self.config.api_key, str(raised.exception))

    def test_redirect_handler_is_installed_and_redirect_is_actionable(self):
        actual = BailianTextClient(self.config)
        self.assertTrue(any(isinstance(handler, _NoRedirect) for handler in actual._opener.handlers))
        self.client._opener.open.side_effect = error.HTTPError(
            self.config.base_url, 302, "Moved", {"Location": "https://other.example/"}, io.BytesIO(b""))
        with self.assertRaisesRegex(TextError, "已阻止转发凭证"):
            self.client.complete_json(MESSAGES)
        self.client._opener.open.assert_called_once()

    def test_model_echo_of_credential_cannot_escape_in_returned_data(self):
        plain = json.dumps({"secret": self.config.api_key})
        for content in (plain, plain.replace("sk-", "\\u0073k-")):
            with self.subTest(content=content):
                self.respond(completion(content))
                with self.assertRaises(TextError) as raised:
                    self.client.complete_json(MESSAGES)
                self.assertNotIn(self.config.api_key, str(raised.exception))

    def test_request_id_header_is_used_if_body_omits_id(self):
        body = completion()
        del body["id"]
        self.respond(body, {"x-request-id": "header-trace"})
        self.assertEqual(self.client.complete_json(MESSAGES)["request_id"], "header-trace")


if __name__ == "__main__":
    unittest.main()
