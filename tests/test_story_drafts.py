"""Offline story-draft visibility regressions; no provider or real credentials."""
import hashlib
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
from pianchang.web_server import create_server


class StoryDraftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        environment = patch.dict(os.environ, {"AGENT_TRACE_DIR": str(self.root / "traces")})
        environment.start()
        self.addCleanup(environment.stop)
        configuration = SimpleNamespace(api_key="", model="offline-model", output_dir=self.root / "videos")
        loaders = patch.multiple("pianchang.app_service", load_config=Mock(return_value=configuration),
                                 load_text_config=Mock(return_value=configuration),
                                 load_observer_config=Mock(return_value=configuration))
        loaders.start()
        self.addCleanup(loaders.stop)
        self.service = AppService(self.root)
        self.addCleanup(self.service.close)

    def job(self, suffix="1", **changes):
        job = {"id": suffix * 32, "kind": "story", "project_id": None, "status": "failed",
               "created_at": "2026-11-04T14:42:48+00:00", "updated_at": "2026-11-04T14:43:07+00:00",
               "error": "episodes[0].scenes[0].beats: expected 3, actual 2", "message": "offline failure",
               "result": None, "trace_id": None}
        job.update(changes)
        self.service.state["jobs"].append(job)
        self.service._save()
        return job

    def legacy_files(self, job):
        folder = self.service.state_dir / "stories" / job["id"]
        write_json(folder / "run.json", {"inputs": {"idea": "结婚当天新娘逃跑（离线样例）",
                   "episodes": 1, "scenes_per_episode": 1, "beats_per_scene": 3}})
        draft = {"title": "离线新娘故事", "episodes": [{"scenes": [{"beats": ["开始", "逃跑"]}]}]}
        write_json(folder / "draft-call.json", {"response": {"data": draft}, "offline": True})
        write_json(folder / "draft-validation.json", {"status": "FAIL", "checks": [{
            "path": "episodes[0].scenes[0].beats", "expected": 3, "actual": 2, "status": "FAIL"}]})
        return folder, draft

    def test_failed_legacy_story_remains_visible_with_original_constraints_and_evidence(self):
        job = self.job()
        folder, draft = self.legacy_files(job)
        originals = {path.name: path.read_bytes() for path in folder.iterdir()}
        with patch("pianchang.app_service.StoryRunner.build", side_effect=AssertionError("No model retries")) as build:
            bootstrap = self.service.bootstrap()
            detail = self.service.story_draft_detail(job["id"])
        self.assertEqual(bootstrap["projects"], [])
        self.assertEqual([d["id"] for d in bootstrap["story_drafts"]], [job["id"]])
        self.assertIn("新娘逃跑", detail["idea"])
        self.assertEqual(detail["inputs"], {"idea": "结婚当天新娘逃跑（离线样例）", "episodes": 1, "scenes": 1, "beats": 3})
        self.assertEqual(detail["draft"], draft)
        self.assertFalse(detail["ready_for_generation"])
        self.assertEqual(detail["content_status"], "unvalidated_draft")
        self.assertEqual(detail["validation"]["checks"][0], {
            "path": "episodes[0].scenes[0].beats", "expected": 3, "actual": 2, "status": "FAIL"})
        self.assertEqual({a["name"] for a in detail["artifacts"]}, {"run.json", "draft-call.json", "draft-validation.json"})
        for artifact in detail["artifacts"]:
            public_path = self.service.resolve_asset(artifact["url"].rsplit("/", 1)[1])
            self.assertTrue(public_path.is_relative_to(self.service.state_dir / "story-draft-previews" / job["id"]))
            self.assertEqual(public_path.name, artifact["name"])
            self.assertTrue(artifact["redacted"])
            self.assertEqual(public_path.parent.name, hashlib.sha256(public_path.read_bytes()).hexdigest())
            self.assertEqual(read_json(public_path), read_json(folder / artifact["name"]))
        self.assertEqual({path.name: path.read_bytes() for path in folder.iterdir()}, originals)
        build.assert_not_called()

    def test_queued_story_is_persisted_and_visible_before_any_output_exists(self):
        request = {"idea": "排队中的离线婚礼故事", "episodes": 1, "scenes": 1, "beats": 3}
        with patch.object(self.service.executor, "submit") as enqueue:
            job = self.service.submit("story", None, request, "offline-draft-request")
        enqueue.assert_called_once()
        self.assertEqual(job["story_input"], request)
        self.assertFalse((self.service.state_dir / "stories" / job["id"]).exists())
        saved = read_json(self.service.state_dir / "state.json")["jobs"][0]
        self.assertEqual(saved["story_input"], request)
        detail = self.service.story_draft_detail(job["id"])
        self.assertEqual(self.service.story_drafts()[0]["id"], job["id"])
        self.assertEqual(detail["status"], "queued")
        self.assertEqual(detail["inputs"], request)
        self.assertIsNone(detail["draft"])
        self.assertIsNone(detail["validation"])
        self.assertFalse(detail["ready_for_generation"])

    def test_successful_story_replaces_draft_but_old_deep_link_resolves_project(self):
        job = self.job(status="succeeded", error=None)
        folder, _ = self.legacy_files(job)
        project_path = folder / "project.json"
        write_json(project_path, {"title": "离线成功工程", "shots": []})
        expected = hashlib.sha256(str(project_path.resolve()).encode()).hexdigest()[:16]
        self.assertEqual(self.service.story_drafts(), [])
        self.assertEqual(self.service.story_draft_detail(job["id"])["project_id"], expected)
        bootstrap = self.service.bootstrap()
        self.assertEqual([p["id"] for p in bootstrap["projects"]], [expected])
        self.assertEqual(bootstrap["story_drafts"], [])

    def test_saved_project_result_resolves_without_recreating_missing_files(self):
        job = self.job(status="succeeded", result={"project_id": "existing-project"})
        self.assertEqual(self.service.story_drafts(), [])
        self.assertEqual(self.service.story_draft_detail(job["id"])["project_id"], "existing-project")
        self.assertFalse((self.service.state_dir / "stories" / job["id"]).exists())

    def test_invalid_and_non_story_identifiers_cannot_access_draft_files(self):
        other = self.job(kind="agent")
        self.legacy_files(other)
        self.assertEqual(self.service.story_drafts(), [])
        for identifier in (other["id"], "f" * 32, "../state", "A" * 32, None):
            with self.subTest(identifier=identifier), self.assertRaises(KeyError):
                self.service.story_draft_detail(identifier)

    def test_public_draft_payloads_redact_registered_offline_secret(self):
        secret = "offline-credential-placeholder-only"
        self.service.secrets.add(secret)
        self.service.tracer.register_secret(secret)
        job = self.job(story_input={"idea": "离线创意 " + secret, "episodes": 1, "scenes": 1, "beats": 3},
                       error="offline error " + secret)
        folder, _ = self.legacy_files(job)
        write_json(folder / "draft-call.json", {"response": {"data": {"title": secret}}})
        public = json.dumps([self.service.story_drafts(), self.service.story_draft_detail(job["id"])])
        self.assertNotIn(secret, public)
        self.assertIn("[REDACTED]", public)

    def test_json_and_markdown_downloads_are_redacted_without_mutating_source_evidence(self):
        secret = "offline-download-credential-placeholder"
        self.service.secrets.add(secret)
        self.service.tracer.register_secret(secret)
        job = self.job()
        folder, _ = self.legacy_files(job)
        write_json(folder / "run.json", {"inputs": {"idea": "离线创意 " + secret,
                   "episodes": 1, "scenes_per_episode": 1, "beats_per_scene": 3}})
        write_json(folder / "draft-call.json", {"response": {"data": {"title": "离线标题", "text": secret}}})
        (folder / "script.md").write_text("# 离线证据\n原始敏感内容 " + secret, encoding="utf-8")
        originals = {path.name: path.read_bytes() for path in folder.iterdir()}
        detail = self.service.story_draft_detail(job["id"])
        server = create_server(self.service, port=0)
        server.daemon_threads = False  # Join request workers before removing their trace database.
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(3)))
        selected = [item for item in detail["artifacts"] if item["name"] in {"run.json", "draft-call.json", "script.md"}]
        self.assertEqual(len(selected), 3)
        for artifact in selected:
            with self.subTest(name=artifact["name"]):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=15)
                try:
                    connection.request("GET", artifact["url"])
                    response = connection.getresponse()
                    downloaded = response.read().decode("utf-8")
                    self.assertEqual(response.status, 200)
                finally:
                    connection.close()
                self.assertTrue(artifact["redacted"])
                self.assertNotIn(secret, downloaded)
                self.assertIn("[REDACTED]", downloaded)
                self.assertIn(secret.encode(), originals[artifact["name"]])
        self.assertEqual({path.name: path.read_bytes() for path in folder.iterdir()}, originals)
        self.assertEqual(self.service.story_draft_detail(job["id"])["artifacts"], detail["artifacts"])

    def test_http_detail_and_not_found_routes_are_traced_without_model_calls(self):
        job = self.job()
        self.legacy_files(job)
        server = create_server(self.service, port=0)
        server.daemon_threads = False
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(3)))
        port = server.server_address[1]
        def get(path):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
            try:
                conn.request("GET", path)
                response = conn.getresponse()
                return response.status, json.loads(response.read())
            finally:
                conn.close()
        with patch("pianchang.app_service.StoryRunner.build", side_effect=AssertionError("No provider use")) as build:
            status, detail = get("/api/story-drafts/" + job["id"])
            self.assertEqual(status, 200)
            self.assertEqual(detail["id"], job["id"])
            self.assertEqual(detail["validation"]["status"], "FAIL")
            for identifier in ("invalid", "e" * 32):
                self.assertEqual(get("/api/story-drafts/" + identifier)[0], 404)
        build.assert_not_called()
        traces = self.service.tracer.list_traces(kind="http")
        self.assertTrue(traces)
        self.assertTrue(any(s["name"] == "story.draft.read" for trace in traces
                            for s in self.service.tracer.get_trace(trace["trace_id"])["spans"]))


if __name__ == "__main__":
    unittest.main()
