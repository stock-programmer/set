"""Offline behavioral tests of the drama agent's persistent video workflow."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from pianchang.bailian_video import VideoConfig, VideoError, build_request
from pianchang.video_agent import VideoRunner, compile_shot, main


ROOT = Path(__file__).resolve().parents[1]


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeClient:
    """A deterministic provider boundary; no HTTP requests or real media."""

    def __init__(self, statuses=(), *, submit_error=None, download_errors=0, diagnostic=""):
        self.statuses = list(statuses)
        self.submit_error = submit_error
        self.download_errors = download_errors
        self.diagnostic = diagnostic
        self.submissions = []
        self.queries = []
        self.downloads = []

    def submit(self, payload):
        self.submissions.append(payload)
        if self.submit_error:
            raise self.submit_error
        return {
            "request_id": "request-submit",
            "output": {"task_id": f"task-{len(self.submissions)}", "task_status": "PENDING"},
            "diagnostic": self.diagnostic,
        }

    def get_task(self, task_id):
        self.queries.append(task_id)
        status = self.statuses.pop(0) if self.statuses else "RUNNING"
        if isinstance(status, Exception):
            raise status
        output = {"task_id": task_id, "task_status": status}
        response = {"request_id": "request-poll", "output": output, "diagnostic": self.diagnostic}
        if status == "SUCCEEDED":
            output["video_url"] = "https://media.example.invalid/generated.mp4"
            response["usage"] = {"duration": 6, "output_video_duration": 6, "video_count": 1}
        elif status == "FAILED":
            output.update(code="InvalidParameter", message="Invalid size")
        return response

    def download(self, url, destination):
        destination = Path(destination)
        self.downloads.append((url, destination))
        if self.download_errors:
            self.download_errors -= 1
            raise VideoError("download connection interrupted")
        destination.write_bytes(b"offline-test-media-not-a-real-video")
        return destination


class CompileShotTests(unittest.TestCase):
    def setUp(self):
        self.project = json.loads((ROOT / "examples" / "example.json").read_text(encoding="utf-8"))

    def test_shots_carry_canonical_descriptions_state_and_original_duration(self):
        for shot_id, duration, holder_after in (("S001", 6, "char:lin"), ("S002", 8, "char:chen")):
            with self.subTest(shot=shot_id):
                compiled = compile_shot(self.project, shot_id)
                self.assertEqual(compiled["duration"], duration)
                self.assertIn("成年女性，短黑发，左眉有浅疤", compiled["prompt"])
                self.assertIn("西墙门、北墙窗、中央木桌", compiled["prompt"])
                self.assertIn("米色双排扣风衣", compiled["prompt"])
                self.assertIn("prop:key.holder", compiled["prompt"])
                self.assertIn("char:chen.knows_key_origin", compiled["prompt"])
                self.assertEqual(compiled["metadata"]["state_before"]["prop:key.holder"], "char:lin")
                self.assertEqual(compiled["metadata"]["state_after"]["prop:key.holder"], holder_after)
                self.assertFalse(compiled["metadata"]["state_after"]["char:chen.knows_key_origin"])
                self.assertEqual(compiled["metadata"]["input_versions"]["char:lin"], 1)
                self.assertTrue(compiled["metadata"]["spec_pass"])
                self.assertFalse(compiled["metadata"]["reference_media_used"])
        self.assertIn(self.project["events"][0]["reason"], compile_shot(self.project, "S002")["prompt"])

    def test_unknown_shot_is_rejected(self):
        with self.assertRaises(VideoError):
            compile_shot(self.project, "S999")

    def test_invalid_spec_is_rejected_even_with_prompt_override(self):
        broken = json.loads((ROOT / "examples" / "broken_example.json").read_text(encoding="utf-8"))
        with self.assertRaisesRegex(VideoError, "CONTINUITY"):
            compile_shot(broken, "S003", prompt_override="自定义提示词")

    def test_explicit_prompt_override_preserves_traceability(self):
        compiled = compile_shot(self.project, "S002", prompt_override="重新编写的提示词")
        self.assertEqual(compiled["prompt"], "重新编写的提示词")
        self.assertTrue(compiled["metadata"]["prompt_overridden"])
        self.assertEqual(compiled["metadata"]["shot_id"], "S002")
        self.assertEqual(compiled["metadata"]["target_duration_s"], 8)

    def test_event_actions_follow_shot_order_instead_of_registry_order(self):
        self.project["shots"] = self.project["shots"][:2]
        handover = self.project["events"][0]
        return_key = {
            "id": "event:return",
            "reason": "陈默检查钥匙后，把它交还给林岚。",
            "requires": {"prop:key.holder": "char:chen"},
            "changes": {"prop:key.holder": "char:lin"},
        }
        self.project["events"] = [return_key, handover]
        shot = self.project["shots"][1]
        shot["event_ids"] = [handover["id"], return_key["id"]]
        shot["expected_after"] = {"prop:key.holder": "char:lin"}
        compiled = compile_shot(self.project, "S002")
        self.assertEqual(compiled["metadata"]["state_after"]["prop:key.holder"], "char:lin")
        self.assertLess(compiled["prompt"].index(handover["reason"]),
                        compiled["prompt"].index(return_key["reason"]))

    def test_empty_shot_description_needs_an_explicit_prompt(self):
        for description in (None, "", "   "):
            with self.subTest(description=description):
                shot = self.project["shots"][0]
                if description is None:
                    shot.pop("description", None)
                else:
                    shot["description"] = description
                with self.assertRaises(VideoError):
                    compile_shot(self.project, "S001")
                compiled = compile_shot(self.project, "S001", prompt_override="林岚拿出铜钥匙。")
                self.assertEqual(compiled["prompt"], "林岚拿出铜钥匙。")
                self.assertTrue(compiled["metadata"]["prompt_overridden"])


class VideoWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = VideoConfig(
            api_key="sk-offline-test-credential", output_dir=Path(self.directory.name),
            poll_interval=1, wait_timeout=3,
        )
        self.payload = build_request(self.config, "办公室内，林岚拿出铜钥匙。", duration=6)
        self.clock = FakeClock()

    def runner(self, client, config=None):
        return VideoRunner(config or self.config, client,
                           sleep=self.clock.sleep, monotonic=self.clock.monotonic)

    @staticmethod
    def read_job(job):
        return json.loads(Path(job["manifest_path"]).read_text(encoding="utf-8"))

    def test_full_lifecycle_downloads_and_never_claims_media_quality(self):
        client = FakeClient(["PENDING", "RUNNING", "SUCCEEDED"], diagnostic=self.config.api_key)
        runner = self.runner(client)
        submitted = runner.submit(self.payload, {"shot_id": "S001"})
        self.assertEqual(submitted["status"], "PENDING")
        self.assertFalse(submitted["media_generated"])
        result = runner.resume(submitted["manifest_path"])
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertTrue(result["media_generated"])
        self.assertIsNone(result["visual_pass"])
        self.assertIsNone(result["audio_pass"])
        self.assertIsNone(result["cost"])
        self.assertEqual(result["usage"]["output_video_duration"], 6)
        self.assertEqual(Path(result["local_video_path"]).read_bytes(), b"offline-test-media-not-a-real-video")
        self.assertGreater(result["download_bytes"], 0)
        self.assertEqual(len(result["download_sha256"]), 64)
        self.assertEqual(len(client.submissions), 1)
        self.assertEqual(client.queries, [submitted["task_id"]] * 3)
        self.assertEqual(len(self.clock.sleeps), 2)
        self.assertNotIn(self.config.api_key, Path(result["manifest_path"]).read_text(encoding="utf-8"))

    def test_identical_request_reuses_task_across_runner_instances(self):
        client = FakeClient()
        first = self.runner(client).submit(self.payload, {"shot_id": "S001"})
        second = self.runner(client).submit(self.payload, {"shot_id": "S001"})
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertEqual(first["manifest_path"], second["manifest_path"])
        self.assertEqual(len(client.submissions), 1)

    def test_transient_manifest_lock_retries_the_write_without_another_provider_submission(self):
        from pianchang import atomic_files
        original_replace = atomic_files._replace_file
        denied = PermissionError("offline Windows job manifest sharing conflict"); denied.winerror = 5
        seen = []
        def replacing(source, target):
            seen.append((source, target))
            if len(seen) == 1:
                raise denied
            return original_replace(source, target)
        client = FakeClient(["SUCCEEDED"])
        with patch("pianchang.atomic_files._replace_file", side_effect=replacing), patch("pianchang.atomic_files._sleep"):
            runner = self.runner(client)
            submitted = runner.submit(self.payload, {"shot_id": "S001"})
            result = runner.resume(submitted["manifest_path"])
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(len(client.submissions), 1)
        self.assertEqual(client.queries, [submitted["task_id"]])

    def test_explicit_new_candidate_creates_separate_job(self):
        client = FakeClient()
        runner = self.runner(client)
        first = runner.submit(self.payload)
        second = runner.submit(self.payload, new=True)
        self.assertNotEqual(first["manifest_path"], second["manifest_path"])
        self.assertNotEqual(first["task_id"], second["task_id"])
        self.assertEqual(len(client.submissions), 2)

    def test_ambiguous_submit_is_recorded_and_cannot_repeat_post(self):
        client = FakeClient(submit_error=VideoError("timeout echo " + self.config.api_key))
        runner = self.runner(client)
        with self.assertRaises(VideoError) as raised:
            runner.submit(self.payload)
        self.assertNotIn(self.config.api_key, str(raised.exception))
        manifests = list(Path(self.directory.name).glob("*/job.json"))
        self.assertEqual(len(manifests), 1)
        text = manifests[0].read_text(encoding="utf-8")
        stored = json.loads(text)
        self.assertEqual(stored["status"], "SUBMISSION_UNKNOWN")
        self.assertIsNone(stored["task_id"])
        self.assertNotIn(self.config.api_key, text)
        with self.assertRaises(VideoError):
            self.runner(client).submit(self.payload)
        with self.assertRaises(VideoError):
            runner.resume(manifests[0])
        self.assertEqual(len(client.submissions), 1)
        self.assertEqual(client.queries, [])

    def test_timeout_preserves_task_and_new_runner_can_resume(self):
        client = FakeClient()
        submitted = self.runner(client).submit(self.payload)
        with self.assertRaisesRegex(VideoError, "超时"):
            self.runner(client).resume(submitted["manifest_path"])
        stored = self.read_job(submitted)
        self.assertEqual(stored["task_id"], submitted["task_id"])
        self.assertEqual(stored["status"], "RUNNING")
        self.assertLessEqual(sum(self.clock.sleeps), self.config.wait_timeout)
        client.statuses.append("SUCCEEDED")
        result = self.runner(client).resume(submitted["manifest_path"])
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(len(client.submissions), 1)

    def test_terminal_failures_stop_polling_and_keep_provider_status(self):
        for status in ("FAILED", "CANCELED", "UNKNOWN"):
            with self.subTest(status=status):
                client = FakeClient([status])
                runner = self.runner(client)
                submitted = runner.submit(self.payload, new=True)
                with self.assertRaisesRegex(VideoError, status):
                    runner.resume(submitted["manifest_path"])
                stored = self.read_job(submitted)
                self.assertEqual(stored["status"], status)
                self.assertEqual(stored["task_id"], submitted["task_id"])
                self.assertFalse(stored["media_generated"])
                self.assertEqual(len(client.queries), 1)
                self.assertEqual(len(client.submissions), 1)
                self.assertEqual(client.downloads, [])
        self.assertEqual(self.clock.sleeps, [])

    def test_download_failure_can_resume_without_regeneration(self):
        client = FakeClient(["SUCCEEDED", "SUCCEEDED"], download_errors=1)
        runner = self.runner(client)
        submitted = runner.submit(self.payload)
        with self.assertRaisesRegex(VideoError, "download connection interrupted"):
            runner.resume(submitted["manifest_path"])
        stored = self.read_job(submitted)
        self.assertEqual(stored["status"], "SUCCEEDED")
        self.assertTrue(stored["media_generated"])
        self.assertEqual(stored["task_id"], submitted["task_id"])
        result = self.runner(client).resume(submitted["manifest_path"])
        self.assertTrue(Path(result["local_video_path"]).is_file())
        self.assertEqual(len(client.submissions), 1)
        self.assertEqual(len(client.downloads), 2)

    def test_poll_network_failure_can_resume_same_task(self):
        client = FakeClient([VideoError("temporary network failure"), "SUCCEEDED"])
        runner = self.runner(client)
        submitted = runner.submit(self.payload)
        with self.assertRaisesRegex(VideoError, "temporary network failure"):
            runner.resume(submitted["manifest_path"])
        result = runner.resume(submitted["manifest_path"])
        self.assertEqual(result["task_id"], submitted["task_id"])
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(len(client.submissions), 1)

    def test_existing_verified_download_can_be_used_offline(self):
        client = FakeClient(["SUCCEEDED"])
        runner = self.runner(client)
        submitted = runner.submit(self.payload)
        result = runner.resume(submitted["manifest_path"])
        client.statuses = [VideoError("network unavailable")]
        reused = self.runner(client).resume(result["manifest_path"])
        self.assertEqual(reused["download_sha256"], result["download_sha256"])
        self.assertEqual(len(client.queries), 1)
        self.assertEqual(len(client.downloads), 1)

    def test_corrupt_local_video_is_redownloaded_without_post(self):
        client = FakeClient(["SUCCEEDED", "SUCCEEDED"])
        runner = self.runner(client)
        submitted = runner.submit(self.payload)
        result = runner.resume(submitted["manifest_path"])
        Path(result["local_video_path"]).write_bytes(b"damaged")
        repaired = runner.resume(result["manifest_path"])
        self.assertEqual(Path(repaired["local_video_path"]).read_bytes(), b"offline-test-media-not-a-real-video")
        self.assertEqual(len(client.submissions), 1)
        self.assertEqual(len(client.downloads), 2)

    def test_status_checks_once_without_waiting_or_downloading(self):
        client = FakeClient(["RUNNING"])
        runner = self.runner(client)
        submitted = runner.submit(self.payload)
        result = runner.resume(submitted["manifest_path"], wait=False, download=False)
        self.assertEqual(result["status"], "RUNNING")
        self.assertEqual(len(client.queries), 1)
        self.assertEqual(client.downloads, [])
        self.assertEqual(self.clock.sleeps, [])

    def test_endpoint_mismatch_is_rejected_before_network(self):
        client = FakeClient()
        submitted = self.runner(client).submit(self.payload)
        other = replace(self.config, base_url="https://example.cn-beijing.maas.aliyuncs.com/api/v1")
        with self.assertRaisesRegex(VideoError, "Endpoint"):
            self.runner(client, other).resume(submitted["manifest_path"])
        self.assertEqual(client.queries, [])

    def cli(self, args, client=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("pianchang.video_agent.load_config", return_value=self.config), \
                patch("pianchang.video_agent.BailianVideoClient", return_value=client) as factory, \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(args)
        return code, stdout.getvalue(), stderr.getvalue(), factory

    def test_invalid_project_never_creates_provider_client_or_submits(self):
        client = FakeClient()
        code, _, stderr, factory = self.cli([
            "generate", "--project", str(ROOT / "examples" / "broken_example.json"), "--shot", "S003",
        ], client)
        self.assertEqual(code, 1)
        self.assertIn("CONTINUITY", stderr)
        factory.assert_not_called()
        self.assertEqual(client.submissions, [])
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_cli_dry_run_needs_no_key_and_records_duration_override(self):
        self.config = replace(self.config, api_key="")
        code, stdout, stderr, factory = self.cli([
            "generate", "--project", str(ROOT / "examples" / "example.json"), "--shot", "S001",
            "--duration", "5", "--dry-run",
        ])
        self.assertEqual(code, 0, stderr)
        result = json.loads(stdout)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["request"]["parameters"]["duration"], 5)
        self.assertEqual(result["metadata"]["target_duration_s"], 6)
        self.assertTrue(result["metadata"]["duration_differs_from_target"])
        self.assertFalse(result["media_generated"])
        factory.assert_not_called()

    def test_cli_new_flag_requests_a_new_candidate(self):
        client = FakeClient()
        args = ["generate", "--prompt", "办公室内，林岚拿出铜钥匙。", "--no-wait"]
        first_code, first_stdout, first_error, _ = self.cli(args, client)
        second_code, second_stdout, second_error, _ = self.cli(args + ["--new"], client)
        self.assertEqual(first_code, 0, first_error)
        self.assertEqual(second_code, 0, second_error)
        self.assertNotEqual(json.loads(first_stdout)["task_id"], json.loads(second_stdout)["task_id"])
        self.assertEqual(len(client.submissions), 2)
        self.assertEqual(client.queries, [])


if __name__ == "__main__":
    unittest.main()
