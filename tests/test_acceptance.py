"""Offline acceptance tests with fabricated media and declared tool results.

No test decodes these fake files or calls a model. Inspection records model the
trusted local inspection tool's output; they do not constitute real media QA.
"""
import copy
import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from pianchang.acceptance import (AcceptanceError, create_contract, file_sha256,
                        regression_plan, run_checks, validate_contract)
from pianchang.story_spec import digest_json


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        fixture = Path(__file__).resolve().parents[1] / "fixtures" / "story_project.json"
        self.project = json.loads(fixture.read_text(encoding="utf-8"))
        self.contract = create_contract(self.project, "S002")
        self.media = self.root / "candidate.mp4"
        self.media.write_bytes(b"fabricated-mp4-bytes-for-offline-tests")
        frames = []
        for index, moment in enumerate((0.0, 3.5, 7.9), 1):
            path = self.root / f"frame-{index}.jpg"
            path.write_bytes(f"fabricated-jpeg-frame-{index}".encode("ascii"))
            frames.append({"id": f"F{index:03}", "time_s": moment,
                           "path": str(path), "sha256": file_sha256(path)})
        self.inspection = {
            "schema_version": "media-inspection/1", "media_path": str(self.media),
            "media_sha256": file_sha256(self.media), "frames": frames,
            "metadata": {"duration_s": 8.0, "width": 720, "height": 1280},
            "decode": {"status": "PASS", "returncode": 0,
                       "method": "mocked_full_decode"},
        }

    def observations(self, *, contract=None, inspection=None):
        contract = self.contract if contract is None else contract
        inspection = self.inspection if inspection is None else inspection
        items = [{"criterion_id": requirement["criterion_id"], "verdict": "PASS",
                  "frame_ids": ["F001", "F003"],
                  "reason": "离线模拟观察：仅这些引用帧中的可见外观符合所述条件。"}
                 for requirement in contract["requirements"] if requirement["kind"] == "sampled_visual"]
        return {"schema_version": "sampled-observations/1",
                "method": "vl_on_timestamped_still_frames", "capabilities": ["sampled_visual"],
                "contract_sha256": digest_json(contract),
                "inspection_sha256": digest_json(inspection),
                "media_sha256": inspection["media_sha256"],
                "response": {"data": {"observations": items}, "model": "offline-fake-vl"}}

    def job(self):
        identity = {"base_url": "https://dashscope.aliyuncs.com/api/v1",
                    "request": {"model": "offline-fake-model", "input": {"prompt": "offline fixture"},
                                "parameters": {"duration": 8, "size": "720*1280"}},
                    "metadata": {"project_sha256": self.contract["project_sha256"], "shot_id": "S002",
                                 "input_versions": copy.deepcopy(self.contract["input_versions"])}}
        return {"schema_version": "bailian-video-job/1", "status": "SUCCEEDED",
                "task_id": "offline-task-id", **identity,
                "request_sha256": digest_json(identity),
                "download_sha256": self.inspection["media_sha256"]}

    def check(self, observations=None, *, project=None, contract=None, inspection=None, job=None):
        return run_checks(self.project if project is None else project,
                          self.contract if contract is None else contract,
                          self.inspection if inspection is None else inspection, observations, job=job)

    @staticmethod
    def result(report, criterion_id):
        return next(r for r in report["results"] if r["criterion_id"] == criterion_id)

    def assert_integrity_failure(self, report):
        self.assertEqual(self.result(report, "evidence_integrity")["status"], "FAIL")
        self.assertEqual(report["status"], "FAIL")
        self.assertFalse(report["task_complete"])
        self.assertTrue(all(r["status"] == "UNKNOWN" for r in report["results"]
                            if r["criterion_id"] != "evidence_integrity"))

    def test_required_unknown_blocks_completion_despite_technical_and_sampled_passes(self):
        report = self.check(self.observations())
        for cid in ("file_decode", "duration", "resolution", "sampled_prop_appearance"):
            self.assertEqual(self.result(report, cid)["status"], "PASS")
        for cid in ("character_identity", "scene_geometry", "action_sequence", "start_end_state"):
            self.assertEqual(self.result(report, cid)["status"], "UNKNOWN")
        self.assertEqual(report["status"], "UNKNOWN")
        self.assertFalse(report["task_complete"])
        self.assertEqual(report["creative_review"], "UNKNOWN")
        self.assertEqual(report["audio_content_review"], "UNKNOWN")

    def test_missing_model_observations_do_not_disappear_from_results(self):
        report = self.check()
        expected = {r["criterion_id"] for r in self.contract["requirements"]}
        self.assertEqual({r["criterion_id"] for r in report["results"]},
                         expected | {"evidence_integrity", "candidate_binding"})
        self.assertEqual(self.result(report, "sampled_prop_appearance")["status"], "UNKNOWN")
        self.assertFalse(report["task_complete"])

    def test_technical_duration_resolution_and_decode_failures_block_acceptance(self):
        for cid, field, value in (("duration", "duration_s", 10.0),
                                  ("resolution", "height", 720),
                                  ("file_decode", "decode", {"status": "FAIL", "error": "corrupt stream"})):
            with self.subTest(criterion=cid):
                inspection = copy.deepcopy(self.inspection)
                if field == "decode":
                    inspection["decode"] = value
                else:
                    inspection["metadata"][field] = value
                report = self.check(inspection=inspection)
                self.assertEqual(self.result(report, cid)["status"], "FAIL")
                self.assertEqual(report["status"], "FAIL")
                self.assertFalse(report["task_complete"])

    def test_missing_decode_tool_result_is_unknown_not_pass(self):
        self.inspection["decode"] = {"status": "UNKNOWN", "error": "ffmpeg unavailable"}
        report = self.check()
        self.assertEqual(self.result(report, "file_decode")["status"], "UNKNOWN")
        self.assertFalse(report["task_complete"])

    def test_missing_generation_job_is_a_required_unknown(self):
        report = self.check(self.observations())
        result = self.result(report, "candidate_binding")
        self.assertTrue(result["required"])
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertFalse(report["task_complete"])

    def test_valid_generation_job_binds_candidate_but_does_not_prove_its_content(self):
        report = self.check(self.observations(), job=self.job())
        self.assertEqual(self.result(report, "candidate_binding")["status"], "PASS")
        self.assertEqual(self.result(report, "action_sequence")["status"], "UNKNOWN")
        self.assertFalse(report["task_complete"])

    def pinned_candidate(self, *, voice=False):
        from pianchang.bailian_video import BailianVideoClient, VideoConfig, build_request
        from tests.test_bailian_video import Response
        from pianchang.video_agent import VideoRunner
        raw = b"offline reference adapter bytes"
        pin = {"asset_id":"portrait-lin", "revision_id":"r3", "sha256":"a"*64,
               "media_sha256":hashlib.sha256(raw).hexdigest(), "subject_id":"char:lin", "purpose":"portrait"}
        ref = {**pin,"mime_type":"image/jpeg","width":240,"height":320,"bytes":len(raw),
               "provider_url":"data:image/jpeg;base64," + base64.b64encode(raw).decode()}
        config = VideoConfig(api_key="offline-acceptance",model="wan2.7-r2v",output_dir=self.root / "jobs")
        pins, refs = [pin], [ref]
        if voice:
            from tests.test_video_reference_receipt import offline_voice_upload
            media = b"offline timbre"
            voice_pin = {**pin, "asset_id":"voice-lin", "purpose":"voice", "sha256":"b"*64,
                         "media_sha256":hashlib.sha256(media).hexdigest()}
            pins.append(voice_pin)
            refs.append(offline_voice_upload(config,{**voice_pin, "mime_type":"audio/wav", "bytes":len(media),
                                                      "duration_s":2}))
        self.project["shots"][1]["required_refs"] = pins
        self.project["reference_story_id"] = "offline-story"
        self.contract = create_contract(self.project, "S002")
        client = BailianVideoClient(config)
        client._api_opener = Mock()
        response = Response(b'{"output":{"task_id":"offline-accepted","task_status":"PENDING"}}')
        response.status = 200
        client._api_opener.open.return_value = response
        job = VideoRunner(config,client).submit(build_request(config,"测试",resolved_refs=refs),{
            "required_refs":pins,"reference_story_id":"offline-story","shot_id":"S002",
            "project_sha256":self.contract["project_sha256"],"input_versions":self.contract["input_versions"]
        }, resolved_refs=refs)
        job.update(status="SUCCEEDED",download_sha256=self.inspection["media_sha256"])
        return job

    def test_pinned_shot_checks_provider_accepted_media_input(self):
        job = self.pinned_candidate()
        report = self.check(job=job)
        self.assertEqual(self.result(report,"reference_submission")["status"],"PASS")
        self.assertEqual(self.result(report,"character_identity")["status"],"UNKNOWN")
        reduced = copy.deepcopy(self.contract)
        reduced["requirements"] = [r for r in reduced["requirements"] if r["criterion_id"] != "reference_submission"]
        self.assertEqual(self.result(self.check(contract=reduced,job=job),"evidence_integrity")["status"],"FAIL")

    def test_reuse_checks_original_reference_job_without_claiming_current_generation(self):
        job = self.pinned_candidate()
        self.project["title"] += "新版工程"
        contract = create_contract(self.project,"S002")
        report = run_checks(self.project,contract,self.inspection,reference_job=job)
        self.assertEqual(self.result(report,"reference_submission")["status"],"PASS")
        self.assertEqual(self.result(report,"candidate_binding")["status"],"UNKNOWN")
        self.assertEqual(report["reference_job_sha256"],digest_json(job))
        self.project["shots"][1]["required_refs"][0]["revision_id"] = "r4"
        report = run_checks(self.project,create_contract(self.project,"S002"),self.inspection,reference_job=job)
        self.assertEqual(self.result(report,"reference_submission")["status"],"FAIL")

    def test_voice_acceptance_requires_verified_upload_trace(self):
        job = self.pinned_candidate(voice=True)
        self.assertEqual(self.result(self.check(job=job),"reference_submission")["status"],"PASS")
        source = job["reference_receipt"]["voice_uploads"][0]
        actual_trace = source["trace_id"]
        source["trace_id"] = "9" * 32
        self.assertEqual(self.result(self.check(job=job),"reference_submission")["status"],"UNKNOWN")
        source["trace_id"] = actual_trace
        source["bytes"] += 1
        self.assertEqual(self.result(self.check(job=job),"reference_submission")["status"],"FAIL")

    def test_reference_absence_unknown_and_conflicting_evidence_fails(self):
        from trace_system import Tracer, use_context
        job = self.pinned_candidate()
        self.assertEqual(self.result(self.check(),"reference_submission")["status"],"UNKNOWN")
        missing = {k:v for k,v in job.items() if k != "reference_receipt"}
        self.assertEqual(self.result(self.check(job=missing),"reference_submission")["status"],"UNKNOWN")
        with use_context(None,tracer=Tracer(self.root / "offline-missing-original-traces")):
            self.assertEqual(self.result(self.check(job=job),"reference_submission")["status"],"UNKNOWN")
        for tamper in ("file", "receipt"):
            altered = copy.deepcopy(job)
            if tamper == "file":
                altered["download_sha256"] = "c"*64
            else:
                altered["reference_receipt"]["submitted_refs"] = []
            with self.subTest(tamper=tamper):
                result = self.result(self.check(job=altered),"reference_submission")
                self.assertEqual(result["status"],"FAIL")
                self.assertTrue(any(row.get("status") == "FAIL" for row in result["evidence"]))

    def test_wrong_shot_project_or_versions_in_generation_job_fail_binding(self):
        for field, value in (("shot_id", "S001"), ("project_sha256", "0" * 64),
                             ("input_versions", {"script": 999})):
            with self.subTest(field=field):
                job = self.job()
                job["metadata"][field] = value
                job["request_sha256"] = digest_json({k: job[k] for k in ("base_url", "request", "metadata")})
                report = self.check(self.observations(), job=job)
                self.assertEqual(self.result(report, "candidate_binding")["status"], "FAIL")
                self.assertFalse(report["task_complete"])

    def test_different_candidate_cannot_reuse_another_generation_job(self):
        job = self.job()
        candidate = self.root / "other-candidate.mp4"
        candidate.write_bytes(b"different video with fresh inspection and fresh observations")
        inspection = copy.deepcopy(self.inspection)
        inspection.update(media_path=str(candidate), media_sha256=file_sha256(candidate))
        report = self.check(self.observations(inspection=inspection), inspection=inspection, job=job)
        self.assertEqual(self.result(report, "evidence_integrity")["status"], "PASS")
        self.assertEqual(self.result(report, "candidate_binding")["status"], "FAIL")
        self.assertFalse(report["task_complete"])

    def test_changed_request_without_updating_generation_fingerprint_fails_binding(self):
        job = self.job()
        job["request"]["input"]["prompt"] = "a request for another scene"
        report = self.check(self.observations(), job=job)
        self.assertEqual(self.result(report, "candidate_binding")["status"], "FAIL")
        self.assertFalse(report["task_complete"])

    def test_changed_project_invalidates_previous_contract(self):
        project = copy.deepcopy(self.project)
        project["title"] = "Changed project snapshot"
        self.assert_integrity_failure(self.check(self.observations(), project=project))

    def test_changed_contract_invalidates_previous_observations(self):
        contract = copy.deepcopy(self.contract)
        next(r for r in contract["requirements"] if r["criterion_id"] == "sampled_prop_appearance")["expectation"] += "新约束。"
        self.assert_integrity_failure(self.check(self.observations(), contract=contract))

    def test_replaced_media_bytes_invalidate_inspection(self):
        observations = self.observations()
        self.media.write_bytes(b"different video under the same file name")
        self.assert_integrity_failure(self.check(observations))

    def test_replaced_frame_bytes_invalidate_observations(self):
        observations = self.observations()
        Path(self.inspection["frames"][0]["path"]).write_bytes(b"replacement frame")
        self.assert_integrity_failure(self.check(observations))

    def test_observations_from_a_different_candidate_cannot_be_reused(self):
        old_observations = self.observations()
        candidate = self.root / "candidate-two.mp4"
        candidate.write_bytes(b"newly generated candidate with different pixels")
        inspection = copy.deepcopy(self.inspection)
        inspection.update(media_path=str(candidate), media_sha256=file_sha256(candidate))
        self.assert_integrity_failure(self.check(old_observations, inspection=inspection))

    def test_reextracted_frame_manifest_requires_new_observations(self):
        old_observations = self.observations()
        inspection = copy.deepcopy(self.inspection)
        inspection["frames"][0]["time_s"] = 0.1
        self.assert_integrity_failure(self.check(old_observations, inspection=inspection))

    def test_model_missing_one_required_observation_cannot_pass_it(self):
        observations = self.observations()
        observations["response"]["data"]["observations"] = [
            item for item in observations["response"]["data"]["observations"]
            if item["criterion_id"] != "sampled_prop_appearance"]
        report = self.check(observations)
        self.assertEqual(self.result(report, "sampled_prop_appearance")["status"], "UNKNOWN")
        self.assertFalse(report["task_complete"])

    def test_duplicate_model_criterion_ids_are_not_arbitrarily_selected(self):
        observations = self.observations()
        items = observations["response"]["data"]["observations"]
        item = next(item for item in items if item["criterion_id"] == "sampled_prop_appearance")
        items.append(copy.deepcopy(item))
        report = self.check(observations)
        self.assertEqual(self.result(report, "sampled_prop_appearance")["status"], "UNKNOWN")

    def test_model_unknown_frame_cannot_support_pass(self):
        observations = self.observations()
        item = next(item for item in observations["response"]["data"]["observations"]
                    if item["criterion_id"] == "sampled_prop_appearance")
        item["frame_ids"] = ["unobserved-frame"]
        report = self.check(observations)
        self.assertEqual(self.result(report, "sampled_prop_appearance")["status"], "UNKNOWN")

    def test_model_cannot_claim_continuous_video_authority_from_still_frames(self):
        observations = self.observations()
        observations["capabilities"].append("continuous_video")
        observations["response"]["data"]["observations"].extend([
            {"criterion_id": cid, "verdict": "PASS", "frame_ids": ["F001", "F003"],
             "reason": "模型声称两张首尾帧证明整个交接过程。"}
            for cid in ("action_sequence", "start_end_state")])
        report = self.check(observations)
        for cid in ("action_sequence", "start_end_state"):
            self.assertNotEqual(self.result(report, cid)["status"], "PASS")
        self.assertFalse(report["task_complete"])

    def test_wrong_observation_method_or_missing_capability_invalidates_evidence(self):
        for field, value in (("method", "continuous_video"), ("capabilities", [])):
            with self.subTest(field=field):
                observations = self.observations()
                observations[field] = value
                self.assert_integrity_failure(self.check(observations))

    def test_malformed_frame_or_out_of_range_timestamp_cannot_pass(self):
        malformed = copy.deepcopy(self.inspection)
        malformed["frames"] = [None]
        self.assert_integrity_failure(self.check(inspection=malformed))
        outside = copy.deepcopy(self.inspection)
        outside["frames"][-1]["time_s"] = 80.0
        self.assert_integrity_failure(self.check(inspection=outside))

    def test_valid_sampled_pass_is_limited_to_cited_still_frames(self):
        report = self.check(self.observations())
        result = self.result(report, "sampled_prop_appearance")
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["review_scope"], "cited_still_frames_only")
        self.assertEqual(result["creative_review"], "UNKNOWN")
        self.assertEqual([e["id"] for e in result["evidence"]], ["F001", "F003"])
        self.assertTrue(all(e["sha256"] == file_sha256(e["path"]) for e in result["evidence"]))

    def test_invalid_model_verdict_or_empty_reason_cannot_pass(self):
        for field, value in (("verdict", "PROBABLY"), ("reason", " "), ("frame_ids", [])):
            with self.subTest(field=field):
                observations = self.observations()
                item = next(item for item in observations["response"]["data"]["observations"]
                            if item["criterion_id"] == "sampled_prop_appearance")
                item[field] = value
                report = self.check(observations)
                self.assertEqual(self.result(report, "sampled_prop_appearance")["status"], "UNKNOWN")

    def test_unimplemented_required_capability_stays_unknown(self):
        contract = copy.deepcopy(self.contract)
        contract["requirements"].append({"criterion_id": "future-check", "kind": "unimplemented",
                                         "required": True, "expectation": "A future supported check"})
        report = self.check(self.observations(contract=contract), contract=contract)
        self.assertEqual(self.result(report, "future-check")["status"], "UNKNOWN")
        self.assertFalse(report["task_complete"])

    def test_contract_requires_unique_ids_and_explicit_required_flags(self):
        duplicate = copy.deepcopy(self.contract)
        duplicate["requirements"].append(copy.deepcopy(duplicate["requirements"][0]))
        with self.assertRaises(AcceptanceError):
            validate_contract(duplicate)
        no_required = copy.deepcopy(self.contract)
        for item in no_required["requirements"]:
            item["required"] = False
        with self.assertRaises(AcceptanceError):
            validate_contract(no_required)

    def assert_policy_rejects(self, changed_contract):
        # Fresh observations match the edited contract and the job still binds
        # the correct candidate. Rejection must come from the fixed policy.
        report = self.check(self.observations(contract=changed_contract),
                            contract=changed_contract, job=self.job())
        self.assertEqual(self.result(report, "candidate_binding")["status"], "PASS")
        self.assertEqual(self.result(report, "evidence_integrity")["status"], "FAIL")
        self.assertEqual(report["status"], "FAIL")
        self.assertFalse(report["task_complete"])

    def test_deleting_content_gates_cannot_make_a_technical_only_pass_complete(self):
        contract = copy.deepcopy(self.contract)
        contract["requirements"] = [r for r in contract["requirements"] if r["kind"] == "technical"]
        self.assert_policy_rejects(contract)

    def test_making_mandatory_content_optional_cannot_bypass_unknown(self):
        contract = copy.deepcopy(self.contract)
        for requirement in contract["requirements"]:
            if requirement["kind"] != "technical":
                requirement["required"] = False
        self.assert_policy_rejects(contract)

    def test_replacing_continuous_kind_with_sampled_kind_cannot_lower_evidence_standard(self):
        contract = copy.deepcopy(self.contract)
        for requirement in contract["requirements"]:
            if requirement["kind"] == "continuous_video":
                requirement["kind"] = "sampled_visual"
        self.assert_policy_rejects(contract)

    def test_deleting_neighbor_gate_cannot_drop_mandatory_regression_scope(self):
        contract = copy.deepcopy(self.contract)
        contract["requirements"] = [r for r in contract["requirements"]
                                    if r["criterion_id"] != "continuity:S002->S003"]
        self.assert_policy_rejects(contract)

    def test_changing_scope_or_removing_regression_edges_is_rejected(self):
        reduced_scope = copy.deepcopy(self.contract)
        reduced_scope["scope"] = "only_this_shot"
        self.assert_policy_rejects(reduced_scope)
        reduced_regression = copy.deepcopy(self.contract)
        reduced_regression["regression"]["continuity_edges"] = []
        self.assert_policy_rejects(reduced_regression)

    def test_loosening_duration_tolerance_cannot_turn_a_mismatch_into_pass(self):
        contract = copy.deepcopy(self.contract)
        next(r for r in contract["requirements"] if r["criterion_id"] == "duration")["tolerance_s"] = 1000
        self.assert_policy_rejects(contract)

    def test_s002_regression_preserves_incoming_and_outgoing_continuity_context(self):
        plan = regression_plan(self.project, "S002")
        self.assertEqual(plan["shot_checks"], ["S002", "S003"])
        self.assertEqual(plan["context_shot_ids"], ["S001", "S002", "S003"])
        self.assertEqual(plan["continuity_edges"], [["S001", "S002"], ["S002", "S003"]])
        self.assertFalse(plan["checks_executed"])
        report = self.check(self.observations())
        for cid in ("continuity:S001->S002", "continuity:S002->S003"):
            result = self.result(report, cid)
            self.assertTrue(result["required"])
            self.assertEqual(result["status"], "UNKNOWN")
        self.assertFalse(report["regression"]["checks_executed"])


if __name__ == "__main__":
    unittest.main()
