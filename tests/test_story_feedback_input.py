import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from pianchang.app_service import read_json, write_json
from pianchang.story_spec import digest_json
from pianchang.web_server import create_server
from tools.workbench_loop_input import answer_request, human_request
from tools.workbench_loop_watch import _folder, _signature
from trace_system import get_tracer, use_context


class StoryFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.tracer = get_tracer(self.root / "traces")
        self.enterContext(use_context(None, tracer=self.tracer))
        self.enterContext(self.tracer.trace("test.story.feedback.offline"))
        self.job = {"id": "a" * 32, "kind": "studio_script", "project_id": None,
                    "trace_id": "c" * 32, "status": "interrupted"}
        self.fields = [{"name": "answer", "label": "片名", "type": "text", "max_length": 500}]

    def question(self):
        return human_request(self.job, "script_creative_input", "source", "补充片名", "请提供片名。",
                             fields=self.fields)

    def publish(self, request):
        write_json(self.root / "outputs/web/state.json", {"jobs": [self.job]})
        key = _signature(self.job)
        record = {"status": "needs_input", "human_request": request}
        write_json(_folder(self.root) / "state.json", {"latest": {self.job["id"]: key}, "events": {key: record}})
        return {"request_id": request["id"], "job_id": self.job["id"], "answers": {"answer": "  归途  "}}

    def test_http_receipt_replays_after_question_closes_only_for_original_draft(self):
        self.job["story_job_id"] = "f" * 32  # The initial script's own job is its authoritative draft.
        request = {**self.question(), "story_id": self.job["id"], "session_id": "e" * 32}
        body = self.publish(request)
        server = create_server(SimpleNamespace(root=self.root, tracer=self.tracer), port=0)
        server.daemon_threads = False
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(3)))
        port = server.server_address[1]

        def post(scope, data):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
            try:
                conn.request("POST", "/api/" + scope + "/feedback-input", json.dumps(data),
                             {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"})
                response = conn.getresponse()
                return response.status, json.loads(response.read())
            finally:
                conn.close()

        draft = "story-drafts/" + self.job["id"]
        self.assertEqual(post("projects/" + "a" * 16, body)[0], 404)
        self.assertEqual(post("story-drafts/" + self.job["story_job_id"], body)[0], 404)
        self.assertEqual(post(draft, {**body, "answers": {"answer": "x" * 501}})[0], 400)
        self.assertEqual(post(draft, body), (202, {"request_id": request["id"], "status": "accepted", "job_id": self.job["id"]}))
        target = _folder(self.root) / "answers" / (request["id"] + ".json")
        saved = read_json(target)
        self.assertEqual(saved["answers"], {"answer": "归途"})
        self.assertEqual(saved["request"], request)
        self.assertIsNone(saved["project_id"])
        self.assertEqual(saved["story_job_id"], self.job["id"])
        before = target.read_bytes()
        write_json(_folder(self.root) / "state.json", {"latest": {}, "events": {}})
        self.assertEqual(post(draft, {**body, "answers": saved["answers"]})[0], 202)
        self.assertEqual(post(draft, {**body, "answers": {"answer": "另一部"}})[0], 400)
        self.assertEqual(post("story-drafts/" + self.job["story_job_id"], body)[0], 404)
        self.assertEqual(target.read_bytes(), before)

    def test_followup_uses_its_persisted_story_and_rejects_stale_or_misaddressed_requests(self):
        owner = "d" * 32
        self.job.update(kind="pi_script", story_job_id=owner)
        self.fields = [{"name": "answer", "label": "结局", "type": "select",
                        "options": [{"value": v, "label": v} for v in ("雨夜", "清晨")]}]
        request = self.question()
        self.assertEqual(request["story_job_id"], owner)
        body = self.publish(request)
        body["answers"] = {"answer": "雨夜"}
        with self.assertRaises(KeyError):
            answer_request(self.root, None, body, story_id=self.job["id"])
        with self.assertRaises(ValueError):
            answer_request(self.root, None, {**body, "answers": {"answer": "阴天"}}, story_id=owner)
        for forged in ({"story_id": "b" * 32}, {"job_id": "c" * 32}, {"kind": "review_retake_limit"}):
            with self.subTest(forged=forged), self.assertRaises(ValueError):
                self.publish({**request, **forged})
                answer_request(self.root, None, body, story_id=owner)
        self.job.update(trace_id="b" * 32)
        next_question = self.question()
        new_body = self.publish(next_question)
        with self.assertRaises(ValueError):
            answer_request(self.root, None, body, story_id=owner)
        self.job.update(kind="pi_video")
        write_json(self.root / "outputs/web/state.json", {"jobs": [self.job]})
        with self.assertRaises(KeyError):
            answer_request(self.root, None, new_body, story_id=owner)
        self.job.update(kind="pi_script")
        self.publish(next_question)
        self.assertEqual(answer_request(self.root, None, {**new_body, "answers": {"answer": "清晨"}},
                                        story_id=owner)["status"], "accepted")

    def test_published_story_uses_project_and_keeps_existing_question_ids(self):
        project_id, story_id = "b" * 16, self.job["id"]
        self.job.update(project_id=project_id)
        request = self.question()
        legacy_id = digest_json([project_id, self.job["id"], self.job["trace_id"],
                                 "script_creative_input", "source", self.fields])
        self.assertEqual(request["id"], legacy_id)
        body = self.publish(request)
        with self.assertRaises(KeyError):
            answer_request(self.root, None, body, story_id=story_id)
        self.assertEqual(answer_request(self.root, project_id, body)["request_id"], legacy_id)
        self.assertEqual(answer_request(self.root, project_id, body)["status"], "accepted")
