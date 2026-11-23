"""Local HTTP boundary tests. FakeService never calls a model or reads .env."""
import copy
import base64
import hashlib
import http.client
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from pianchang.acceptance import create_contract, file_sha256
from pianchang.app_service import AppService, read_json, write_json
from pianchang.media_tools import MediaError
from pianchang.story_spec import digest_json
from pianchang.web_server import create_server


class FakeService:
    def __init__(self, root):
        self.root = Path(root)
        self.secret = "sk-offline-test-secret-never-return-to-browser"
        self.calls = []
        self.asset = self.root / "registered-video.mp4"
        self.asset.write_bytes(b"0123456789abcdef")
        self.project = {"id": "project-one", "title": "测试短剧", "shots": []}

    def sanitize(self, value):
        if isinstance(value, str):
            return value.replace(self.secret, "[REDACTED]")
        if isinstance(value, dict):
            return {key: self.sanitize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.sanitize(item) for item in value]
        return value

    def bootstrap(self):
        return {"projects": [copy.deepcopy(self.project)], "api_key_configured": True}

    def project_detail(self, pid):
        if pid == "explode":
            raise RuntimeError("provider failed with " + self.secret)
        if pid == "invalid":
            raise ValueError("invalid request included " + self.secret)
        if pid != self.project["id"]:
            raise KeyError(pid)
        return copy.deepcopy(self.project)

    def list_jobs(self):
        return [{"id": "existing-job", "status": "SUCCEEDED"}]

    def submit(self, action, pid, body, idempotency_key=None):
        self.calls.append(("submit", action, pid, copy.deepcopy(body), idempotency_key))
        return {"id": "queued-job", "status": "QUEUED", "action": action}

    def select(self, pid, body):
        self.calls.append(("select", pid, copy.deepcopy(body)))
        return {"id": pid, "selected": body.get("candidate_id")}

    def resolve_asset(self, assetid):
        self.calls.append(("asset", assetid))
        if assetid != "registered-video":
            raise KeyError(assetid)
        return self.asset


class WebHttpTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = FakeService(self.directory.name)
        self.server = create_server(self.service, host="127.0.0.1", port=0)
        # Wait for final request Trace writes before deleting the Windows temp DB.
        self.server.daemon_threads = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.host = f"127.0.0.1:{self.port}"
        self.origin = f"http://{self.host}"
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.directory.cleanup()

    def request(self, method, path, *, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def post(self, path, payload=None, *, headers=None):
        request_headers = {"Origin": self.origin, "Content-Type": "application/json"}
        request_headers.update(headers or {})
        return self.request("POST", path, body=json.dumps(payload or {}, ensure_ascii=False).encode("utf-8"),
                            headers=request_headers)

    def json_body(self, response):
        self.assertIn("application/json", response[1].get("Content-Type", ""))
        return json.loads(response[2].decode("utf-8"))

    def assert_rejected_without_submission(self, response, allowed=(400, 403)):
        self.assertIn(response[0], allowed)
        self.assertFalse(any(call[0] in {"submit", "select"} for call in self.service.calls))

    def test_server_binds_only_loopback(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        with self.assertRaises(ValueError):
            unwanted = create_server(self.service, host="0.0.0.0", port=0)
            unwanted.server_close()

    def test_continue_button_posts_frozen_commission_through_existing_message_endpoint(self):
        data={"scope":{"project":"a"*16},"role":"video","text":"继续当前制作委托","mode":"work",
              "message_id":"b"*32,"continue_commission":"c"*32}
        with patch("pi_bridge.service.get_sessions") as sessions:
            submit=sessions.return_value.submit
            submit.return_value={"job":{"id":"d"*32}}
            response=self.post("/api/pi/messages",data)
            self.assertEqual(response[0],202)
            submit.assert_called_once_with(data["scope"],"video",data["text"],data["message_id"],
                                           mode="work",continue_commission=data["continue_commission"])

    def test_bootstrap_contains_public_state_without_secret(self):
        response = self.request("GET", "/api/bootstrap")
        self.assertEqual(response[0], 200)
        payload = self.json_body(response)
        self.assertTrue(payload["api_key_configured"])
        self.assertEqual(payload["projects"][0]["id"], "project-one")
        self.assertNotIn(self.service.secret.encode(), response[2])

    def test_human_shot_approval_requires_local_post_and_preserves_candidate_identity(self):
        path = "/api/projects/project-one/studio/shots/approve"
        body = {"request_id": "a"*32, "shot_id": "S003", "media_sha256": "b"*64, "report_sha256": "c"*64}
        with patch("pianchang.shot_approval.approve_shot", return_value={"delivery": {"delivered": True}}) as approve:
            self.assertNotEqual(self.request("GET", path)[0], 200)
            self.assertEqual(self.post(path, body, headers={"Origin": "https://other.example"})[0], 403)
            approve.assert_not_called()
            reply = self.post(path, body)
            self.assertEqual(reply[0], 200)
            self.assertTrue(self.json_body(reply)["delivery"]["delivered"])
            approve.assert_called_once_with(self.service, "project-one", body)
            self.assertFalse(self.service.calls)

    def test_feedback_receipts_are_read_only_and_require_exact_job_ids(self):
        job_id = "a"*32
        status, _, body = self.request("GET","/api/feedback-watch?job="+job_id)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["jobs"][job_id]["status"], "untracked")
        self.assertEqual(self.request("GET","/api/feedback-watch?job=wrong")[0], 400)
        self.assertEqual(self.service.calls, [])

    def test_get_project_and_jobs_dispatch_to_read_only_service(self):
        project = self.request("GET", "/api/projects/project-one")
        self.assertEqual(project[0], 200)
        self.assertIn("project-one", project[2].decode("utf-8"))
        jobs = self.request("GET", "/api/jobs")
        self.assertEqual(jobs[0], 200)
        self.assertEqual(self.json_body(jobs)["jobs"], self.service.list_jobs())
        self.assertFalse(self.service.calls)

    def test_valid_story_post_dispatches_with_idempotency_key(self):
        payload = {"idea": "两个成年人交接一把钥匙。"}
        response = self.post("/api/projects", payload, headers={"Idempotency-Key": "story-request-001"})
        self.assertEqual(response[0], 202)
        self.assertEqual(self.json_body(response)["job"]["id"], "queued-job")
        self.assertEqual(self.service.calls, [("submit", "story", None, payload, "story-request-001")])

    def test_project_actions_dispatch_without_exposing_filesystem_paths(self):
        for action in ("generate", "resume", "check", "export"):
            with self.subTest(action=action):
                self.service.calls.clear()
                payload = {"shot_id": "S002", "candidate_id": "candidate-one"}
                response = self.post(f"/api/projects/project-one/{action}", payload)
                self.assertEqual(response[0], 202)
                self.assertEqual(self.service.calls, [("submit", action, "project-one", payload, None)])

    def test_select_is_an_explicit_post(self):
        payload = {"shot_id": "S002", "candidate_id": "candidate-one"}
        response = self.post("/api/projects/project-one/select", payload)
        self.assertEqual(response[0], 200)
        self.assertEqual(self.json_body(response)["project"]["id"], "project-one")
        self.assertEqual(self.service.calls, [("select", "project-one", payload)])

    def test_get_cannot_start_a_generation(self):
        response = self.request("GET", "/api/projects/project-one/generate")
        self.assert_rejected_without_submission(response, allowed=(404, 405))

    def test_cross_origin_post_cannot_submit_work(self):
        for origin in ("https://example.com", "null", "http://127.0.0.1.evil.example",
                       f"https://{self.host}", "http://127.0.0.1:1"):
            with self.subTest(origin=origin):
                response = self.post("/api/projects", {"idea": "x"}, headers={"Origin": origin})
                self.assert_rejected_without_submission(response)

    def test_missing_origin_post_is_rejected_even_with_json(self):
        response = self.request("POST", "/api/projects", body=b'{"idea":"x"}',
                                headers={"Content-Type": "application/json"})
        self.assert_rejected_without_submission(response)

    def test_host_spoofing_cannot_use_a_matching_local_origin(self):
        for host in ("evil.example", f"127.0.0.1.evil.example:{self.port}", "127.0.0.1:1"):
            with self.subTest(host=host):
                response = self.post("/api/projects", headers={"Host": host})
                self.assert_rejected_without_submission(response)

    def test_plain_text_post_is_rejected_before_service_dispatch(self):
        response = self.post("/api/projects", {"idea": "x"}, headers={"Content-Type": "text/plain"})
        self.assert_rejected_without_submission(response, allowed=(400, 415))

    def test_json_content_type_with_charset_is_accepted(self):
        response = self.post("/api/projects", {"idea": "x"},
                             headers={"Content-Type": "application/json; charset=utf-8"})
        self.assertEqual(response[0], 202)

    def test_invalid_json_and_non_object_bodies_are_rejected(self):
        for body in (b"{broken", b"[]", b"null", b'"text"'):
            with self.subTest(body=body):
                response = self.request("POST", "/api/projects", body=body,
                                        headers={"Origin": self.origin, "Content-Type": "application/json"})
                self.assert_rejected_without_submission(response)

    def test_oversized_json_is_rejected_without_dispatch(self):
        response = self.post("/api/projects", {"idea": "x" * (32 * 1024)})
        self.assert_rejected_without_submission(response, allowed=(400, 413))

    def test_unknown_mutation_route_does_not_dispatch(self):
        response = self.post("/api/projects/project-one/delete-everything")
        self.assert_rejected_without_submission(response, allowed=(404, 405))

    def test_unexpected_error_uses_generic_message_without_secret(self):
        response = self.request("GET", "/api/projects/explode")
        self.assertEqual(response[0], 500)
        self.assertNotIn(self.service.secret.encode(), response[2])
        self.assertNotIn(b"provider failed with", response[2])
        self.json_body(response)

    def test_expected_error_is_sanitized(self):
        response = self.request("GET", "/api/projects/invalid")
        self.assertEqual(response[0], 400)
        self.assertNotIn(self.service.secret.encode(), response[2])
        self.json_body(response)

    def test_unknown_project_returns_not_found(self):
        response = self.request("GET", "/api/projects/missing")
        self.assertEqual(response[0], 404)
        self.json_body(response)

    def test_static_allowlist_never_serves_dotenv_or_project_python(self):
        for path in ("/.env", "/.env.example", "/../.env", "/%2e%2e/.env", "/bailian_video.py",
                     "/outputs/", "/web/../.env", "/fixtures/story_project.json"):
            with self.subTest(path=path):
                response = self.request("GET", path)
                self.assertIn(response[0], (400, 404))
                self.assertNotIn(b"DASHSCOPE_API_KEY=", response[2])

    def test_fixed_index_page_is_served_as_html(self):
        response = self.request("GET", "/")
        self.assertEqual(response[0], 200)
        self.assertIn("text/html", response[1].get("Content-Type", ""))
        self.assertIn(b"<html", response[2].lower())
        policy = response[1]["Content-Security-Policy"]
        for tag, directive in ((b"script", "script-src"), (b"style", "style-src")):
            content = response[2].split(b"<" + tag + b">", 1)[1].split(b"</" + tag + b">", 1)[0]
            digest = base64.b64encode(hashlib.sha256(content).digest()).decode("ascii")
            self.assertIn(directive + " 'self' 'sha256-" + digest + "'", policy)
        self.assertNotIn("'unsafe-inline'", policy)
        self.assertIn(b'<html data-video-detail="true"', response[2])
        self.assertNotIn(b'<script src="/workbench.js"', response[2])
        self.assertLess(response[2].index(b'id="stage"'), response[2].index(b"<script>"))

    def test_video_detail_returns_scoped_dto_and_records_it_before_sending(self):
        from trace_system import get_tracer
        pid = "1"*16
        fixture = {"project_id": pid, "projection": "video", "video_plan_error": self.service.secret}
        url = "/api/projects/" + pid + "/studio/video-detail"
        with patch("pianchang.studio_flow.studio_video_detail", return_value=fixture) as read:
            response = self.request("GET", url)
            self.assertEqual(response[0], 200)
            read.assert_called_once_with(self.service, pid)
            data = self.json_body(response)
            self.assertEqual(data["video_plan_error"], "[REDACTED]")
            tracer = get_tracer(self.service.root / "outputs/traces")
            records = tracer.get_events(response[1]["X-Trace-Id"])
            body = next(row for row in records if row["name"] == "http.response.body")
            self.assertEqual(tracer.resolve(body["data"])["body"], data)
            self.assertEqual(self.request("GET", url + "?project=" + pid)[0], 400)
            self.assertEqual(read.call_count, 1)

    def test_registered_asset_is_served_without_accepting_a_path_parameter(self):
        response = self.request("GET", "/assets/registered-video?path=../../.env")
        self.assertEqual(response[0], 200)
        self.assertEqual(response[2], self.service.asset.read_bytes())
        self.assertIn("video/mp4", response[1].get("Content-Type", ""))
        self.assertEqual(self.service.calls, [("asset", "registered-video")])

    def test_unknown_or_traversal_asset_ids_cannot_resolve_files(self):
        for path in ("/assets/missing", "/assets/../.env", "/assets/%2e%2e%2f.env",
                     "/assets/E%3A%5Csecret.txt"):
            with self.subTest(path=path):
                response = self.request("GET", path)
                self.assertIn(response[0], (400, 404))
                self.assertNotIn(b"DASHSCOPE_API_KEY=", response[2])

    def test_asset_head_returns_length_without_body(self):
        response = self.request("HEAD", "/assets/registered-video")
        self.assertEqual(response[0], 200)
        self.assertEqual(int(response[1]["Content-Length"]), 16)
        self.assertEqual(response[2], b"")

    def test_asset_range_supports_video_seek(self):
        response = self.request("GET", "/assets/registered-video", headers={"Range": "bytes=2-5"})
        self.assertEqual(response[0], 206)
        self.assertEqual(response[2], b"2345")
        self.assertEqual(response[1]["Content-Range"], "bytes 2-5/16")
        self.assertEqual(int(response[1]["Content-Length"]), 4)

    def test_asset_open_ended_range_and_suffix_range(self):
        for value, expected in (("bytes=12-", b"cdef"), ("bytes=-3", b"def")):
            with self.subTest(range=value):
                response = self.request("GET", "/assets/registered-video", headers={"Range": value})
                self.assertEqual(response[0], 206)
                self.assertEqual(response[2], expected)

    def test_asset_unsatisfiable_range_returns_416(self):
        response = self.request("GET", "/assets/registered-video", headers={"Range": "bytes=100-200"})
        self.assertEqual(response[0], 416)
        self.assertEqual(response[1].get("Content-Range"), "bytes */16")


class WebServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.secret = "sk-offline-provider-secret-never-expose"
        self.video_dir = self.root / "outputs" / "videos"
        (self.root / ".env").write_text(
            f"DASHSCOPE_API_KEY={self.secret}\nBAILIAN_VIDEO_OUTPUT_DIR={self.video_dir.as_posix()}\n",
            encoding="utf-8")
        env_patch = patch.dict(os.environ, {}, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        fixtures = self.root / "fixtures"
        fixtures.mkdir()
        project_path = Path(__file__).resolve().parents[1] / "fixtures" / "story_project.json"
        self.project = read_json(project_path)
        self.project_path = fixtures / "story_project.json"
        write_json(self.project_path, self.project)
        self.service = AppService(root=self.root)
        self.addCleanup(self.service.close)
        self.pid = next(iter(self.service._discover()))

    def test_generated_candidate_preview_is_bound_to_manifest_and_project(self):
        folder = self.video_dir / "offline-candidate"
        folder.mkdir(parents=True)
        media = folder / "video.mp4"
        media.write_bytes(b"explicit offline candidate bytes")
        manifest = {"metadata": {"project_sha256": digest_json(self.project), "shot_id": "S001"},
                    "task_id": "offline", "status": "SUCCEEDED", "download_sha256": file_sha256(media)}
        write_json(folder / "job.json", manifest)
        detail = self.service.project_detail(self.pid)
        self.assertFalse(detail["preview_gate"]["ready"])
        self.assertIsNone(detail["export"])
        shot = next(s for s in detail["shots"] if s["id"] == "S001")
        token = shot["candidates"][0]["video_url"].rsplit("/", 1)[1]
        self.assertEqual(self.service.resolve_asset(token), media.resolve())
        write_json(folder / "job.json", {**manifest, "task_id": "different-offline-task"})
        with self.assertRaises(KeyError):
            self.service.resolve_asset(token)
        write_json(folder / "job.json", manifest)
        write_json(self.project_path, {**self.project, "title": "Updated offline project"})
        with self.assertRaises(KeyError):
            self.service.resolve_asset(token)

    def test_sidebar_includes_readable_project_content_without_opening_production(self):
        source = self.project
        shot = source["shots"][0]
        entity_id = shot["scene_id"]
        entity = source["entities"][entity_id]
        entity["canonical"]["layout"] = "Offline scene: west door. " + self.secret
        write_json(self.project_path, source)
        with patch.object(self.service, "project_detail", side_effect=AssertionError("read-only sidebar")), \
             patch.object(self.service, "_candidates", side_effect=AssertionError("no media lookup")):
            selected = next(p for p in self.service.bootstrap(include_jobs=False)["projects"] if p["id"] == self.pid)
        overview = selected["opening"]
        self.assertEqual(selected["shot_count"], len(overview["shots"]))
        self.assertEqual(overview["shots"][0], {k: shot[k] for k in ("id", "description", "duration_s", "entity_ids", "scene_id")})
        self.assertEqual(next(e for e in overview["entities"] if e["id"] == entity_id),
                         {"id": entity_id, "name": entity["canonical"]["name"], "kind": "scene",
                          "description": "Offline scene: west door. [REDACTED]"})
        self.assertNotIn(self.secret, json.dumps(overview))

    def test_sidebar_video_progress_targets_current_commission_in_same_project(self):
        def job(job_id, status, **fields):
            return {"id": job_id, "project_id": self.pid, "kind": "studio_video",
                    "status": status, "result": {"private": self.secret}, **fields}
        def opening_job():
            projects = self.service.bootstrap(include_jobs=False)["projects"]
            return next(p for p in projects if p["id"] == self.pid)["opening"]["video_job"]
        self.service.state["jobs"].extend([
            job("older", "succeeded", message_id="old receipt"),
            job("current", "running", message=self.secret),
            job("finished", "failed", message_id="newer receipt"),
            job("unsubmitted", "failed"),
            job("other story", "running", project_id="another project"),
            job("other task", "running", kind="studio_assets"),
        ])
        self.assertEqual(opening_job(), {"id": "current", "project_id": self.pid, "kind": "studio_video",
                                         "status": "running", "message": "[REDACTED]"})
        self.service.state["jobs"][1]["status"] = "failed"
        self.assertEqual(opening_job()["id"], "finished")

    def waiting_worker(self):
        # Durable Trace flushes on Windows are not a two-second completion SLA.
        # Keep ordering assertions while giving the released worker time to persist.
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def execute(job, body, project_hash):
            self.service._update(job, status="running")
            started.set()
            release.wait(timeout=30)
            self.service._update(job, status="succeeded", result={"offline": True})
            finished.set()
        return execute, started, release, finished

    def test_same_idempotency_key_reuses_one_job_after_completion_and_restart(self):
        execute, started, release, finished = self.waiting_worker()
        with patch.object(self.service, "_execute", side_effect=execute) as worker:
            first = self.service.submit("story", None, {"idea": "same request"}, "stable-key")
            self.assertTrue(started.wait(timeout=2))
            duplicate = self.service.submit("story", None, {"idea": "same request"}, "stable-key")
            self.assertEqual(first["id"], duplicate["id"])
            release.set()
            self.assertTrue(finished.wait(timeout=15))
            completed_retry = self.service.submit("story", None, {"idea": "same request"}, "stable-key")
            self.assertEqual(first["id"], completed_retry["id"])
            self.assertEqual(worker.call_count, 1)
        self.service.close()
        restarted = AppService(root=self.root)
        self.addCleanup(restarted.close)
        with patch.object(restarted, "_execute") as worker:
            retry = restarted.submit("story", None, {"idea": "same request"}, "stable-key")
            self.assertEqual(first["id"], retry["id"])
            worker.assert_not_called()

    def test_same_key_with_changed_payload_is_rejected(self):
        execute, started, release, finished = self.waiting_worker()
        with patch.object(self.service, "_execute", side_effect=execute) as worker:
            self.service.submit("story", None, {"idea": "first"}, "stable-key")
            self.assertTrue(started.wait(timeout=2))
            with self.assertRaises(ValueError):
                self.service.submit("story", None, {"idea": "different"}, "stable-key")
            self.assertEqual(worker.call_count, 1)
            release.set()
            self.assertTrue(finished.wait(timeout=15))

    def test_concurrent_identical_clicks_enqueue_only_one_worker(self):
        execute, started, release, finished = self.waiting_worker()
        with patch.object(self.service, "_execute", side_effect=execute) as worker:
            with ThreadPoolExecutor(max_workers=8) as callers:
                jobs = list(callers.map(lambda _: self.service.submit("story", None,
                                     {"idea": "double click"}, "one-operation"), range(8)))
            self.assertTrue(started.wait(timeout=2))
            self.assertEqual(len({j["id"] for j in jobs}), 1)
            self.assertEqual(worker.call_count, 1)
            self.assertEqual(len(self.service.list_jobs()), 1)
            release.set()
            self.assertTrue(finished.wait(timeout=15))

    def test_without_key_identical_active_job_is_reused_and_prompt_change_is_rejected(self):
        execute, started, release, finished = self.waiting_worker()
        body = {"shot_id": "S002", "prompt": "original prompt"}
        with patch.object(self.service, "_execute", side_effect=execute) as worker:
            first = self.service.submit("generate", self.pid, body)
            self.assertTrue(started.wait(timeout=2))
            duplicate = self.service.submit("generate", self.pid, body)
            self.assertEqual(first["id"], duplicate["id"])
            with self.assertRaises(ValueError):
                self.service.submit("generate", self.pid, {"shot_id": "S002", "prompt": "changed prompt"})
            self.assertEqual(worker.call_count, 1)
            release.set()
            self.assertTrue(finished.wait(timeout=15))

    def test_distinct_jobs_are_serialized(self):
        first_started, second_started, release, finished = (threading.Event() for _ in range(4))
        self.addCleanup(release.set)

        def execute(job, body, project_hash):
            self.service._update(job, status="running")
            if body["shot_id"] == "S001":
                first_started.set()
                release.wait(timeout=30)
            else:
                second_started.set()
            self.service._update(job, status="succeeded")
            if body["shot_id"] == "S002":
                finished.set()

        with patch.object(self.service, "_execute", side_effect=execute):
            self.service.submit("generate", self.pid, {"shot_id": "S001"}, "first")
            self.assertTrue(first_started.wait(timeout=2))
            second = self.service.submit("generate", self.pid, {"shot_id": "S002"}, "second")
            self.assertEqual(second["status"], "queued")
            self.assertFalse(second_started.is_set())
            release.set()
            self.assertTrue(finished.wait(timeout=15))

    def test_restart_marks_uncertain_jobs_interrupted_without_replaying_them(self):
        state = {"jobs": [
            {"id": "was-running", "status": "running", "kind": "story"},
            {"id": "was-queued", "status": "queued", "kind": "story"},
            {"id": "completed", "status": "succeeded", "kind": "story"}],
            "selections": {}, "reviews": {}, "exports": {}}
        self.service.close()
        write_json(self.root / "outputs" / "web" / "state.json", state)
        with patch.object(AppService, "_execute") as worker:
            restarted = AppService(root=self.root)
            self.addCleanup(restarted.close)
            actual = {j["id"]: j["status"] for j in restarted.list_jobs()}
            self.assertEqual(actual, {"was-running": "interrupted", "was-queued": "interrupted", "completed": "succeeded"})
            worker.assert_not_called()
            persisted = read_json(restarted.state_dir / "state.json")
            self.assertEqual({j["id"]: j["status"] for j in persisted["jobs"]}, actual)

    def test_second_service_cannot_take_over_an_active_state_directory(self):
        before = (self.service.state_dir / "state.json").read_bytes()
        with self.assertRaises(ValueError):
            duplicate = AppService(root=self.root)
            duplicate.close()
        self.assertEqual((self.service.state_dir / "state.json").read_bytes(), before)

    def test_registered_asset_is_confined_to_outputs_and_unknown_ids_fail(self):
        asset = self.video_dir / "image.png"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(b"trusted output")
        url = self.service.asset(asset)
        self.assertEqual(self.service.resolve_asset(url.rsplit("/", 1)[1]), asset.resolve())
        with self.assertRaises(KeyError):
            self.service.resolve_asset("../../.env")
        outside = self.root / "not-an-output.json"
        outside.write_text('{"secret": "private"}', encoding="utf-8")
        with self.assertRaises(ValueError):
            self.service.asset(outside)
        with self.assertRaises(ValueError):
            self.service.asset(self.root / ".env")

    def test_asset_token_rejects_changed_bytes_even_if_size_and_mtime_are_preserved(self):
        asset = self.video_dir / "clip.mp4"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(b"version-one")
        token = self.service.asset(asset).rsplit("/", 1)[1]
        stat = asset.stat()
        asset.write_bytes(b"version-two")
        os.utime(asset, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        with self.assertRaises(KeyError):
            self.service.resolve_asset(token)

    def test_hidden_or_executable_outputs_are_not_registered(self):
        for relative in (".private/data.json", "tool.py", "active.html"):
            with self.subTest(path=relative):
                path = self.root / "outputs" / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("private", encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.service.asset(path)

    def test_review_audio_is_hash_bound_and_stays_within_output_roots(self):
        import wave
        audio = self.root / "outputs" / "offline-review.wav"
        audio.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(audio), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(b"\0\0" * 160)
        token = self.service.asset(audio, expected_sha256=file_sha256(audio)).rsplit("/", 1)[1]
        self.assertEqual(self.service.resolve_asset(token), audio.resolve())
        with self.assertRaises(ValueError):
            self.service.asset(audio, expected_sha256="0"*64)
        outside = self.root / "private-review.wav"
        outside.write_bytes(audio.read_bytes())
        with self.assertRaises(ValueError):
            self.service.asset(outside)

    def test_background_error_and_persisted_state_are_sanitized(self):
        completed = threading.Event()
        execute = self.service._execute

        def wrapped(*args):
            try:
                execute(*args)
            finally:
                completed.set()

        with patch.object(self.service, "_build_story", side_effect=ValueError("provider response " + self.secret)), \
                patch.object(self.service, "_execute", side_effect=wrapped):
            self.service.submit("story", None, {"idea": "failure test"}, "failed-request")
            self.assertTrue(completed.wait(timeout=3))
        jobs = self.service.list_jobs()
        self.assertEqual(jobs[0]["status"], "failed")
        self.assertNotIn(self.secret, json.dumps(jobs))
        self.assertNotIn(self.secret, (self.service.state_dir / "state.json").read_text(encoding="utf-8"))
        self.assertTrue(all(not key.startswith("_") for key in jobs[0]))

    def test_project_changed_while_queued_is_rejected_before_provider_use(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original_execute = self.service._execute

        def execute(*args):
            started.set()
            release.wait(timeout=30)
            try:
                original_execute(*args)
            finally:
                finished.set()

        with patch.object(self.service, "_execute", side_effect=execute), patch("pianchang.app_service.VideoRunner") as provider:
            self.service.submit("generate", self.pid, {"shot_id": "S002"}, "fixed-project-version")
            self.assertTrue(started.wait(timeout=2))
            changed = copy.deepcopy(self.project)
            changed["title"] = "Changed after enqueue"
            write_json(self.project_path, changed)
            release.set()
            self.assertTrue(finished.wait(timeout=15))
            provider.assert_not_called()
        self.assertEqual(self.service.list_jobs()[0]["status"], "failed")

    def test_wan27_check_uses_version_correct_resolution_in_contract(self):
        for resolution, ratio, expected in (("1080P", "16:9", (1920, 1080)),
                                             ("720P", "4:3", (1104, 832))):
            with self.subTest(resolution=resolution, ratio=ratio):
                candidate = {"id": "offline-candidate", "shot_id": "S002", "_video": self.root / "fake.mp4",
                             "_job": {"request": {"model": "wan2.7-t2v", "parameters": {"resolution": resolution, "ratio": ratio}},
                                      "download_sha256": "offline-sha"}}
                job = {"id": "offline-check", "project_id": self.pid}
                with patch.object(self.service, "_candidates", return_value=[candidate]), \
                        patch("pianchang.app_service.create_contract", side_effect=ValueError("stop before inspect")) as create:
                    with self.assertRaises(ValueError):
                        self.service._check(job, self.project, {"shot_id": "S002", "candidate_id": candidate["id"]})
                    self.assertEqual((create.call_args.kwargs["width"], create.call_args.kwargs["height"]), expected)

    def test_check_recovers_from_partial_or_invalid_cached_inspection_in_new_attempt(self):
        for has_pointer in (False, True):
            with self.subTest(cached_pointer=has_pointer):
                video = self.video_dir / f"check-source-{has_pointer}.mp4"
                video.parent.mkdir(parents=True, exist_ok=True)
                video.write_bytes(f"offline check source {has_pointer}".encode())
                candidate = {"id": f"check-candidate-{has_pointer}", "shot_id": "S002", "_video": video,
                             "_job": {"request": {"model": "wan2.6-t2v", "parameters": {"size": "720*1280"}},
                                      "download_sha256": file_sha256(video)}}
                contract = create_contract(self.project, "S002", width=720, height=1280)
                folder = self.service.state_dir / "reviews" / digest_json([contract, file_sha256(video)])[:24]
                old = folder / "attempts" / "old-interrupted-job" / "inspection"
                old.mkdir(parents=True)
                partial_frame = old / "frame-001.jpg"
                partial_frame.write_bytes(b"old partial evidence must remain unchanged")
                write_json(old.parent / "observations.json", {"old_observations": True})
                pointer = folder / "current-inspection.json"
                if has_pointer:
                    write_json(pointer, {"directory": str(old)})
                job = {"id": f"check-retry-{has_pointer}", "project_id": self.pid}
                expected_directory = folder / "attempts" / job["id"] / "inspection"

                def inspect(media, directory, **kwargs):
                    directory = Path(directory)
                    if directory == old:
                        raise MediaError("cached inspection is incomplete or its frame hash changed")
                    directory.mkdir(parents=True)
                    frame = directory / "frame-001.jpg"
                    frame.write_bytes(b"new offline evidence frame")
                    manifest = {"schema_version": "media-inspection/1", "media_path": str(media),
                                "media_sha256": file_sha256(media), "bytes": Path(media).stat().st_size,
                                "metadata": {"duration_s": 8.0, "width": 720, "height": 1280},
                                "decode": {"status": "PASS"},
                                "frames": [{"id": "F001", "time_s": 0.0, "path": str(frame), "sha256": file_sha256(frame)}],
                                "sampling": {"interval_s": 1, "max_frames": 16},
                                "inspection_status": "PASS", "tool_versions": {"mock": "offline"},
                                "manifest_path": str(directory / "inspection.json")}
                    write_json(directory / "inspection.json", manifest)
                    return manifest

                report = {"schema_version": "shot-acceptance-report/1", "status": "UNKNOWN",
                          "task_complete": False, "results": []}
                with patch.object(self.service, "_candidates", return_value=[candidate]), \
                        patch("pianchang.app_service.inspect_media", side_effect=inspect) as inspector, \
                        patch("pianchang.app_service.observe", return_value={"new_observations": True}) as observer, \
                        patch("pianchang.app_service.run_checks", return_value=report):
                    result = self.service._check(job, self.project, {"shot_id": "S002", "candidate_id": candidate["id"], "with_model": True})
                directories = [Path(call.args[1]) for call in inspector.call_args_list]
                self.assertEqual(directories, ([old] if has_pointer else []) + [expected_directory])
                observer.assert_called_once()
                self.assertEqual(result["acceptance_status"], "UNKNOWN")
                self.assertFalse(result["task_complete"])
                self.assertEqual(read_json(pointer)["directory"], str(expected_directory))
                saved = self.service.state["reviews"][digest_json([digest_json(self.project), candidate["id"]])]
                self.assertEqual(saved["inspection"], str(expected_directory / "inspection.json"))
                self.assertEqual(read_json(expected_directory.parent / "observations.json"), {"new_observations": True})
                self.assertEqual(read_json(old.parent / "observations.json"), {"old_observations": True})
                self.assertEqual(partial_frame.read_bytes(), b"old partial evidence must remain unchanged")

    def test_legacy_cached_export_cannot_skip_published_acceptance(self):
        from pianchang.preview_access import PreviewBlocked
        prior=self.service.state_dir/"exports/old/preview.mp4"
        prior.parent.mkdir(parents=True);prior.write_bytes(b"old unaccepted preview retained")
        job={"id":"offline-export-rejection","project_id":self.pid}
        with patch("pianchang.episode_export.export_preview") as renderer:
            with self.assertRaises(PreviewBlocked):self.service._export(job,self.project)
            renderer.assert_not_called()
        self.assertEqual(prior.read_bytes(),b"old unaccepted preview retained")
        self.assertNotIn(self.pid,self.service.state["exports"])


if __name__ == "__main__":
    unittest.main()
