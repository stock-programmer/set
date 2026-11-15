"""Offline speech generation and listening contracts. No provider connection."""
import base64
import io
import json
import tempfile
import unittest
import wave
from types import SimpleNamespace
from unittest.mock import patch

from pianchang.asset_voice import observe_voice, sample_text, synthesize_voice
from trace_system import Tracer, use_context


class VoiceTests(unittest.TestCase):
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
