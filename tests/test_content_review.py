"""Offline content review: synthetic clips and explicitly mocked provider observations."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pianchang.acceptance import file_sha256, required_subtitle
from pianchang.app_service import read_json
from pianchang.bailian_text import TextConfig, TextError
from pianchang.content_review import (internal_cut_windows, normalize_observations, review_video, run_content_reviews,
                            validate_content_evidence, VERSION, ContentObserver, cut_review_scope, prepare_cut)
from pianchang.story_spec import digest_json
from trace_system import Tracer, use_context, TraceError
from pianchang.video_collab_agent import VideoWorkspace
from pianchang.studio_recovery import prepare_continuation, read_incomplete_production, run_review_job


class ContentNormalizationTests(unittest.TestCase):
    def test_failed_creative_beat_can_locate_cut_only_with_ordered_video_evidence(self):
        left = {"criterion_id": "creative:S001", "status": "FAIL", "execution_status": "executed",
                "evidence": [{"asset_id": "V0", "start_s": 0, "end_s": 2.8}]}
        right = {"criterion_id": "creative:S002", "status": "PASS", "execution_status": "executed",
                 "evidence": [{"asset_id": "V0", "start_s": 2.9, "end_s": 8.9}]}
        def locate(a, b):
            return internal_cut_windows(("S001", "S002"), [a, b])
        self.assertEqual(locate(left, right), ((0, 2.8), (2.9, 8.9)))
        self.assertIsNone(locate({**left, "execution_status": "evidence_invalid"}, right))
        self.assertIsNone(locate({**left, "evidence": [{"asset_id": "R0"}]}, right))
        self.assertIsNone(locate(left, {**right, "evidence": [{"asset_id": "V0", "start_s": 2, "end_s": 5}]}))

    def test_required_subtitle_is_bound_to_literal_script_without_guessing(self):
        row=required_subtitle({"description":"角色转身，画面切黑，字幕浮现：当晚她删掉歌单。"})
        self.assertEqual(row["expected_text"],"当晚她删掉歌单。")
        self.assertTrue(row["required"])
        self.assertEqual(required_subtitle({"description":"字幕出现：“离线字幕原文” 后续说明"})["expected_text"],"离线字幕原文")
        self.assertIsNone(required_subtitle({"description":"角色谈论字幕，但没有指定上屏内容。"}))

    def test_model_cannot_exempt_missing_subtitle_as_postproduction(self):
        criterion=required_subtitle({"description":"字幕浮现：离线测试字幕。"})
        row={**copy.deepcopy(self.row),"criterion_id":"script_subtitle","observed_text":"",
             "reason":"离线模拟：全片没有字幕，模型错误地认为字幕属于后期所以通过。"}
        checked=self.normalize(row,criterion)
        self.assertEqual(checked["status"],"FAIL")
        self.assertEqual(checked["model_verdict"],"PASS")
        self.assertEqual(checked["observed_text"],"")
        self.assertEqual(row["verdict"],"PASS")
        row.pop("observed_text")
        self.assertEqual(self.normalize(row,criterion)["status"],"UNKNOWN")

    def test_subtitle_requires_exact_observed_text_and_actual_time_evidence(self):
        criterion=required_subtitle({"description":"字幕浮现：离线测试字幕。"})
        row={**copy.deepcopy(self.row),"criterion_id":"script_subtitle","observed_text":"离线测试字幕。"}
        row["evidence"][0].update(start_s=7,end_s=8)
        self.assertEqual(self.normalize(row,criterion)["status"],"PASS")
        row["observed_text"]="离线错字字幕。"
        self.assertEqual(self.normalize(row,criterion)["status"],"FAIL")
        row["observed_text"]=""
        self.assertEqual(self.normalize(row,criterion)["execution_status"],"evidence_invalid")

    def test_known_wrong_render_input_cannot_be_hidden_by_model_copying_expected_text(self):
        criterion=required_subtitle({"description":"字幕浮现：离线测试字幕。"})
        row={**copy.deepcopy(self.row),"criterion_id":"script_subtitle","observed_text":"离线测试字幕。"}
        receipt=self.root/"offline-render.json";receipt.write_text('{"offline":true}',encoding="utf8")
        context={"subtitle_rendering":{"text":"离线测试字幕","receipt":{"path":str(receipt),"sha256":file_sha256(receipt)}}}
        result=self.normalize(row,criterion,context=context)
        self.assertEqual(result["status"],"FAIL");self.assertEqual(result["model_verdict"],"PASS")
        self.assertEqual(result["rendered_text"],"离线测试字幕")
        row.pop("observed_text")
        self.assertEqual(self.normalize(row,criterion,context=context)["status"],"FAIL")

    def test_validation_batch_preserves_each_field_and_storage_failure_prevents_return(self):
        def checks(*args, _check, **kwargs):
            _check("offline.hash","expected","expected")
            _check("offline.binding",True,True)
            return {"offline":"bound"}
        with patch("pianchang.content_review._validate_content_evidence",side_effect=checks),patch("pianchang.content_review.event") as persisted:
            self.assertEqual(validate_content_evidence({}, {}, {}, {}),{"offline":"bound"})
        data=persisted.call_args.args[1]
        self.assertEqual([r["path"] for r in data["checks"]],["offline.hash","offline.binding"])
        self.assertTrue(data["complete"])
        with patch("pianchang.content_review._validate_content_evidence",side_effect=checks),\
             patch("pianchang.content_review.event",side_effect=TraceError("offline unavailable trace storage")):
            with self.assertRaises(TraceError):validate_content_evidence({}, {}, {}, {})

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.enterContext(use_context(None, tracer=Tracer(self.root / "offline-traces")))
        self.enterContext(__import__("trace_system").trace("offline.content_review"))
        self.media = self.root / "offline.mp4"
        self.media.write_bytes(b"offline bytes for evidence shape tests only")
        self.video = {"path": str(self.media), "sha256": file_sha256(self.media), "duration_s": 8,
                      "fps": 10, "scope": "offline synthetic timestamps"}
        self.criterion = {"criterion_id": "action_sequence", "kind": "continuous_video"}
        self.row = {"criterion_id": "action_sequence", "verdict": "PASS", "reason": "离线模拟：从开始到结束的动作证据在所引用区间内。",
                    "evidence": [{"asset_id": "V0", "start_s": 0, "end_s": 8, "observation": "离线模拟完整时间范围。"}],
                    "reference_ids": [], "limitations": [], "components": []}

    def normalize(self, row, criterion=None, refs=None, context=None):
        return normalize_observations({"data": {"observations": [row]}, "model": "offline-fixture", "request_id": "offline"},
            self.video, refs or [], [criterion or self.criterion], context or {})[0]

    def test_time_bounds_full_coverage_and_missing_identity_reference_are_enforced(self):
        self.assertEqual(self.normalize(self.row)["status"], "PASS")
        cropped = copy.deepcopy(self.row)
        cropped["evidence"][0].update(start_s=2, end_s=4)
        self.assertEqual(self.normalize(cropped)["execution_status"], "evidence_invalid")
        for bad in (-1, float("nan"), "1", True):
            invalid = copy.deepcopy(self.row)
            invalid["evidence"][0]["start_s"] = bad
            self.assertEqual(self.normalize(invalid)["status"], "UNKNOWN")
        identity = {**self.row, "criterion_id": "character_identity"}
        checked = self.normalize(identity, {"criterion_id": "character_identity", "kind": "reference_comparison"})
        self.assertEqual(checked["status"], "UNKNOWN")
        self.assertIn("指定参考图", checked["reason"])

    def test_cut_uses_both_sides_and_all_components_including_audio(self):
        row = {**self.row, "criterion_id": "continuity:L->R",
               "components": [{"name": name, "verdict": "PASS", "reason": "离线模拟具体比较依据。"}
                              for name in ("identity", "costume_props", "space", "action", "audio")]}
        criterion = {"criterion_id": row["criterion_id"], "kind": "sequence_review"}
        self.assertEqual(self.normalize(row, criterion, context={"cut_s": 4})["status"], "PASS")
        row["components"][-1].update(verdict="FAIL", reason="离线模拟：切点截断说话声。")
        self.assertEqual(self.normalize(row, criterion, context={"cut_s": 4})["status"], "FAIL")
        row["components"].pop()
        self.assertEqual(self.normalize(row, criterion, context={"cut_s": 4})["status"], "UNKNOWN")

    def test_state_fields_cannot_be_omitted_and_provider_response_is_not_mutated(self):
        fields = [{"phase": phase, "path": "char:offline.eye_contact", "expected": True} for phase in ("start", "end")]
        criterion = {"criterion_id": "start_end_state", "kind": "continuous_video", "state_fields": fields}
        row = {**copy.deepcopy(self.row), "criterion_id": "start_end_state"}
        self.assertEqual(self.normalize(row, criterion)["status"], "UNKNOWN")
        row["state_checks"] = [{"phase": f["phase"], "path": f["path"], "expected": True, "actual": False,
                               "verdict": "PASS", "reason": "离线模拟：实际可见没有对视。",
                               "start_s": 0 if f["phase"] == "start" else 7.8,
                               "end_s": .2 if f["phase"] == "start" else 8} for f in fields]
        checked = self.normalize(row, criterion)
        self.assertEqual(checked["status"], "FAIL")
        self.assertTrue(all(s["verdict"] == "PASS" for s in row["state_checks"]))
        for f in fields:
            f["path"] = "char:offline.knows_misunderstanding"
        for check in row["state_checks"]:
            check.update(path="char:offline.knows_misunderstanding", actual=True, verdict="PASS",
                         reason="离线模拟：表情平静，所以知道了。")
        checked = self.normalize(row, criterion)
        self.assertEqual(checked["execution_status"], "inconclusive")
        self.assertTrue(all(s["verdict"] == "UNKNOWN" for s in checked["state_checks"]))

    def test_cut_rejects_borrowed_next_shot_actions_and_preserves_supported_failure(self):
        left = {"id": "L", "script_scene_id": "scene1", "description": "女生出现，男生立刻挺直肩膀。"}
        right = {"id": "R", "script_scene_id": "scene1", "description": "两人同时点头，擦肩而过。"}
        segments = [{"shot_id": "L", "from_s": 4}, {"shot_id": "R", "from_s": 0}]
        scope = cut_review_scope(left, right, segments)
        row = {**copy.deepcopy(self.row), "criterion_id": "continuity:L->R", "verdict": "FAIL",
               "components": [{"name": n, "verdict": "PASS", "reason": "离线模拟：此分项无实际矛盾。"}
                              for n in ("identity", "costume_props", "space", "action", "audio")]}
        action = row["components"][3]
        action.update(verdict="FAIL", reason="离线模拟错误：要求左镜头先完成右镜头的点头。",
                      failure_basis={"source": "left_shot.description", "quote": "两人同时点头", "observed_side": "left"})
        criterion = {"criterion_id": row["criterion_id"], "kind": "sequence_review"}
        context = {"cut_s": 4, "cut_review_scope": scope}
        self.assertEqual(self.normalize(row, criterion, context=context)["execution_status"], "evidence_invalid")
        self.assertEqual(action["verdict"], "FAIL")  # Never rewrite the received observation.
        action.update(reason="离线模拟：右侧实际有明确相反动作，引用右侧要求。",
                      failure_basis={"source": "right_shot.description", "quote": "两人同时点头", "observed_side": "right"})
        self.assertEqual(self.normalize(row, criterion, context=context)["status"], "FAIL")
        # A bad assertion in one component must not hide another component's
        # supported failure behind an overall UNKNOWN.
        action["failure_basis"].update(source="left_shot.description", quote="两人同时点头", observed_side="left")
        row["components"][2].update(verdict="FAIL", reason="离线模拟：切点两侧站位已明确重置。",
                                   failure_basis={"source": "same_scene_cut", "quote": "同场连续动作", "observed_side": "both"})
        checked = self.normalize(row, criterion, context=context)
        self.assertEqual(checked["status"], "FAIL")
        self.assertEqual(checked["execution_status"], "executed")
        self.assertEqual(checked["components"][2]["verdict"], "FAIL")
        self.assertEqual(checked["components"][3]["verdict"], "UNKNOWN")
        self.assertEqual(action["verdict"], "FAIL")
        self.assertTrue(checked["rejected_claims"])
        identity = row["components"][0]
        identity.update(verdict="FAIL", reason="离线模拟：右侧人物与已提交参考明确不同。",
                        failure_basis={"source": "approved_reference", "quote": "R0（离线角色肖像）", "observed_side": "right"})
        row["reference_ids"] = ["R0"]
        reference = {"id": "R0", "path": str(self.media), "sha256": self.video["sha256"],
                     "source_sha256": self.video["sha256"], "pin": {"purpose": "portrait"}}
        referenced = self.normalize(row, criterion, context=context, refs=[reference])
        self.assertEqual(referenced["components"][0]["verdict"], "FAIL")
        identity["failure_basis"]["quote"] = "R999（未提交的参考）"
        self.assertEqual(self.normalize(row, criterion, context=context, refs=[reference])["components"][0]["verdict"], "UNKNOWN")
        with self.assertRaises(ValueError):
            cut_review_scope(left, right, list(reversed(segments)))

    def test_cross_scene_pose_change_and_silence_do_not_create_new_requirements(self):
        scope = cut_review_scope({"id": "L", "script_scene_id": "a", "description": "两人擦肩而过。"},
            {"id": "R", "script_scene_id": "b", "description": "另一女生递还书本，男生道谢。"},
            [{"shot_id": "L"}, {"shot_id": "R"}])
        row = {**copy.deepcopy(self.row), "criterion_id": "continuity:L->R", "verdict": "FAIL",
               "components": [{"name": n, "verdict": "PASS", "reason": "离线模拟：此分项无实际矛盾。"}
                              for n in ("identity", "costume_props", "space", "action", "audio")]}
        criterion = {"criterion_id": row["criterion_id"], "kind": "sequence_review"}
        context = {"cut_s": 4, "cut_review_scope": scope, "main_audio": {"path": str(self.media)}}
        row["components"][2].update(verdict="FAIL", failure_basis={"source": "same_scene_cut", "quote": "站位必须连续", "observed_side": "both"})
        self.assertEqual(self.normalize(row, criterion, context=context)["execution_status"], "evidence_invalid")
        row["components"][2]["verdict"] = "PASS"
        audio = row["components"][4]
        audio.update(verdict="FAIL", failure_basis={"source": "audio_observation", "quote": "跨场由音乐切为静音", "observed_side": "both", "violation_type": "unexplained_gap_same_scene"})
        self.assertEqual(self.normalize(row, criterion, context=context)["execution_status"], "evidence_invalid")
        audio["failure_basis"].update(violation_type="truncated_speech", quote="离线模拟：实际台词在切点被截断")
        self.assertEqual(self.normalize(row, criterion, context=context)["status"], "FAIL")

    def test_unconfirmed_paid_call_is_not_replayed(self):
        client = Mock()
        client.observe_video.side_effect = TextError("offline timeout, provider outcome unknown")
        first = review_video(TextConfig(), self.video, [], [self.criterion], {}, self.root / "cache", client=client)
        second = review_video(TextConfig(), self.video, [], [self.criterion], {}, self.root / "cache", client=client)
        self.assertEqual(client.observe_video.call_count, 1)
        self.assertEqual(first["rows"], second["rows"])
        self.assertEqual(first["execution_status"], "execution_error")

    def test_adjacent_review_keeps_actions_before_old_tail_crop_and_the_whole_next_shot(self):
        # Explicit offline subprocess fixture. The regression concerns coverage
        # and time binding; these bytes are never sent as real video to a model.
        left, right = self.root / "offline-left.mp4", self.root / "offline-right.mp4"
        left.write_bytes(b"offline source: action at 0.9-1.4s, total 8s")
        right.write_bytes(b"offline source: entire next shot, total 7s")
        clips = {"L": {"media_path": str(left)}, "R": {"media_path": str(right)}}
        calls = []
        def ffmpeg(arguments):
            calls.append(arguments)
            Path(arguments[-1]).write_bytes(b"explicit offline media fixture")
        def exported(items, directory):
            directory = Path(directory); directory.mkdir(parents=True)
            target = directory / "offline.mp4"; target.write_bytes(b"offline combined fixture")
            manifest = directory / "manifest.json"
            manifest.write_text(json.dumps({"clips": [{"preview_duration_s": 8}, {"preview_duration_s": 7}]}), encoding="utf8")
            return {"manifest_path": str(manifest), "preview_path": str(target), "preview_sha256": file_sha256(target)}
        def video(source, directory, **kwargs):
            return {"path": source, "duration_s": 15, "sha256": file_sha256(source)}
        with patch("pianchang.content_review._probe", side_effect=[
                {"format": {"duration": "8"}, "streams": [{"codec_type": "video"}]},
                {"format": {"duration": "7"}, "streams": [{"codec_type": "video"}]}]), \
                patch("pianchang.content_review._ffmpeg", side_effect=ffmpeg), \
                patch("pianchang.content_review.export_preview", side_effect=exported), \
                patch("pianchang.content_review.prepare_video", side_effect=video), \
                patch("pianchang.collab_cut_review._audio_levels", return_value={"offline": True}):
            _, context = prepare_cut({}, clips, "L", "R", self.root / "cut")
        self.assertEqual(context["cut_s"], 8)
        segments = context["source_segments"]
        self.assertEqual([(s["from_s"], s["duration_s"]) for s in segments], [(0, 8), (0, 7)])
        self.assertTrue(all(s["scope"] == "whole_source_clip" for s in segments))
        self.assertEqual([a[a.index("-t") + 1] for a in calls if "-t" in a], ["8.0", "7.0"])

    def test_prior_receipt_is_renormalized_without_replaying_the_paid_observation(self):
        client = Mock(system_prompt="explicit offline migration fixture")
        client.observe_video.return_value = {"data": {"observations": [copy.deepcopy(self.row)]},
                                            "model": "offline", "request_id": "offline-migration"}
        directory = self.root / "migration"
        first = review_video(TextConfig(), self.video, [], [self.criterion], {}, directory, client=client)
        previous = Path(first["receipt_path"]).with_name("receipt-p3.json")
        Path(first["receipt_path"]).replace(previous)
        original_bytes = previous.read_bytes()
        second = review_video(TextConfig(), self.video, [], [self.criterion], {}, directory, client=client)
        self.assertEqual(client.observe_video.call_count, 1)
        self.assertEqual(previous.read_bytes(), original_bytes)
        self.assertEqual(second["normalized_from"]["path"], str(previous))
        self.assertNotEqual(second["receipt_path"], str(previous))

    def test_actual_payload_contains_video_and_voice_reference_with_thinking_disabled(self):
        import wave
        audio = self.root / "offline.wav"
        with wave.open(str(audio), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(b"\0\0" * 1600)
        refs = [{"id": "R0", "path": str(audio), "sha256": file_sha256(audio),
                 "pin": {"purpose": "voice", "subject_id": "char:offline"}}]
        captured = []
        body = {"id": "offline-provider", "model": "offline-omni", "usage": {"total_tokens": 1},
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"observations": [self.row]})}}]}
        client = ContentObserver(TextConfig(api_key="sk-offline-transport-test", model="offline-omni"))
        with patch.object(client, "_post", side_effect=lambda req: (captured.append(json.loads(req.data)) or body, "offline-header")):
            client.observe_video({**self.video, "id": "V0"}, refs, [self.criterion], {}, audiovisual=True)
        payload = captured[0]
        self.assertEqual(payload["reasoning_effort"], "none")
        content = payload["messages"][1]["content"]
        self.assertTrue(any(p["type"] == "input_audio" for p in content))
        self.assertTrue(any(p["type"] == "video_url" and p["fps"] == 10 for p in content))


class ContentPipelineTests(unittest.TestCase):
    def test_real_offline_clips_and_cut_are_bound_cached_and_old_reports_retained(self):
        from tests.test_studio_recovery import StudioRecoveryTests
        fixture = StudioRecoveryTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        service = fixture.service
        service.vision_config = None
        snapshot = read_incomplete_production(fixture.view)
        pointer = {"request_id": snapshot["request_id"], "sha256": snapshot["sha256"]}
        job = {**fixture.job, "kind": "studio_review", "id": "6"*32}
        basic = run_review_job(service, job, {"continuation": pointer})
        original_files = {sid: Path(r["report_path"]).read_bytes() for sid, r in basic["reports"].items()}
        checkpoint = prepare_continuation(fixture.view, pointer)
        workspace = VideoWorkspace(script=fixture.project["script_source"]["script"], script_ref=fixture.view["script_ref"],
            directory=fixture.work, text_config=None, video_config=service.video_config, template_project=fixture.project,
            previous=None, decision_client=None, vision_config=TextConfig(model="offline-fixture"), progress=None,
            reference_store=fixture.session.store, story_id=fixture.view["story_id"])
        workspace.project, workspace.shots, workspace.reports = fixture.view["project"], checkpoint["shots"], basic["reports"]
        calls = []
        def offline_observer(_client, video, references, criteria, context, audiovisual=False):
            calls.append((criteria[0]["criterion_id"], audiovisual))
            return {"model": "offline-content-fixture", "request_id": "offline-" + str(len(calls)),
                "data": {"observations": [{"criterion_id": c["criterion_id"], "verdict": "PASS",
                    "reason": "离线合成色块片段检查，仅用于验证证据绑定与流程。",
                    "evidence": [{"asset_id": "V0", "start_s": 0, "end_s": video["duration_s"], "observation": "离线合成色块覆盖整个片段。"}],
                    "reference_ids": [r["id"] for r in references], "limitations": [], "actual_value": c.get("expected"),
                    "components": [{"name": name, "verdict": "PASS", "reason": "离线模拟切点比较依据。"} for name in
                                   ("identity", "costume_props", "space", "action", "audio")] if audiovisual else [],
                    "state_checks": [{"phase": f["phase"], "path": f["path"], "expected": f["expected"],
                        "actual": f["expected"], "verdict": "PASS", "reason": "离线模拟端点字段直接可见。",
                        "start_s": 0 if f["phase"] == "start" else max(0, video["duration_s"]-.2),
                        "end_s": min(.2, video["duration_s"]) if f["phase"] == "start" else video["duration_s"]}
                        for f in (c.get("state_fields", []) if context.get("shot_id") != "S002" else [])]}
                    for c in criteria]}}
        with patch("pianchang.content_review.ContentObserver.observe_video", offline_observer):
            run_content_reviews(workspace)
            first_paths = {sid: r["report_path"] for sid, r in workspace.reports.items()}
            run_content_reviews(workspace)
        self.assertEqual(len(calls), 6, calls)
        self.assertTrue(calls[-1][1])
        self.assertEqual(first_paths, {sid: r["report_path"] for sid, r in workspace.reports.items()})
        continuation = copy.copy(workspace)
        continuation.directory = fixture.work.parent / ("video-spec-" + "4"*32)
        continuation.shots = {sid: {**clip, "operation": "reused"} for sid, clip in workspace.shots.items()}
        continuation.reports = copy.deepcopy(workspace.reports)
        continuation.partial, continuation.shooting_plan = checkpoint, {"continuation": pointer}
        with patch("pianchang.content_review.ContentObserver.observe_video", side_effect=AssertionError("offline: unchanged media must reuse observed evidence")):
            run_content_reviews(continuation)
        # Native evidence for the same bytes remains reusable even when the
        # candidate was produced by a prior repair, rather than an initial take.
        native = continuation.shots["S001"]
        continuation.shots = {"S001": {**native, "repair_id":"offline-prior-repair"}}
        continuation.reports = {"S001":continuation.reports["S001"]}
        with patch("pianchang.content_review.ContentObserver.observe_video", side_effect=AssertionError("offline: same repaired media must not be re-observed")):
            run_content_reviews(continuation)
        for sid, raw in original_files.items():
            old = fixture.work / "reviews" / digest_json(sid)[:16] / "report.json"
            self.assertEqual(old.read_bytes(), raw)
            report = read_json(workspace.reports[sid]["report_path"])
            self.assertEqual(report["content_review_version"], VERSION)
            self.assertEqual(report["results"][0]["status"], "PASS", report["results"][0])
            self.assertTrue(any(r.get("execution_status") == "blocked_missing_media" for r in report["results"]), report["results"])
        report_path = Path(workspace.reports["S001"]["report_path"])
        evidence = read_json(report_path.parent / "content.json")
        contract = read_json(report_path.parent / "contract.json")
        inspection = read_json(workspace.reports["S001"]["inspection_path"])
        self.assertTrue(validate_content_evidence(fixture.project, contract, inspection, evidence))
        receipt_path = Path(evidence["receipts"][0]["path"])
        receipt = read_json(receipt_path)
        receipt["rows"][0]["status"] = "FAIL"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        with self.assertRaises(ValueError):
            validate_content_evidence(fixture.project, contract, inspection, evidence)


if __name__ == "__main__":
    unittest.main()
