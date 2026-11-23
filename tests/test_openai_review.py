"""Exercise the OpenAI review wire formats with local media and fake model responses."""
import base64
import json
import shutil
import tempfile
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pianchang.acceptance import file_sha256
from pianchang.asset_review import _repair_instructions
from pianchang.asset_voice import OPENAI_LISTEN_FORMAT, observe_voice
from pianchang.bailian_observer import BailianObserver, RoutedVisionConfig
from pianchang.content_review import VERSION, _ffmpeg, prepare_video, review_video, validate_content_evidence
from pianchang.openai_review import merge_cut
from pianchang.creative_sound import POLICY, combine, effective_results, review_scope
from pianchang.story_spec import digest_json
from trace_system import Tracer, trace, use_context


def reply(model, rows):
    return {"id": "offline-" + model, "model": model, "choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps({"observations": rows})}}]}, None


class OpenAIReviewTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.folder = Path(folder)
        self.enterContext(use_context(None, tracer=Tracer(self.folder / "trace")))
        self.enterContext(trace("offline.openai.review"))
        self.audio = RoutedVisionConfig(api_key="test-audio", base_url="https://api.openai.com/v1",
                                        model="audio", adapter="openai", route_account="audio-account")
        self.config = replace(self.audio, api_key="test-visual", model="visual", continuity_config=self.audio)
        self.wave = self.folder / "audio.wav"
        with wave.open(str(self.wave), "wb") as f:
            f.setparams((1, 2, 8000, 0, "NONE", ""))
            f.writeframes(bytes(16000))

    def test_echoed_audio_cannot_locate_the_missing_side_of_a_visual_cut(self):
        row = {
            "criterion_id": "cut", "verdict": "PASS", "reason": "fixture",
            "components": [{"name": kind, "verdict": "PASS", "reason": "fixture"}
                           for kind in ("identity", "costume_props", "space", "action")],
            "evidence": [{"asset_id": "V0", "start_s": 0, "end_s": .4},
                         {"asset_id": "A0", "start_s": 0, "end_s": 1}],
        }
        with self.assertRaisesRegex(ValueError, "两侧"):
            merge_cut({"data": {"observations": [row]}}, {}, .5)

    def test_malformed_visual_components_preserve_only_the_independent_audio_verdict(self):
        visual = {"criterion_id":"pair", "verdict":"FAIL", "reason":"unreliable", "components":["broken"],
                  "evidence":[{"asset_id":"V0", "start_s":0, "end_s":1}]}
        for sound_verdict, expected in (("PASS","UNKNOWN"), ("FAIL","FAIL")):
            sound = {"criterion_id":"pair", "verdict":sound_verdict, "reason":"actual original audio",
                     "evidence":[{"asset_id":"A0", "start_s":0, "end_s":1}]}
            result = merge_cut({"data":{"observations":[visual]}}, {"data":{"observations":[sound]}}, .5)
            row = result["data"]["observations"][0]
            self.assertEqual(row["verdict"], expected)
            self.assertEqual([c["verdict"] for c in row["components"]], ["UNKNOWN"] * 4 + [sound_verdict])

    def test_spoken_beat_uses_original_wave_and_keeps_independent_visual_failures(self):
        cid = "creative:S001"
        shot = {"id": "G001", "creative_shots": [
            {"id": "S001", "description": "屏幕行情跳动，财经主播播报市场下跌。"},
            {"id": "S002", "description": "交易员把软件缩小。"}]}
        timing = [{"criterion_id": cid, "status": "FAIL", "execution_status": "executed",
                   "evidence": [{"asset_id": "V0", "start_s": .1, "end_s": .6}]},
                  {"criterion_id": "action_sequence"}]
        scopes = review_scope(shot, timing, 1.)
        self.assertEqual(scopes[cid]["visual"], ["屏幕行情跳动"])
        self.assertEqual(scopes[cid]["mixed_source"], [])
        self.assertIn("交易员把软件缩小", scopes["action_sequence"]["visual"])
        legacy = {"content_review_version": "grounded-video-review/2", "results": [
            {**timing[0], "required": True}, {"criterion_id": "creative:S002", "status": "FAIL"},
            {"criterion_id": "action_sequence", "status": "PASS"}]}
        self.assertEqual([r["status"] for r in effective_results(shot, legacy)], ["UNKNOWN", "FAIL", "UNKNOWN"])
        legacy["results"][0]["creative_audio_policy"] = POLICY
        self.assertEqual(effective_results(shot, legacy)[0]["status"], "FAIL")
        source = {"path": str(self.wave), "sha256": file_sha256(self.wave)}
        audio = {"id": "A0", **source, "source_path": str(self.wave), "source_sha256": source["sha256"]}
        video = {**source, "id": "V0", "duration_s": 1., "scope": "offline mocked frames", "binding": {"source": source}}
        timing_path = self.folder / "source-timing.json"
        original_response = {"data": {"observations": [{"criterion_id": cid, "verdict": "FAIL"}]}}
        original_call = {"status": "completed", "request": {"video":video, "references":[]}, "response":original_response}
        original_path = self.folder / "source-call.json"
        original_path.write_text(json.dumps(original_call), encoding="utf-8")
        timing_record = {"video": video, "references": [], "context": {"shot_id":"G001"}, "rows": [timing[0]],
                         "call_path": str(original_path), "call_sha256": digest_json(original_call), "response":original_response}
        timing_path.write_text(json.dumps(timing_record), encoding="utf-8")
        context = {"shot_id": "G001", "main_audio": audio, "creative_audio_scope": {cid: scopes[cid]}, "review_duration_s": 1.,
                   "visual_timing_receipt": {"path": str(timing_path), "sha256": digest_json(timing_record)}}
        image = {"criterion_id": cid, "verdict": "PASS", "reason": "原片上确有屏幕行情变化。", "reference_ids": [],
                 "evidence": [{"asset_id": "V0", "start_s": .1, "end_s": .6, "observation": "屏幕行情正在变化。"}]}
        sound = {"criterion_id": cid, "verdict": "PASS", "reason": "对应时段实际听到市场下跌的播报。",
                 "evidence": [{"asset_id": "A0", "cue_id": "S001/1", "start_s": .1, "end_s": .6, "words": "市场正在下跌",
                               "observation": "听到市场下跌的新闻播报。"}]}
        def observations(row):
            return {"data": {"observations": [row]}, "model": "fixture", "request_id": "offline"}
        def result(im, au):
            return combine(observations(im), observations(au), context)["data"]["observations"][0]["verdict"]
        self.assertEqual(result({**image, "verdict": "FAIL", "visual_requirement": "财经主播露脸"}, sound), "UNKNOWN")
        self.assertEqual(result({**image, "verdict": "FAIL", "visual_requirement": "屏幕行情跳动"},
                                {**sound, "verdict": "UNKNOWN", "evidence": []}), "FAIL")
        self.assertEqual(result(image, {**sound, "evidence": []}), "UNKNOWN")
        self.assertEqual(result(image, {**sound, "evidence": [{**sound["evidence"][0], "start_s": .7, "end_s": .9}]}), "UNKNOWN")
        criteria = [{"criterion_id": cid, "kind": "creative_shot"}]
        reservations = []
        def respond(req):
            parts = json.loads(req.data)["messages"][0]["content"]
            self.assertEqual(base64.b64decode(parts[-1]["input_audio"]["data"]), self.wave.read_bytes())
            return reply("audio", [sound])
        with patch.object(BailianObserver, "_post", side_effect=respond) as post, \
             patch("pianchang.content_review.VisualBeatObserver.observe_video", return_value=observations(image)) as see:
            for _ in range(2):
                record = review_video(self.config, video, [], criteria, context, self.folder / "spoken",
                                      audiovisual=True, reserve=reservations.append)
                self.assertEqual(record["rows"][0]["status"], "PASS", record["rows"])
                self.assertEqual({e["asset_id"] for e in record["rows"][0]["evidence"]}, {"V0", "A0"})
            post.assert_called_once()
            see.assert_called_once()
        self.assertEqual(reservations, ["vision.review", "continuity.review"])
        cached = {"path":record["receipt_path"],"sha256":record["receipt_sha256"]}
        never_reserve = Mock(side_effect=AssertionError("local publication charged a call"))
        with patch.object(BailianObserver,"_post",side_effect=AssertionError("local publication sent a request")):
            saved = review_video(self.config,video,[],criteria,context,self.folder/"spoken",
                                 audiovisual=True,reserve=never_reserve,cached_receipt=cached)
            self.assertEqual(saved,record)
            wrong = self.folder/"changed-inputs"
            with self.assertRaisesRegex(ValueError,"指纹"):
                review_video(self.config,video,[],criteria,context,wrong,audiovisual=True,
                             reserve=never_reserve,cached_receipt=cached)
            self.assertFalse(wrong.exists())
            never_reserve.assert_not_called()
        failed_image = {**image, "verdict": "FAIL", "visual_requirement": "屏幕行情跳动",
                        "reason": "该时段屏幕行情一直静止，与要求的跳动相反。"}
        def limit(purpose):
            if purpose == "continuity.review":
                raise ValueError("原委托声音复核额度已用完")
        with patch("pianchang.content_review.VisualBeatObserver.observe_video", return_value=observations(failed_image)), \
             patch.object(BailianObserver, "_post", side_effect=AssertionError("unapproved request")):
            partial = review_video(self.config, video, [], criteria, context, self.folder / "no-audio-budget",
                                   audiovisual=True, reserve=limit)
            self.assertEqual(partial["execution_status"], "partial")
            self.assertEqual(partial["rows"][0]["status"], "FAIL")
            self.assertEqual(partial["rows"][0]["components"][1]["verdict"], "UNKNOWN")
        project, contract = {"shots": [shot]}, {"shot_id": "G001", "requirements": criteria}
        inspection = {"media_sha256": source["sha256"]}
        for receipt in (record, partial):
            evidence = {"version": VERSION, "project_sha256": digest_json(project), "contract_sha256": digest_json(contract),
                        "inspection_sha256": digest_json(inspection), "media_sha256": source["sha256"],
                        "selected_media": {"G001": source}, "rows": receipt["rows"],
                        "receipts": [context["visual_timing_receipt"], {"path": receipt["receipt_path"], "sha256": receipt["receipt_sha256"]}]}
            self.assertEqual(validate_content_evidence(project, contract, inspection, evidence)[cid], receipt["rows"][0])

    def test_voice_input_sends_raw_wav_and_no_bailian_parameters(self):
        route = SimpleNamespace(api_key="test-audio", base_url=self.audio.base_url, adapter="openai", model_id="audio")
        service = SimpleNamespace(text_config=self.config, model_routes=Mock(resolve=Mock(return_value=route)))
        rows = [{"criterion_id": "line", "verdict": "UNKNOWN", "reason": "offline test"}]
        with patch.object(BailianObserver, "_post", return_value=reply("audio", rows)) as post:
            actual, receipt = observe_voice(service, self.wave.read_bytes(), "audio/wav", {}, [], with_receipt=True)
        self.assertEqual(actual, rows)
        self.assertEqual(receipt["media_sha256"], file_sha256(self.wave))
        payload = json.loads(post.call_args.args[0].data)
        parts = payload["messages"][0]["content"]
        self.assertEqual([part["type"] for part in parts], ["text", "input_audio"])
        self.assertIn(OPENAI_LISTEN_FORMAT, parts[0]["text"])
        self.assertEqual(receipt["input_format"], OPENAI_LISTEN_FORMAT)
        audio = parts[1]["input_audio"]
        self.assertEqual(base64.b64decode(audio["data"], validate=True), self.wave.read_bytes())
        self.assertNotIn("reasoning_effort", payload)
        self.assertEqual(payload["modalities"], ["text"])

    def test_voice_requires_observation_and_does_not_regenerate_for_missing_evidence(self):
        route = SimpleNamespace(api_key="test", base_url=self.audio.base_url, adapter="openai", model_id="audio")
        service = SimpleNamespace(text_config=self.config, model_routes=Mock(resolve=Mock(return_value=route)))
        rows = [{"criterion_id":"voice", "verdict":"PASS", "reason":"clear recording"}]
        body, _ = reply("audio", rows)
        check = {"candidate":{"purpose":"voice"}, "checks":[{"path":"voice","actual":"missing input",
                 "expected":"listen to the recording","status":"UNKNOWN"}]}
        self.assertEqual(_repair_instructions(check), "")
        check["checks"][0].update(status="FAIL",actual="clipped audio")
        self.assertIn("clipped audio", _repair_instructions(check))
        with patch.object(BailianObserver, "_post", return_value=(body,None)):
            def listen():
                return observe_voice(service, self.wave.read_bytes(), "audio/wav", {}, {}, with_receipt=True)
            with self.assertRaisesRegex(ValueError,"需复听"):
                listen()
            heard = {"words":"", "sound":"silence across the whole fixture"}
            body["choices"][0]["message"]["content"] = json.dumps({"heard":heard, "observations":rows})
            body["usage"] = {"prompt_tokens_details":{"audio_tokens":0}}
            with self.assertRaisesRegex(ValueError,"需复听"):
                listen()
            body["usage"]["prompt_tokens_details"]["audio_tokens"] = 16
            _, receipt = listen()
            self.assertEqual(receipt["heard"], heard)

    def test_split_cut_uses_full_cadence_and_reserves_once_per_request(self):
        source = self.folder / "original.mp4"
        _ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=96x64:rate=10:duration=1", "-c:v", "libx264", str(source)])
        video = prepare_video(source, self.folder / "review", include_audio=False)
        audio = {"id": "A0", "path": str(self.wave), "sha256": file_sha256(self.wave)}
        context = {"cut_s": .5, "main_audio": audio, "creative_pair":["S001","S002"]}
        criteria = [{"criterion_id": "cut", "kind": "sequence_review"}]
        row = {"criterion_id": "cut", "verdict": "PASS", "reason": "Synthetic fixture response", "reference_ids": [],
               "evidence": [{"asset_id": "V0", "start_s": 0, "end_s": 1, "observation": "fixture image interval"}],
               "components": [{"name": name, "verdict": "PASS", "reason": "fixture visual observation"}
                              for name in ("identity","costume_props","space","action")]}
        sound = {"criterion_id": "cut", "verdict": "UNKNOWN", "reason": "Cannot resolve required audio",
                 "evidence": [{"asset_id": "A0", "start_s": 0, "end_s": 1, "observation": "fixture silence"}]}
        row["evidence"].append({"asset_id": "A0", "start_s": 0, "end_s": 1, "observation": "visual echo of listener"})
        sent, reservations = [], []
        def respond(req):
            payload = json.loads(req.data)
            sent.append(payload)
            self.assertEqual(len(sent), len(reservations))
            return reply(payload["model"], [sound if payload["model"] == "audio" else row])
        def interrupt_before_visual(purpose):
            if purpose == "vision.review":
                raise KeyboardInterrupt("simulate process interruption after the completed audio call")
            reservations.append(purpose)
        with patch.object(BailianObserver, "_post", side_effect=respond):
            with self.assertRaises(KeyboardInterrupt):
                review_video(self.config, video, [], criteria, context, self.folder / "calls",
                             audiovisual=True, reserve=interrupt_before_visual,
                             bound_cut={"mode":"new","folder":str(self.folder/"calls")})
            self.assertEqual(reservations, ["continuity.review"])
            with self.assertRaises(FileExistsError):
                review_video(self.config,video,[],criteria,context,self.folder/"calls",audiovisual=True,
                             reserve=reservations.append,bound_cut={"mode":"new","folder":str(self.folder/"calls")})
            with patch("pianchang.openai_review.merge_cut", side_effect=ValueError("legacy cut assembly error")):
                before = review_video(self.config, video, [], criteria, context, self.folder / "calls",
                                      audiovisual=True, reserve=reservations.append)
            prior_path = Path(before["receipt_path"])
            self.assertEqual(before["execution_status"], "execution_error")
            prior_bytes = prior_path.read_bytes()
            parts = {name:digest_json(json.loads(prior_path.with_name(name).read_bytes()))
                     for name in ("audio-call.json","visual-call.json")}
            recovery = {"folder":str(self.folder/"calls"),"mode":"assemble","parts":parts,
                        "receipt":{"path":str(prior_path),"sha256":before["receipt_sha256"]}}
            for _ in range(2):
                result = review_video(self.config, video, [], criteria, context, self.folder / "calls",
                                      audiovisual=True, reserve=reservations.append,bound_cut=recovery)
                self.assertEqual(result["rows"][0]["status"], "UNKNOWN")
                self.assertEqual(result["response"]["audio_review"]["request_id"], "offline-audio")
                merged = result["response"]["data"]["observations"][0]["evidence"]
                self.assertEqual(merged, [row["evidence"][0], *sound["evidence"]])
                self.assertEqual(result["normalized_from"]["path"], str(prior_path))
            self.assertEqual(prior_path.read_bytes(), prior_bytes)
            wrong = {**recovery,"parts":{**parts,"audio-call.json":"changed"}}
            with self.assertRaisesRegex(ValueError,"recovery_part"):
                review_video(self.config,video,[],criteria,context,self.folder/"calls",audiovisual=True,
                             reserve=reservations.append,bound_cut=wrong)
            cache = {**recovery,"mode":"cached","receipt":{"path":result["receipt_path"],"sha256":result["receipt_sha256"]}}
            unchanged = review_video(self.config,video,[],criteria,context,self.folder/"calls",audiovisual=True,
                                     reserve=reservations.append,bound_cut=cache)
            self.assertEqual(unchanged,result)
            orphan = self.folder/"interrupted-publication"
            interrupted = orphan/prior_path.parent.name
            interrupted.mkdir(parents=True)
            for name in ("audio-call.json","visual-call.json","call.json"):
                shutil.copy2(prior_path.with_name(name),interrupted/name)
            from_parts = review_video(self.config,video,[],criteria,context,orphan,audiovisual=True,reserve=reservations.append,
                                      bound_cut={"mode":"assemble_parts","folder":str(orphan),"parts":parts})
            self.assertEqual(from_parts["rows"],result["rows"])
            self.assertEqual(Path(from_parts["receipt_path"]),interrupted/"receipt-p4.json")
        self.assertEqual(reservations, ["continuity.review", "vision.review"])
        self.assertEqual(len(sent), 2)
        visuals = sent[1]["messages"][1]["content"]
        self.assertEqual(sum(v["type"] == "image_url" for v in visuals), len(video["frame_times_s"]))
        self.assertEqual(len(video["frame_times_s"]), 10)
        self.assertIn("cut_audio_observations", visuals[0]["text"])
        self.assertNotIn("enable_thinking", sent[1])
        self.assertIn("max_completion_tokens", sent[1])
        self.assertFalse(any(v["type"] in {"video_url", "input_audio"} for v in visuals))
        for v in visuals:
            if v["type"] == "image_url":
                self.assertTrue(base64.b64decode(v["image_url"]["url"].split(",", 1)[1]).startswith(bytes([255,216])))
        wire_audio = sent[0]["messages"][0]["content"][-1]["input_audio"]
        self.assertEqual(base64.b64decode(wire_audio["data"], validate=True), self.wave.read_bytes())

    def test_reserved_audio_without_visual_budget_never_passes_or_replays(self):
        audio = {"id": "A0", "path": str(self.wave), "sha256": file_sha256(self.wave)}
        video = {"id": "V0", "path": str(self.wave), "sha256": audio["sha256"], "duration_s": 1}
        criteria = [{"criterion_id": "cut", "kind": "sequence_review"}]
        row = {"criterion_id": "cut", "verdict": "PASS", "reason": "fixture audio observation",
               "evidence": [{"asset_id": "A0", "start_s": 0, "end_s": 1, "observation": "fixture waveform"}]}
        reserved = []
        def reserve(purpose):
            reserved.append(purpose)
            if purpose == "vision.review":
                raise ValueError("no visual budget")
        with patch.object(BailianObserver, "_post", return_value=reply("audio", [row])) as post:
            for _ in range(2):
                result = review_video(self.config, video, [], criteria, {"cut_s": .5, "main_audio": audio},
                                      self.folder / "cut", audiovisual=True, reserve=reserve)
                self.assertEqual(result["rows"][0]["status"], "UNKNOWN")
                self.assertEqual(result["execution_status"], "execution_error")
            post.assert_called_once()
        self.assertEqual(reserved, ["continuity.review", "vision.review", "vision.review"])
