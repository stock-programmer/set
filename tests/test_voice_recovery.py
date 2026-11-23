import json
import tempfile
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

from pianchang.voice_recovery import _casting_recipe, original_voice_recipe, verified_voice_recovery
from trace_system import get_tracer, use_context


class VoiceRecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(use_context(None, tracer=get_tracer(directory)))

    def test_exact_request_reuses_its_own_completed_casting(self):
        cast = {"request_kind":"model","trace_id":"trace","span_id":"cast","response_seq":12,"http_status":200}
        audio = {"request_kind":"audio","trace_id":"trace","span_id":"audio","request_seq":14}
        design = {"voice":"sage","instructions":"轻声","tempo":0.9,"gain_db":1}
        brief = {"purpose":"voice","sample_text":"早安","canonical":{"name":"测试"},"shots":["S001","S002"]}
        cast_body = {"brief":brief,"text":"早安","feedback":""}
        spans = {"cast":{
            "http.request":{"body":{"messages":[{"role":"user","content":json.dumps(cast_body)}]}},
            "http.response":{"http_status":200,"header_request_id":"casting-id","body":{"choices":[
                {"finish_reason":"stop","message":{"content":json.dumps(design)}}]}}},
            "audio":{"model.route":{"route":{"model_id":"audio"}},"http.request":{"wire_body_sha256":"a"*64}}}
        tracer = SimpleNamespace(resolve=lambda v:v, get_events=lambda tid,*,span_id,names:
            [{"name":name,"data":spans[span_id][name]} for name in names])
        original, recipe = _casting_recipe(tracer,[cast,audio])
        self.assertEqual(original_voice_recipe(dict(brief,shots=["S001"]),(original["brief"],recipe)),
                         {**design,"sample_text":"早安","casting_request_id":"casting-id","route":{"model_id":"audio"},
                          "wire_body_sha256":"a"*64})
        with self.assertRaises(ValueError):
            original_voice_recipe(dict(brief,sample_text="晚安"),(brief,recipe))
        with self.assertRaises(ValueError):
            _casting_recipe(tracer,[cast,audio,audio])

    def test_worker_requires_authorized_source_with_positive_non_dispatch_proof(self):
        jid, pid = "a"*32, "b"*16
        job = {"id":jid,"kind":"asset_production","status":"failed","project_id":pid}
        service = SimpleNamespace(state={"jobs":[job]}, lock=nullcontext(),root=".")
        evidence = {"reason":"voice_request_not_sent","missing_count":1,"receipt_unknown":True,
                    "transport_failure":{"dispatch":"unknown"}}
        with patch("tools.workbench_loop_dependency.recheck_dependency",return_value=evidence) as check:
            with self.assertRaises(ValueError):
                verified_voice_recovery(service,pid,jid,{})
            check.assert_called_once()
            job["_voice_recovery_from"]="c"*32
            with self.assertRaises(ValueError):
                verified_voice_recovery(service,pid,jid,{})
            self.assertEqual(check.call_count,1)
