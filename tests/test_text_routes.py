"""Offline checks for server-selected decision and structured text routes."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pi_bridge.model_gateway import ModelGateway
from pi_bridge.runtime import make_loop
from pianchang.bailian_text import BailianTextClient, TextError, load_text_config, text_client_for
from pianchang.story_agent import StoryRunner
from trace_system import TraceError, get_tracer, use_context


class TextRouteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env_file = self.root / ".env"
        env_file.write_text("SCRIPT_TEST_TOKEN=offline-script\nVIDEO_TEST_TOKEN=offline-video\n", encoding="utf-8")
        folder = self.root / "config"
        folder.mkdir()
        connections, models = {}, {}
        for name in ("script", "video"):
            connections[name] = {"provider": name, "account": name + "-account",
                                 "base_url": "https://" + name + ".example.test/v1",
                                 "api_key_env": name.upper() + "_TEST_TOKEN"}
            models[name] = {"adapter": "litellm", "sdk_provider": "openai", "connection": name,
                            "model_id": name + "-v1"}
        self.routing = {"version": 1, "connections": connections, "models": models,
                        "routes": {"script.decision": "script", "video.decision": "video",
                                   "story.compile": "video", "video.repair_strategy": "script"}}
        (folder / "models.json").write_text(json.dumps(self.routing), encoding="utf-8")
        self.config = load_text_config(env_file)
        self.tracer = get_tracer(self.root / "traces")

    def test_pi_dispatch_pins_role_for_tools_and_compaction(self):
        client = BailianTextClient(self.config)
        reply = {"choices": [{"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}]}
        with use_context(None, tracer=self.tracer), self.tracer.trace("test.route.decisions"), \
                patch("pianchang.sdk_transport.call_sdk", return_value=reply) as sdk:
            for role in ("script", "video"):
                loop = make_loop(client, [], self.root / role, role=role, snapshot={}, finish_check=lambda: {})
                self.assertIn("VIDEO_TEST_TOKEN", loop.credential_env_names)
                charged = Mock()
                gateway = ModelGateway(self.config, self.tracer, charged, route=loop.model_route)
                self.assertEqual(gateway.model, role + "-v1")
                self.assertEqual(gateway.route.account, role + "-account")
                self.assertEqual(gateway.api_key, "offline-" + role)
                call = {"id": "tool-id", "type": "function", "function": {"name": "read_fixture", "arguments": "{}"}}
                history = [{"role": "user", "content": "read"}, {"role": "assistant", "tool_calls": [call]},
                           {"role": "tool", "tool_call_id": call["id"], "content": "fixture"}]
                gateway.exchange({"messages": history, "model": "client-override", "api_base": "untrusted",
                                  "tools": [{"type": "function", "function": {"name": "read_fixture"}}]})
                self.assertEqual(sdk.call_args.args, (loop.model_route, "completion"))
                self.assertEqual(sdk.call_args.kwargs["messages"], history)
                self.assertTrue(sdk.call_args.kwargs["tools"])
                self.assertFalse({"model", "api_base", "enable_thinking"} & sdk.call_args.kwargs.keys())
                gateway.exchange({"messages": [{"role": "user", "content": "summarize"}]}, purpose="compaction")
                self.assertEqual(charged.call_args.kwargs["purpose"], "compaction")
                self.assertEqual(gateway.calls, 2)
                self.assertEqual(charged.call_count, 2)
            self.assertEqual(sdk.call_count, 4)

    def test_helper_uses_own_route_and_validates_provider_output(self):
        client = BailianTextClient(self.config)
        content = [{"role": "user", "content": "Give a JSON object"}]
        reply = {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                 "_provider_request_id": "actual-provider-id"}
        with use_context(None, tracer=self.tracer), self.tracer.trace("test.route.structured"), \
                patch("pianchang.sdk_transport.call_sdk", return_value=reply) as sdk:
            compiler = StoryRunner(self.config)
            self.assertEqual(compiler.config.model, "video-v1")
            actual = compiler.client.complete_json(content)
            self.assertEqual(actual["request_id"], "actual-provider-id")
            self.assertEqual(actual["data"], {})
            self.assertEqual(sdk.call_args.kwargs["response_format"], {"type": "json_object"})
            self.assertNotIn("enable_thinking", sdk.call_args.kwargs)
            repair = text_client_for(client, "video.repair_strategy")
            self.assertEqual(repair.config.model, "script-v1")
            reply["choices"][0]["finish_reason"] = "length"
            with self.assertRaises(TextError) as error:
                repair.complete_json(content)
            self.assertEqual(error.exception.code, "OutputTruncated")
            self.assertEqual(sdk.call_count, 2)
            sdk.side_effect = RuntimeError("failed using offline-script")
            with self.assertRaises(TextError) as error:
                repair.complete_json(content)
            self.assertNotIn("offline-script", str(error.exception))
            failure = TraceError("cannot persist request")
            sdk.side_effect = failure
            with self.assertRaises(TraceError) as error:
                repair.complete_json(content)
            self.assertIs(error.exception, failure)
