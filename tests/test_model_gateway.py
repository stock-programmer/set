import importlib.util
import gzip
import hashlib
import json
import os
import socket
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from pianchang.model_routes import ModelRoute, load_routes
from pianchang.sdk_transport import call_sdk
from trace_system import TraceError, Tracer, use_context


class RouteTests(unittest.TestCase):
    def test_openai_ark_project_has_no_bailian_fallback(self):
        from pianchang.app_service import AppService
        from pianchang.bailian_video import BailianVideoClient, config_for_references, load_config
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = root / ".env"
            config = root / "config/models.json"
            config.parent.mkdir()
            sample = Path(__file__).resolve().parents[1] / "config/models.example.json"
            settings = json.loads(sample.read_text("utf-8"))
            settings["disabled_providers"] = ["aliyun-bailian"]
            settings["models"]["test-review"] = {
                "connection": "openai-art", "adapter": "openai", "model_id": "test-review"}
            settings["models"]["test-chat"] = {
                "connection": "openai-art", "adapter": "litellm", "sdk_provider": "openai", "model_id": "test-chat"}
            settings["routes"] = dict.fromkeys(
                ("script.decision", "video.decision", "story.compile", "reference.prompt", "voice.casting",
                 "video.repair_strategy", "voice.synthesis"), "test-chat")
            settings["routes"].update(dict.fromkeys(("vision.review", "continuity.review", "voice.review"), "test-review"))
            settings["routes"].update({"reference.image": "gpt-reference-image", "video.text_to_video": "seedance-reference-video",
                                       "video.reference_to_video": "seedance-reference-video"})
            config.write_text(json.dumps(settings), encoding="utf-8")
            with patch.dict(os.environ, {"OPENAI_API_KEY": "fake-openai-key", "ARK_VIDEO_API_KEY": "fake-ark-key",
                                         "DASHSCOPE_API_KEY": "", "AGENT_TRACE_DIR": str(root / "traces")}):
                service = AppService(root)
                try:
                    routes = service.bootstrap(include_jobs=False)["config"]["model_routes"]
                    self.assertEqual(len(routes), 13)
                    self.assertEqual({p for p, r in routes.items() if r["provider"] == "volcengine-ark"},
                                     {"video.text_to_video", "video.reference_to_video"})
                    self.assertEqual(sum(r["provider"] == "openai" for r in routes.values()), 11)
                    self.assertTrue(all(r["key_configured"] for r in routes.values()))
                    self.assertNotIn("fake-openai-key", json.dumps(routes))
                finally:
                    service.close()
                with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "old-local-key"}):
                    old_config = load_config(env, allow_missing=True)
                    with self.assertRaisesRegex(ValueError, "已.*禁用"):
                        config_for_references(old_config, [], legacy=True)
                    client = BailianVideoClient(old_config)
                    with patch.object(client._api_opener, "open") as http:
                        with self.assertRaisesRegex(ValueError, "已.*禁用"):
                            client.get_task("old-task")
                        http.assert_not_called()
                incomplete = json.loads(config.read_text("utf-8"))
                del incomplete["routes"]["voice.review"]
                config.write_text(json.dumps(incomplete), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "voice.review.*禁用"):
                    load_routes(env)

    def test_workbench_reports_each_purpose_without_exposing_credentials(self):
        from pianchang.app_service import AppService
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            sample = Path(__file__).resolve().parents[1] / "config" / "models.example.json"
            (root / "config/models.json").write_bytes(sample.read_bytes())
            env = {"AGENT_TRACE_DIR": str(root / "traces"), "OPENAI_API_KEY": "synthetic-art-key",
                   "DASHSCOPE_API_KEY": "", "ARK_VIDEO_API_KEY": "", "GEMINI_API_KEY": ""}
            with patch.dict(os.environ, env):
                service = AppService(root)
                try:
                    config = service.bootstrap()["config"]
                    self.assertTrue(config["model_routes"]["reference.image"]["key_configured"])
                    self.assertFalse(config["model_routes"]["script.decision"]["key_configured"])
                    self.assertTrue(config["key_configured"])
                    self.assertNotIn("synthetic-art-key", json.dumps(config))
                finally:
                    service.close()

    def test_independent_routes_and_frozen_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config" / "models.json"
            config.parent.mkdir()
            env = root / ".env"
            with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "old-local-key", "FILM_KEY": "first-local-key"}):
                original = load_routes(env)
                self.assertEqual(original.resolve("video.decision"), original.resolve("script.decision"))
                data = {"version": 1, "connections": {"film": {"provider": "ark", "account": "production",
                        "base_url": "https://ark.invalid/api/v3", "api_key_env": "FILM_KEY"}},
                        "models": {"take": {"adapter": "litellm", "connection": "film",
                        "model_id": "endpoint-one", "sdk_provider": "volcengine"}},
                        "routes": {"video.text_to_video": "take"}}
                config.write_text(json.dumps(data))
                configured = load_routes(env)
                snapshot = configured.resolve("video.text_to_video").snapshot()
                self.assertEqual(configured.resolve("reference.image"), original.resolve("reference.image"))
                self.assertEqual(configured.describe()["video.text_to_video"]["model"], "endpoint-one")
                self.assertNotIn("local-key", json.dumps([snapshot, configured.describe()]))
                data["models"].clear()
                data["routes"].clear()
                config.write_text(json.dumps(data))
                with patch.dict(os.environ, {"FILM_KEY": "rotated-key"}):
                    recovered = load_routes(env).restore(snapshot)
                    self.assertEqual(recovered.model_id, "endpoint-one")
                    self.assertEqual(recovered.api_key, "rotated-key")
                data["connections"]["film"]["account"] = "other"
                config.write_text(json.dumps(data))
                with self.assertRaisesRegex(ValueError, "冻结任务"):
                    load_routes(env).restore(snapshot)

    def test_rejects_malformed_routes_before_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            config = root / "config" / "models.json"
            for raw in ('{"version":1,"version":1}', '{"version":1,"routes":{"typo":"bailian.text"}}',
                        '{"version":1,"routes":{"reference.image":"missing"}}'):
                with self.subTest(raw=raw), self.assertRaises(ValueError):
                    config.write_text(raw)
                    load_routes(root / ".env")

    def test_image_size_belongs_to_image_model_and_survives_config_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config/models.json"
            config.parent.mkdir()
            sample = Path(__file__).resolve().parents[1] / "config/models.example.json"
            settings = json.loads(sample.read_text("utf-8"))
            alias = settings["routes"]["reference.image"]
            model = settings["models"][alias]
            model["image_size"] = "2K"
            config.write_text(json.dumps(settings), encoding="utf-8")
            original = load_routes(root / ".env").resolve("reference.image").snapshot()
            model["image_size"] = "1024x1024"
            config.write_text(json.dumps(settings), encoding="utf-8")
            registry = load_routes(root / ".env")
            self.assertEqual(registry.resolve("reference.image").image_size, "1024x1024")
            self.assertEqual(registry.restore(original).image_size, "2K")
            settings["routes"]["video.decision"] = alias
            config.write_text(json.dumps(settings), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "video.decision.*image_size"):
                load_routes(root / ".env")


@unittest.skipUnless(importlib.util.find_spec("litellm"), "optional LiteLLM SDK")
class SDKTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.route = ModelRoute("litellm", "example", "studio", "https://model.invalid/v1", "selected-model",
                                "synthetic-local-key", "openai", "studio")
        self.context = use_context(None, tracer=Tracer(self.tmp.name))
        self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        original_connect = socket.socket.connect
        def local_connect(sock, address):
            # Windows asyncio creates a loopback socket pair even for a synchronous SDK call.
            if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
                return original_connect(sock, address)
            raise AssertionError("unexpected network connection")
        block = patch("socket.socket.connect", local_connect)
        block.start()
        self.addCleanup(block.stop)

    def run_sdk(self, operation, reply, **kwargs):
        import httpx
        requests = []
        def send(req):
            requests.append(req)
            return httpx.Response(200, json=reply, headers={"x-request-id": "test-request"})
        with patch("pianchang.sdk_transport._upstream_transport", return_value=httpx.MockTransport(send)):
            result = call_sdk(self.route, operation, timeout=5, **kwargs)
        self.assertEqual(len(requests), 1)
        self.assertEqual(json.loads(requests[0].content)["model"], self.route.model_id)
        self.assertEqual(result["_provider_request_id"], "test-request")
        self.assertTrue(result["_wire_receipt"]["trace_context"]["trace_id"])
        return requests[0].url.path, result

    def test_actual_sdk_chat_and_image_serialization(self):
        path, result = self.run_sdk("completion", {"id": "chat-test", "object": "chat.completion", "created": 1,
            "model": "selected-model", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop"}]}, messages=[{"role": "user", "content": "test"}])
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        self.route = replace(self.route, model_id="gpt-image-2.5-flare")
        path, result = self.run_sdk("image_generation", {"created": 1, "data": [{"b64_json": "dGVzdA=="}]},
                                    prompt="test", n=1, size="1024x1024")
        self.assertEqual(path, "/v1/images/generations")
        self.assertEqual(result["data"][0]["b64_json"], "dGVzdA==")

    def test_trace_failure_stops_before_send_and_provider_error_never_reposts(self):
        import httpx
        requests = []
        def fail(req):
            requests.append(req)
            return httpx.Response(502, json={"error": {"message": "temporary"}})
        kwargs = {"messages": [{"role": "user", "content": "test"}]}
        with patch("pianchang.sdk_transport._upstream_transport", return_value=httpx.MockTransport(fail)):
            with patch("pianchang.sdk_transport._trace_http_request", side_effect=TraceError("store unavailable")):
                with self.assertRaises(TraceError):
                    call_sdk(self.route, "completion", timeout=3, **kwargs)
                self.assertFalse(requests)
            with self.assertRaises(Exception):
                call_sdk(self.route, "completion", timeout=3, **kwargs)
            self.assertEqual(len(requests), 1)

    def test_concurrent_connections_keep_their_credentials_and_endpoints(self):
        import httpx
        requests = []
        def reply(req):
            requests.append((req.url.host, req.headers["authorization"], json.loads(req.content)["model"]))
            return httpx.Response(200, json={"created": 1, "data": [{"b64_json": "dGVzdA=="}]})
        second = replace(self.route, base_url="https://second.invalid/v2", account="other-account",
                         api_key="other-local-key", model_id="second-model")
        tracer = Tracer(self.tmp.name)
        def generate(route):
            with use_context(None, tracer=tracer):
                return call_sdk(route, "image_generation", timeout=5, prompt="test", n=1)
        with patch("pianchang.sdk_transport._upstream_transport", side_effect=lambda: httpx.MockTransport(reply)):
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(generate, [self.route, second]))
        self.assertCountEqual(requests, [("model.invalid", "Bearer synthetic-local-key", "selected-model"),
                                         ("second.invalid", "Bearer other-local-key", "second-model")])
        for path in Path(self.tmp.name).rglob("*"):
            if path.is_file():
                stored = path.read_bytes()
                self.assertNotIn(b"synthetic-local-key", stored)
                self.assertNotIn(b"other-local-key", stored)

    def test_gzip_response_records_the_wire_digest_and_the_decoded_result(self):
        import httpx
        from pianchang.bailian_video import _trace_http_response
        raw = json.dumps({"created": 1, "data": [{"b64_json": "dGVzdA=="}]}).encode()
        compressed = gzip.compress(raw)
        def reply(req):
            return httpx.Response(200, headers={"content-encoding": "gzip", "content-type": "application/json"},
                                  stream=httpx.ByteStream(compressed))
        with patch("pianchang.sdk_transport._upstream_transport", return_value=httpx.MockTransport(reply)), \
             patch("pianchang.sdk_transport._trace_http_response", wraps=_trace_http_response) as recorded:
            result = call_sdk(self.route, "image_generation", timeout=5, prompt="synthetic")
        self.assertEqual(recorded.call_args.args[0].captured_wire_sha256, hashlib.sha256(compressed).hexdigest())
        self.assertEqual(recorded.call_args.args[1], raw)
        self.assertEqual(result["data"][0]["b64_json"], "dGVzdA==")
