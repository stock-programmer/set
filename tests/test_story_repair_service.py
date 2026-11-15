"""Offline service/HTTP contracts for bounded story repair; no provider calls."""
import copy
import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pianchang.app_service import AppService, read_json, write_json
from pianchang.story_spec import StoryError, digest_json
from pianchang.web_server import create_server


class StoryRepairServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {"AGENT_TRACE_DIR": str(self.root / "traces")})
        env.start()
        self.addCleanup(env.stop)
        config = SimpleNamespace(api_key="", model="offline-model", output_dir=self.root / "videos")
        loaders = patch.multiple("pianchang.app_service", load_config=Mock(return_value=config),
                                 load_text_config=Mock(return_value=config),
                                 load_observer_config=Mock(return_value=config))
        loaders.start()
        self.addCleanup(loaders.stop)
        self.service = AppService(self.root)
        self.addCleanup(self.service.close)
        old_span = self.service.tracer.start_span("offline.story.source", kind="task", new_trace=True)
        self.service.tracer.end_span(old_span.context, status="ERROR", error={"message": "offline count mismatch"})
        self.source = {"id": "a" * 32, "kind": "story", "project_id": None,
                       "status": "failed", "created_at": "2026-11-04T14:42:48+00:00",
                       "updated_at": "2026-11-04T14:43:07+00:00", "message": "offline failure",
                       "error": "expected 3 beats, actual 2", "result": None, "trace_id": old_span.trace_id}
        self.service.state["jobs"].append(self.source)
        self.service._save()
        self.inputs = {"idea": "结婚当天新娘逃跑（离线修复样例）", "episodes": 1, "scenes": 1, "beats": 3}
        self.folder = self.service.state_dir / "stories" / self.source["id"]
        self.seed = {"title": "离线草稿", "episodes": [{"scenes": [{"beats": ["准备婚礼", "新娘犹豫"]}]}]}
        write_json(self.folder / "run.json", {"inputs": {"idea": self.inputs["idea"], "episodes": 1,
                   "scenes_per_episode": 1, "beats_per_scene": 3}})
        write_json(self.folder / "draft-call.json", {"status": "COMPLETED", "response": {"data": self.seed}, "offline": True})
        write_json(self.folder / "draft-validation.json", {"status": "FAIL", "checks": [{
            "path": "episodes[0].scenes[0].beats", "expected": 3, "actual": 2, "status": "FAIL"}]})

    def queue(self):
        with patch.object(self.service.executor, "submit") as enqueue:
            public = self.service.submit("story", None, {"repair_from": self.source["id"]}, "offline-repair")
        enqueue.assert_called_once()
        _, job, body, project_hash = enqueue.call_args.args
        self.assertIsNone(project_hash)
        return public, job, body

    def test_http_repair_recovers_original_constraints_and_links_trace_without_rewriting_source(self):
        original_job = copy.deepcopy(self.source)
        original_files = {p.name: p.read_bytes() for p in self.folder.iterdir()}
        server = create_server(self.service, port=0)
        server.daemon_threads = False
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(3)))
        port = server.server_address[1]
        with patch.object(self.service.executor, "submit") as enqueue:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
            try:
                conn.request("POST", "/api/projects", json.dumps({"repair_from": self.source["id"]}),
                             {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"})
                response = conn.getresponse()
                public = json.loads(response.read())["job"]
                self.assertEqual(response.status, 202)
            finally:
                conn.close()
        _, job, body, _ = enqueue.call_args.args
        self.assertEqual(public["repair_from"], self.source["id"])
        self.assertEqual(job["story_input"], self.inputs)
        self.assertEqual({k: body[k] for k in self.inputs}, self.inputs)
        self.assertEqual(body["repair_source"]["draft_sha256"], digest_json(self.seed))
        self.assertNotEqual(public["id"], self.source["id"])
        self.assertEqual(self.source, original_job)
        self.assertEqual({p.name: p.read_bytes() for p in self.folder.iterdir()}, original_files)
        root = self.service.tracer.get_trace(public["trace_id"])
        self.assertIn({"trace_id": self.source["trace_id"], "relation": "repairs"}, root["links"])
        self.assertTrue(any(link["relation"] == "submitted_by" for link in root["links"]))
        self.assertEqual(read_json(self.service.state_dir / "state.json")["jobs"][-1]["story_input"], self.inputs)

    def test_latest_completed_candidate_takes_precedence_over_original_draft(self):
        latest = {**self.seed, "title": "最新的离线失败修订"}
        write_json(self.folder / "draft-candidate.json", latest)
        write_json(self.folder / "draft-repair-01-call.json", {"status": "COMPLETED", "offline": True})
        saved = self.service._story_repair_source(self.source["id"])
        self.assertEqual(saved["draft"], latest)
        self.assertEqual(saved["source"]["draft_sha256"], digest_json(latest))

    def test_repair_request_cannot_override_original_idea_counts_or_provenance(self):
        for field, value in (("idea", "替换剧情"), ("episodes", 2), ("beats", 2), ("repair_source", {})):
            with self.subTest(field=field), patch.object(self.service.executor, "submit") as enqueue:
                with self.assertRaisesRegex(ValueError, "不能覆盖参数"):
                    self.service.submit("story", None, {"repair_from": self.source["id"], field: value})
                enqueue.assert_not_called()
        with self.assertRaisesRegex(ValueError, "不能自行指定"):
            self.service.submit("story", None, {**self.inputs, "repair_source": {"job_id": self.source["id"]}})
        self.assertEqual(len(self.service.state["jobs"]), 1)

    def test_unknown_provider_result_is_not_automatically_replayed(self):
        for stage in ("draft-repair-01", "compile-01"):
            with self.subTest(stage=stage):
                path = self.folder / f"{stage}-call.json"
                write_json(path, {"status": "FAILED_OR_INTERRUPTED", "offline": True})
                with patch.object(self.service.executor, "submit") as enqueue:
                    with self.assertRaisesRegex(ValueError, "未确认的模型调用"):
                        self.service.submit("story", None, {"repair_from": self.source["id"]})
                    enqueue.assert_not_called()
                # This is a single explicitly created offline fixture file.
                path.unlink()
        self.assertEqual(len(self.service.state["jobs"]), 1)

    def test_source_or_constraints_changed_while_queued_stop_before_model_build(self):
        _, job, body = self.queue()
        write_json(self.folder / "draft-candidate.json", {**self.seed, "title": "排队后改变"})
        with patch("pianchang.app_service.StoryRunner") as runner:
            with self.assertRaisesRegex(ValueError, "排队期间"):
                self.service._build_story(job, body)
            runner.assert_not_called()
        (self.folder / "draft-candidate.json").unlink()
        write_json(self.folder / "run.json", {"inputs": {"idea": self.inputs["idea"], "episodes": 1,
                   "scenes_per_episode": 1, "beats_per_scene": 2}})
        with patch("pianchang.app_service.StoryRunner") as runner:
            with self.assertRaisesRegex(ValueError, "排队期间"):
                self.service._build_story(job, body)
            runner.assert_not_called()

    def test_build_receives_seed_fixed_budget_and_progress_callback(self):
        _, job, body = self.queue()
        directory = self.service.state_dir / "stories" / job["id"]
        def build(idea, output, **kwargs):
            self.assertEqual(idea, self.inputs["idea"])
            self.assertEqual(Path(output), directory)
            self.assertEqual(kwargs["initial_draft"], self.seed)
            self.assertEqual(kwargs["repair_source"], body["repair_source"])
            self.assertEqual(kwargs["max_repairs"], 2)
            self.assertEqual(kwargs["beats"], 3)
            runner.call_args.kwargs["progress"]({"summary": "离线修复第 1 次", "stage": "draft", "attempt": 1})
            write_json(directory / "project.json", {"title": "离线完成", "shots": [], "entities": {}})
            return {"ready_for_generation": True, "project_path": str(directory / "project.json"),
                    "draft_attempts": 2, "draft_repair_count": 1, "compile_attempts": 1}
        with patch("pianchang.app_service.StoryRunner") as runner, patch.object(self.service.asset_executor, "submit") as assets:
            runner.return_value.build.side_effect = build
            result = self.service._build_story(job, body)
            runner.return_value.build.assert_called_once()
            assets.assert_called_once()
        self.assertEqual(job["story_progress"]["attempt"], 1)
        self.assertEqual(job["message"], "离线修复第 1 次")
        self.assertEqual(result["draft_repair_count"], 1)
        self.assertEqual(result["repair_from"], self.source["id"])
        self.assertTrue(result["ready_for_generation"])

    def test_exhausted_repair_build_stays_failed_and_service_never_requeues_it(self):
        _, job, body = self.queue()
        with patch("pianchang.app_service.StoryRunner") as runner, patch.object(self.service.asset_executor, "submit") as enqueue:
            runner.return_value.build.side_effect = StoryError("离线校验仍失败；修复次数耗尽")
            self.service._execute(job, body, None)
            runner.return_value.build.assert_called_once()
            enqueue.assert_not_called()
        self.assertEqual(job["status"], "failed")
        self.assertIsNone(job["result"])
        self.assertIn("修复次数耗尽", job["error"])
        self.assertEqual(self.source["status"], "failed")
        self.assertEqual(len(self.service.state["jobs"]), 2)

    def test_source_must_be_failed_story_with_completed_readable_draft(self):
        with patch.object(self.service.executor, "submit") as enqueue:
            for status in ("queued", "running", "succeeded"):
                self.source["status"] = status
                with self.subTest(status=status), self.assertRaisesRegex(ValueError, "只有失败或中断"):
                    self.service.submit("story", None, {"repair_from": self.source["id"]})
            self.source["status"] = "failed"
            self.source["kind"] = "agent"
            with self.assertRaises(KeyError):
                self.service.submit("story", None, {"repair_from": self.source["id"]})
            self.source["kind"] = "story"
            write_json(self.folder / "draft-call.json", {"status": "COMPLETED", "response": {"data": []}})
            with self.assertRaisesRegex(ValueError, "没有已完成"):
                self.service.submit("story", None, {"repair_from": self.source["id"]})
            enqueue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
