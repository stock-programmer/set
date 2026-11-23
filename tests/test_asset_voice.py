"""Offline speech generation and listening contracts. No provider connection."""
import base64
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
import wave
from types import SimpleNamespace
from unittest.mock import patch

from pianchang.asset_voice import observe_voice, sample_text, synthesize_voice
from pianchang.bailian_video import VideoConfig
from pianchang.model_routes import ModelRoute
from trace_system import Tracer, use_context


class VoiceTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("litellm"), "optional LiteLLM SDK")
    def test_openai_audition_sends_cast_voice_and_exact_script_and_keeps_receipt(self):
        import httpx
        from unittest.mock import Mock

        wav_file = io.BytesIO()
        with wave.open(wav_file, "wb") as wav:
            wav.setparams((1, 2, 16000, 160, "NONE", "not compressed"))
            wav.writeframes(b"\x01\x00" * 160)
        raw = wav_file.getvalue()
        encoded = base64.b64encode(raw).decode()
        voice_route = ModelRoute("litellm", "openai", "studio", "https://voice.invalid/v1", "gpt-audio-1.5",
                                 "offline-test-voice", "openai", "openai-studio")
        routes = Mock()
        routes.resolve.return_value = voice_route
        service = SimpleNamespace(text_config=None, video_config=VideoConfig(), model_routes=routes)
        received = []
        digests = []
        brief = {"name": "角色", "sample_text": "我们出发。"}

        def respond(req):
            payload = json.loads(req.content)
            received.append((req.url.path, payload))
            digests.append(hashlib.sha256(req.content).hexdigest())
            return httpx.Response(200, headers={"x-request-id": "offline-request"}, json={
                "id": "chatcmpl-voice", "object": "chat.completion", "created": 1, "model": voice_route.model_id,
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": None,
                             "audio": {"id": "audio-test", "data": encoded, "transcript": brief["sample_text"],
                                       "expires_at": 9999999999}}}]})

        with tempfile.TemporaryDirectory() as tmp:
            tracer = Tracer(tmp)
            with use_context(None, tracer=tracer), patch("pianchang.asset_voice.BailianTextClient") as cast, \
                 patch("pianchang.sdk_transport._upstream_transport", return_value=httpx.MockTransport(respond)):
                cast.return_value.complete_json.return_value = {"data": {"voice": "alloy", "instructions": "沉稳。"}}
                data, mime, origin = synthesize_voice(service, brief)
                replay = {"voice":"alloy", "instructions":"沉稳。", "tempo":1, "gain_db":0,
                          "casting_request_id":"existing-cast", "route":voice_route.snapshot(),
                          "sample_text":brief["sample_text"], "wire_body_sha256":digests[0]}
                replayed, _, provenance = synthesize_voice(service, brief, replay=replay)
                self.assertEqual((replayed,provenance["casting_request_id"]), (data,"existing-cast"))
                for change in ({"wire_body_sha256":"0"*64}, {"sample_text":"changed"}, {"route":{}}):
                    with self.subTest(change=list(change)), self.assertRaises(ValueError):
                        synthesize_voice(service, brief, replay={**replay, **change})
                cast.assert_called_once()
            self.assertEqual(digests, [digests[0]]*2)
            path, payload = received[0]
            self.assertEqual((path, payload["model"]), ("/v1/chat/completions", voice_route.model_id))
            self.assertEqual(payload["audio"], {"voice": "alloy", "format": "wav"})
            self.assertEqual(payload["modalities"], ["text", "audio"])
            self.assertEqual(json.loads(payload["messages"][-1]["content"]),
                             {"text": brief["sample_text"], "direction": "沉稳。"})
            self.assertEqual((data, mime, origin["request_id"], origin["audio_id"]),
                             (raw, "audio/wav", "offline-request", "audio-test"))
            traces = json.dumps([tracer.export_trace(t["trace_id"]) for t in tracer.list_traces()])
            self.assertNotIn(encoded, traces)
            self.assertIn("inline_media", traces)

    def test_audition_uses_only_the_named_characters_quoted_line(self):
        brief = {"name": "林薇", "shots": [{"sources": [{"quote": "小雅说：“你是谁？”林薇：“等一下。”"}]}]}
        self.assertEqual(sample_text(brief), "等一下。")

    def test_offline_provider_reads_exact_audio_and_traces_evidence_without_raw_media(self):
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setparams((1, 2, 16000, 160, "NONE", "not compressed"))
            wav.writeframes(b"\x02\x00" * 160)
        sound = buffer.getvalue()
        encoded = base64.b64encode(sound).decode()
        outgoing = []

        def exchange(req, timeout):
            body = json.loads(req.data)
            outgoing.append(body)
            if body["model"].startswith("qwen3-tts"):
                result = {"request_id": "offline-tts", "output": {"audio": {"data": encoded, "id": "offline-voice"}}}
            else:
                rows = [{"criterion_id": "voice", "verdict": "UNKNOWN", "reason": "offline synthetic sample"}]
                result = {"id": "offline-listen", "choices": [{"finish_reason": "stop",
                          "message": {"content": json.dumps({"observations": rows})}}]}
            response = io.BytesIO(json.dumps(result).encode())
            response.headers, response.status = {}, 200
            return response

        config = SimpleNamespace(api_key="offline-credential-voice-test", base_url="https://offline.invalid",
                                 request_timeout=5)
        service = SimpleNamespace(text_config=config, video_config=config)
        brief = {"name": "离线角色", "sample_text": "等等。"}
        with tempfile.TemporaryDirectory() as temporary:
            tracer = Tracer(temporary)
            with use_context(None, tracer=tracer), patch("pianchang.asset_voice.BailianTextClient") as client, \
                 patch("pianchang.asset_voice.request.build_opener") as opener:
                client.return_value.complete_json.return_value = {"data": {"voice": "Kai", "instructions": "压低声音。"}}
                opener.return_value.open.side_effect = exchange
                data, mime, origin = synthesize_voice(service, brief, "慢一些")
                result = observe_voice(service, data, mime, brief, {"voice": "声音舒缓"})
            self.assertEqual(data, sound)
            self.assertEqual((origin["request_id"], result[0]["verdict"]), ("offline-tts", "UNKNOWN"))
            self.assertEqual(outgoing[0]["input"]["text"], "等等。")
            self.assertEqual(outgoing[1]["messages"][0]["content"][0]["input_audio"]["data"], "data:audio/wav;base64," + encoded)
            trace_data = json.dumps([tracer.export_trace(t["trace_id"]) for t in tracer.list_traces()])
            self.assertIn("offline-tts", trace_data)
            self.assertNotIn(encoded, trace_data)
            self.assertNotIn(config.api_key, trace_data)
