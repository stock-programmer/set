"""Exercise urllib proxy CONNECT and origin writes with in-memory sockets."""
import io
import json
import ssl
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from urllib import request
from unittest.mock import Mock, patch

from pianchang.bailian_observer import BailianObserver, RoutedVisionConfig
from pianchang.bailian_text import TextError
from pianchang.bailian_video import _NoRedirect, _verified_https_handler
from pianchang.content_review import review_video
from pianchang.review_dispatch import ReviewNotDispatched, ReviewOutOfScope, _claim, _verified_unsent, one_review_call, retry_review_scope
from pianchang.story_spec import digest_json
from trace_system import TraceError, Tracer, get_tracer, trace, use_context


class ReviewDispatchTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(use_context(None, tracer=Tracer(self.folder / "trace")))
        self.enterContext(trace("offline.review.dispatch"))
        self.config = RoutedVisionConfig(api_key="offline-credential", base_url="https://review.invalid/v1",
                                         model="fixture", adapter="openai", route_account="offline")
        self.client = BailianObserver(self.config)
        self.client._opener = request.build_opener(request.ProxyHandler({"https": "http://proxy.invalid:443"}),
                                                   _NoRedirect(), _verified_https_handler())
        self.enterContext(patch("urllib.request.proxy_bypass", return_value=False))
        self.sent = []
        self.sock = Mock()
        self.sock.sendall.side_effect = self.sent.append
        self.enterContext(patch("socket.create_connection", return_value=self.sock))
        self.wrap = self.enterContext(patch.object(ssl.SSLContext, "wrap_socket"))
        self.budget = Mock()
        self.budget.commission_id = "original-commission"
        self.path = self.folder / "visual-call.json"
        self.inputs = {"model": "fixture", "media_sha256": "original", "endpoint": self.config.base_url}

    def exchanges(self, *, handshake_error=False, origin_reply=None):
        replies = [b"HTTP/1.1 200 Connection Established\r\n\r\n", origin_reply or b""]
        self.sock.makefile.side_effect = [io.BytesIO(v) for v in replies]
        self.wrap.side_effect = ssl.SSLEOFError(8, "offline handshake ended") if handshake_error else None
        self.wrap.return_value = self.sock

    def post(self):
        req = request.Request(self.config.base_url + "/chat/completions", data=b'{"model":"fixture"}')
        return self.client._post(req)[0]

    def observe(self, *, budget=None):
        return one_review_call(self.path, self.inputs, "vision.review", self.post, available=True,
                               reserve=self.budget if budget is None else budget)

    def test_proxy_handshake_recovery_requires_original_proof_and_budget(self):
        self.exchanges(handshake_error=True)
        with self.assertRaises(ReviewNotDispatched):
            self.observe()
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0].startswith(b"CONNECT review.invalid:443 "))
        prior = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertTrue(_verified_unsent(prior))
        wrong = Mock(commission_id="another-commission")
        with self.assertRaises(ReviewNotDispatched):
            self.observe(budget=wrong)
        wrong.assert_not_called()
        self.budget.side_effect = ValueError("model limit reached")
        with self.assertRaisesRegex(ReviewNotDispatched, "model limit"):
            self.observe()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), prior)
        self.budget.side_effect = None
        self.exchanges(origin_reply=b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}')
        self.assertEqual(self.observe(), {})
        self.assertEqual(self.observe(), {})
        self.assertEqual(sum(data.startswith(b"POST ") for data in self.sent), 1)
        self.assertEqual(self.budget.call_count, 3)
        archive, = self.folder.glob("visual-call-*.json")
        self.assertEqual(json.loads(archive.read_text(encoding="utf-8")), prior)
        altered = {**prior, "wire": {**prior["wire"], "wire_body_sha256": "0" * 64}}
        self.assertFalse(_verified_unsent(altered))
        wrong_model = {**prior, "request": {**prior["request"], "model": "changed"}}
        self.assertFalse(_verified_unsent(wrong_model))

    def test_unknown_after_origin_write_and_legacy_unknown_are_never_replayed(self):
        self.exchanges()
        with self.assertRaises(TextError):
            self.observe()
        self.assertTrue(any(data.startswith(b"POST ") for data in self.sent))
        legacy = {"input_sha256": digest_json(self.inputs), "request": self.inputs, "status": "submitted_or_unknown"}
        for record in [json.loads(self.path.read_text(encoding="utf-8")), legacy]:
            self.assertFalse(_verified_unsent(record))
            self.path.write_text(json.dumps(record), encoding="utf-8")
            if record.get("attempt"):
                with retry_review_scope({self.path: record["attempt"]["id"]}), self.assertRaises(ReviewOutOfScope):
                    self.observe()
            with self.assertRaisesRegex(ValueError, "未确认"):
                self.observe()
        self.budget.assert_called_once()

    def test_listener_proof_is_bound_to_the_distinct_audio_route(self):
        inputs = {**self.inputs, "model": "visual", "endpoint": "https://visual.invalid",
                  "audio_review": {"model": self.config.model, "endpoint": self.config.base_url}}
        path = self.folder / "audio-call.json"
        def listen():
            return one_review_call(path, inputs, "continuity.review", self.post, available=True, reserve=self.budget)
        self.exchanges(handshake_error=True)
        with self.assertRaises(ReviewNotDispatched):
            listen()
        self.assertTrue(_verified_unsent(json.loads(path.read_text(encoding="utf-8"))))
        self.exchanges(origin_reply=b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}')
        self.assertEqual(listen(), {})
        self.assertEqual(sum(data.startswith(b"POST ") for data in self.sent), 1)

    def test_scoped_worker_only_finishes_original_pair_and_reads_completed_caches(self):
        from pianchang.video_collab_agent import _one_model_call
        first, second = self.folder / "audio-call.json", self.folder / "visual-call.json"
        inputs = {**self.inputs, "audio_review": {"model": self.config.model, "endpoint": self.config.base_url}}
        def listen():
            return one_review_call(first, inputs, "continuity.review", self.post, available=True, reserve=self.budget)
        def companion():
            return one_review_call(second, {**inputs, "audio_sha256": digest_json({})}, "vision.review", self.post,
                                   available=True, reserve=self.budget, partner=(first, inputs))
        self.exchanges(handshake_error=True)
        with self.assertRaises(ReviewNotDispatched):
            listen()
        selected = json.loads(first.read_text(encoding="utf-8"))["attempt"]["id"]
        cached = self.folder / "other-completed.json"
        self.assertEqual(_one_model_call(cached, {"x": 1}, lambda: 42), 42)
        with retry_review_scope({first: "a" * 32}):
            with self.assertRaises(ReviewOutOfScope):
                listen()
        with retry_review_scope({first: selected}):
            with self.assertRaises(ReviewOutOfScope):
                companion()
            with self.assertRaises(ReviewOutOfScope):
                self.post()
            self.assertEqual(_one_model_call(cached, {"x": 1}, self.fail), 42)
            absent = self.folder / "unrelated-fallback.json"
            with self.assertRaises(ReviewOutOfScope):
                _one_model_call(absent, {}, self.fail)
            self.assertFalse(absent.exists())
            self.assertFalse(second.exists())
            self.exchanges(origin_reply=b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}')
            self.assertEqual(listen(), {})
        # Reentering after the first response can finish its still-unsubmitted companion.
        with retry_review_scope({first: selected}):
            self.exchanges(origin_reply=b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}')
            self.assertEqual(companion(), {})
            self.assertEqual(companion(), {})
        self.assertEqual(sum(data.startswith(b"POST ") for data in self.sent), 2)
        self.assertEqual([c.args[0] for c in self.budget.call_args_list],
                         ["continuity.review", "continuity.review", "vision.review"])

    def test_changed_wire_and_concurrent_worker_cannot_replay(self):
        self.exchanges(handshake_error=True)
        with self.assertRaises(ReviewNotDispatched):
            self.observe()
        prior = self.path.read_bytes()
        with _claim(self.path), self.assertRaises(ReviewNotDispatched):
            self.observe()
        self.budget.assert_called_once()
        def changed_wire():
            return self.client._post(request.Request(self.config.base_url + "/chat/completions", data=b"changed"))
        with self.assertRaisesRegex(ReviewNotDispatched, "不一致"):
            one_review_call(self.path, self.inputs, "vision.review", changed_wire, available=True, reserve=self.budget)
        self.assertEqual(self.path.read_bytes(), prior)
        self.assertEqual(len(self.sent), 1)

    def test_redirect_uses_one_original_post_and_never_replays(self):
        self.exchanges(origin_reply=b"HTTP/1.1 307 Temporary Redirect\r\nLocation: https://other.invalid/\r\nContent-Length: 0\r\n\r\n")
        with self.assertRaises(TextError):
            self.observe()
        with self.assertRaisesRegex(ValueError, "未确认"):
            self.observe()
        self.assertEqual(sum(b"CONNECT " in data for data in self.sent), 1)
        self.assertEqual(sum(data.startswith(b"POST ") for data in self.sent), 1)
        self.assertNotIn(b"other.invalid", b"".join(self.sent))

    def test_trace_failure_at_origin_boundary_stops_before_post(self):
        self.exchanges()
        original = get_tracer().event
        def fail(name, *args, **kwargs):
            if name == "http.origin_write":
                raise TraceError("offline trace write failed")
            return original(name, *args, **kwargs)
        with patch.object(get_tracer(), "event", side_effect=fail), self.assertRaises(TraceError):
            self.observe()
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0].startswith(b"CONNECT "))
        self.assertFalse(_verified_unsent(json.loads(self.path.read_text(encoding="utf-8"))))

    def test_paired_review_reuses_listener_then_publishes_separate_result(self):
        source = self.folder / "source"
        source.write_bytes(b"offline media")
        config = replace(self.config, continuity_config=replace(self.config, model="listener"))
        video = {"id": "V0", "duration_s": 1, "scope": "offline fixture", "path": str(source)}
        context = {"cut_s": .5, "main_audio": {"path": str(source)}}
        criteria = [{"kind": "sequence_review", "criterion_id": "cut"}]
        row = {"criterion_id": "cut", "verdict": "UNKNOWN", "reason": "offline missing evidence",
               "evidence": [{"asset_id": "V0", "start_s": 0, "end_s": 1, "observation": "offline visual fixture"}],
               "reference_ids": [], "components": [{"name": name, "verdict": "UNKNOWN", "reason": "offline fixture"}
                                                   for name in ("identity", "costume_props", "space", "action")]}
        audio = {"data": {"observations": [{**row, "evidence": [{**row["evidence"][0], "asset_id": "A0"}]}]},
                 "request_id": "original-listener-receipt"}
        observer = Mock(config=config, system_prompt="offline paired model")
        def visual(*args):
            self.post()
            return {"data": {"observations": [row]}}
        observer.observe_video.side_effect = visual
        def review():
            return review_video(config, video, [], criteria, context, self.folder / "pair",
                                client=observer, audiovisual=True, reserve=self.budget)
        with patch("pianchang.openai_review.listen_cut", return_value=audio) as listen:
            self.exchanges(handshake_error=True)
            pending = review()
            old_path = Path(pending["receipt_path"])
            old = old_path.read_bytes()
            self.assertTrue(old_path.name.startswith("undispatched-"))
            self.assertEqual(pending["dispatch_evidence"]["status"], "not_sent")
            self.exchanges(origin_reply=b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}')
            finished = review()
            self.assertEqual(finished["execution_status"], "executed", finished["rows"])
            self.assertEqual(finished["rows"][0]["status"], "UNKNOWN")
            self.assertEqual(finished["response"]["audio_review"], audio)
            self.assertEqual(old_path.read_bytes(), old)
            self.assertEqual(Path(finished["receipt_path"]).name, "receipt-p4.json")
            self.assertEqual(review()["receipt_sha256"], finished["receipt_sha256"])
            listen.assert_called_once()
        self.assertEqual([c.args[0] for c in self.budget.call_args_list],
                         ["continuity.review", "vision.review", "vision.review"])
