"""Offline media feedback and evidence regressions. No cloud endpoints are contacted."""
import tempfile
import unittest
import io
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pianchang.asset_review import (_anchors, _expectations, _repair_instructions, _specifies_reference, evaluate_observations,
                          review_asset_id, review_studio_candidate, run_reference_loop)
from pianchang.collab_store import CollabStore
from pianchang.studio_assets import compose_reference_prompt, studio_asset_catalog
from tests.test_reference_assets import offline_png
from tests.test_studio_assets import OfflineStudio
from trace_system import TraceError, Tracer, use_context


class ReferenceLoopTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.service = OfflineStudio(Path(tmp.name))
        self.service.video_config = SimpleNamespace(request_timeout=5)
        context = use_context(None, tracer=Tracer(Path(tmp.name) / "offline-trace"))
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.calls = []
        def fake_image(config, payload):
            self.calls.append(payload)
            return {"request_id":"offline-" + str(len(self.calls)),
                    "output":{"choices":[{"message":{"content":[{"image":"https://offline.invalid"}]}}]}}
        for adapter in [patch("pianchang.asset_review._anchors",return_value=[]),
                        patch("pianchang.studio_assets._request_image",side_effect=fake_image),
                        patch("pianchang.studio_assets._download_image",side_effect=lambda *args: offline_png((len(self.calls),20,30)))]:
            adapter.start()
            self.addCleanup(adapter.stop)
        self.body = {"subject_id":"char:lin","purpose":"portrait","style":"冷色胶片","feedback":"眉部要清晰"}

    def test_review_of_actual_candidate_drives_next_prompt_and_freezes_evidence(self):
        seen = []
        def observe(service, brief, criteria, files):
            seen.append(files["candidate"].read_bytes())
            self.assertEqual(brief["shared_style"], "冷色胶片")
            self.assertIn("左眉有浅疤", str(brief["source_refs"]))
            rows = [{"criterion_id":key,"verdict":"PASS","reason":"测试候选左眉清晰，符合要求","frame_ids":["candidate"]}
                    for key in criteria]
            if len(seen) == 1:
                rows[0].update(verdict="FAIL", reason="测试：头发遮挡了左眉")
            return rows, {"request_id":"offline-vl-id"}
        with patch("pianchang.asset_review._observe_images",side_effect=observe):
            result = run_reference_loop(self.service, "project-a", self.body)
        self.assertEqual(seen, [offline_png((1,20,30)), offline_png((2,20,30))])
        self.assertIn("头发遮挡了左眉", self.calls[1]["input"]["messages"][0]["content"][0]["text"])
        self.assertEqual(result["verdict"],"PASS")
        self.assertEqual(result["status"],"verified")
        self.assertEqual([a["verdict"] for a in result["attempts"]], ["FAIL","PASS"])
        catalog = studio_asset_catalog(self.service,"project-a")
        store = CollabStore(catalog["store_path"])
        for attempt in result["attempts"]:
            saved = store.get_asset(review_asset_id(attempt["pin"]))
            self.assertEqual(saved["content"]["status"],attempt["verdict"])
            self.assertEqual(saved["content"]["candidate"],attempt["pin"])
            self.assertEqual(saved["dependencies"][0]["revision_id"],attempt["pin"]["revision_id"])

    def test_observer_outage_preserves_candidate_and_does_not_rebill(self):
        with patch("pianchang.asset_review._observe_images",side_effect=ValueError("offline observation unavailable")):
            result = run_reference_loop(self.service, "project-a", self.body)
        self.assertEqual((result["verdict"],len(self.calls)),("UNKNOWN",1))
        self.assertTrue(result["review"]["observation_error"])

    def test_trace_error_aborts_without_another_generation(self):
        with patch("pianchang.asset_review._observe_images",side_effect=TraceError("offline trace unavailable")):
            with self.assertRaises(TraceError):
                run_reference_loop(self.service, "project-a", self.body)
        self.assertEqual(len(self.calls),1)

    def test_cross_reference_check_requires_both_images_and_missing_items_stay_unknown(self):
        result = evaluate_observations({"anchor_0":"compare actual images","palette":"dark"},
            [{"criterion_id":"anchor_0","verdict":"PASS","reason":"offline claim","frame_ids":["candidate"]}],
            visual=True,anchors=[{"asset_id":"explicit-offline-anchor"}])
        self.assertEqual([r["status"] for r in result],["UNKNOWN","UNKNOWN"])

    def test_portrait_on_plain_background_is_not_subject_to_room_layout_or_performance_checks(self):
        contracts = {purpose:_expectations({"purpose":purpose, "human_feedback":"", "subject_id":"fixture"}, [])
                     for purpose in ("portrait","scene","costume","prop")}
        self.assertEqual(4,len({rules["asset_use"] for rules in contracts.values()}))
        self.assertIn("允许纯色背景", contracts["portrait"]["asset_use"])
        self.assertIn("不要求场景布置", contracts["portrait"]["asset_use"])
        self.assertIn("布局、陈设与光照", contracts["scene"]["asset_use"])
        self.assertIn("物件", contracts["prop"]["asset_use"])
        self.assertIn("独立挂拍", contracts["costume"]["asset_use"])

    def test_long_direction_compacts_without_sending_truncated_feedback(self):
        target = {"entity":self.service.project["entities"]["char:lin"], "purpose":"portrait"}
        brief = {"shared_style":"共同风格要求" * 300, "script_direction":{}, "script":{}, "human_feedback":"眉部要清晰"}
        self.service.text_config = SimpleNamespace(model="offline-text")
        with patch("pianchang.studio_assets.BailianTextClient") as client:
            complete = client.return_value.complete_json
            complete.side_effect = [{"data":{"prompt":"太长" * 900}}, {"data":{"prompt":"林岚短黑发，左眉浅疤清晰；修正遮挡。"}}]
            prompt = compose_reference_prompt(self.service,target,brief,"头发遮挡左眉")
        self.assertEqual(complete.call_count,2)
        request = str(complete.call_args_list[0].args[0])
        self.assertIn("头发遮挡左眉",request)
        self.assertIn("眉部要清晰",request)
        self.assertLessEqual(len(prompt.encode("utf8")),1300)

    def test_same_style_pending_candidate_uses_both_real_images_and_records_its_review(self):
        work = {"style":self.body["style"], "items":[]}
        palette_drift = False
        def observe(service, brief, criteria, files):
            if brief["purpose"] == "prop":
                self.assertEqual(files["anchor_0"].read_bytes(),offline_png((1,20,30)))
                self.assertEqual(files["candidate"].read_bytes(),offline_png((2,20,30)))
            rows = [{"criterion_id":key,"verdict":"UNKNOWN" if key.startswith("anchor_") else "PASS","reason":"offline visual observation",
                     "frame_ids":["candidate", key] if key.startswith("anchor_") else ["candidate"]} for key in criteria]
            if palette_drift:
                for row in rows:
                    if row["criterion_id"] == "palette":
                        row.update(verdict="FAIL", reason="offline：主体颜色与剧本指定的材质不符")
            return rows, {"request_id":"offline"}
        with patch("pianchang.asset_review._anchors",side_effect=_anchors), patch("pianchang.asset_dispatch.asset_work_state",return_value=work), \
             patch("pianchang.asset_review._observe_images",side_effect=observe):
            first = run_reference_loop(self.service, "project-a", self.body)
            work["items"].append({"status":"awaiting_review", "candidate":first["pin"], "review":first["review"]})
            next_body = dict(self.body, subject_id="prop:key", purpose="prop")
            second = run_reference_loop(self.service, "project-a", next_body)
            optional = next(c for c in second["review"]["checks"] if c["path"] == "anchor_0")
            self.assertEqual((optional["required"],optional["status"]),(False,"UNKNOWN"))
            self.assertEqual(len(second["attempts"]),1)
            self.assertEqual(second["verdict"],"PASS")
            self.assertEqual(_repair_instructions(second["review"]),"")
            self.assertIn("辅助对照", second["review"]["coverage"])
            # An explicit direction makes a cross-type comparison a required check on the very same pixels.
            explicit = review_studio_candidate(self.service,"project-a",second["pin"],
                                               dict(next_body,feedback="配色必须参照林岚的定妆照"))
            mandatory = next(c for c in explicit["checks"] if c["path"] == "anchor_0")
            self.assertEqual((mandatory["required"],explicit["status"]),(True,"UNKNOWN"))
            with patch("pianchang.asset_review._anchors",return_value=[]):
                retried = review_studio_candidate(self.service,"project-a",second["pin"],next_body,
                                                  prior_visual_review=explicit)
            self.assertEqual(retried["previous_review_ref"],explicit["review_ref"])
            self.assertEqual(retried["anchor_pins"],explicit["anchor_pins"])
            self.assertEqual(retried["visual_input_version"],"reference-visual-input/3")
            self.assertEqual(retried["status"],"UNKNOWN")
            self.assertTrue(next(c for c in retried["checks"] if c["path"]=="anchor_0")["required"])
            palette_drift = True
            drift = review_studio_candidate(self.service,"project-a",second["pin"],next_body)
            self.assertEqual(drift["status"],"FAIL")
            repairs = _repair_instructions(drift)
            self.assertIn("palette",repairs)
            self.assertNotIn("anchor_0",repairs)
        self.assertEqual(len(self.calls),2)
        self.assertEqual(second["review"]["anchor_pins"], [first["pin"]])
        self.assertEqual(second["review"]["anchors"][0]["status"], "awaiting_review")
        self.assertEqual(second["review"]["anchors"][0]["review_ref"], first["review"]["review_ref"])
        self.assertIn("offline visual observation", self.calls[-1]["input"]["messages"][0]["content"][0]["text"])

    def test_explicit_version_and_named_comparison_are_not_generic_style_context(self):
        brief = {"declared_refs":[{"asset_id":"ref-A","revision_id":"r2"}], "human_feedback":"",
                 "shared_style":"写实", "script_direction":{}}
        pin = {"asset_id":"ref-A","revision_id":"r2","subject_id":"char:lin"}
        self.assertTrue(_specifies_reference(brief,pin,"林岚"))
        self.assertFalse(_specifies_reference(brief,dict(pin,revision_id="r1"),"林岚"))
        brief.update(declared_refs=[],human_feedback="林岚进店，桌上有一捧花。")
        self.assertFalse(_specifies_reference(brief,pin,"林岚"))

    def test_voice_uses_audio_listener_and_stops_after_budget(self):
        stream = io.BytesIO()
        with wave.open(stream,"wb") as out:
            out.setparams((1,2,8000,0,"NONE",""))
            out.writeframes(b"\x00\x00" * 9600)
        sample = stream.getvalue()
        def hear(service, data, mime, brief, criteria, *, with_receipt):
            self.assertEqual(data,sample)
            self.assertEqual(mime,"audio/wav")
            self.assertTrue(with_receipt)
            return ([{"criterion_id":k,"verdict":"FAIL","reason":"模拟试听的音量过低"} for k in criteria],
                    {"request_id":"offline-hearing"})
        with patch("pianchang.asset_voice.synthesize_voice",return_value=(sample,"audio/wav",{"model":"offline-tts"})) as synth, \
             patch("pianchang.asset_voice.observe_voice",side_effect=hear):
            result = run_reference_loop(self.service,"project-a",dict(self.body,purpose="voice",max_attempts=2))
        self.assertEqual(result["status"],"needs_attention")
        self.assertEqual(synth.call_count,2)
        self.assertEqual(result["review"]["provider"]["request_id"],"offline-hearing")
