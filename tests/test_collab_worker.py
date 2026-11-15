"""Offline worker tests, including a real independent CLI receiving a durable publication."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from pianchang.collab_negotiation import VideoNegotiation
from pianchang.collab_worker import run_worker
from pianchang.collaboration_workflow import Collaboration, ref
from tests.test_collab_negotiation import FINISH, OfflineDecisions, call


ROOT = Path(__file__).resolve().parents[1]


class WorkerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        env = patch.dict(os.environ, PYTHONIOENCODING="utf-8", AGENT_TRACE_DIR=str(self.folder / "traces"))
        env.start()
        self.addCleanup(env.stop)
        self.session = Collaboration(self.folder, "1" * 32)
        self.session.folder.mkdir(parents=True)
        self.session.write(id=self.session.run_id, title="offline session", status="running", stages=[])
        self.store = self.session.store
        self.source = json.loads((ROOT / "fixtures" / "story_script.json").read_text(encoding="utf8"))

    def publish(self, corr=None, asset_id="script"):
        return self.store.publish_asset(asset_id, self.source, kind="script", producer="script",
            story_id=self.session.story, correlation_id=corr or self.session.run_id, topic="script.revision_published")

    def commission(self, asset):
        return self.store.send("production.requested", sender="commissioner", recipient="video", category="request",
            causation_id=asset["event_id"], story_id=self.session.story, payload={"script": ref(asset),
            "template": None, "previous": None, "max_video_generations": 0, "goal": "offline fixture"})

    def test_offline_cli_observes_publication_without_model_or_new_production_and_filters_correlation(self):
        self.store.subscribe("video", "script.revision_published")
        other = self.publish("2" * 32, asset_id="other-session-script")
        pub = self.publish()
        args = [sys.executable, "-m", "pianchang.collab_worker", "--run-id", self.session.run_id, "--agent", "video",
                "--directory", str(self.folder), "--env-file", str(self.folder / "nonexistent.env")]
        first = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=50)
        self.assertEqual(first.returncode, 0, first.stderr)
        data = json.loads(first.stdout)
        self.assertEqual(data["status"], "observed")
        self.assertEqual(data["last"]["observation"]["script"], ref(pub))
        self.assertEqual(self.store.get_message(other["event_id"])["deliveries"]["video"], "pending")
        self.assertEqual(self.store.get_message(pub["event_id"])["deliveries"]["video"], "completed")
        second = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=50)
        self.assertEqual(json.loads(second.stdout)["status"], "idle")
        self.assertFalse(self.store.messages(self.session.run_id, message_type="production.requested"))

    def test_watch_and_single_consumer_contend_on_one_explicit_task_without_duplicate_execution(self):
        pub = self.publish()
        start, stop = Event(), Event()
        calls = []
        def execute(mid):
            if not self.store.claim("video", message_id=mid):
                raise ValueError("another worker claimed the task")
            self.store.ack("video", mid, "accepted")
            calls.append(mid)
            self.store.ack("video", mid, "completed", result={"offline": True})
            return pub, {"message_id": "offline-result"}
        def watch():
            start.set()
            return run_worker(self.session, "video", watch=True, poll_interval=0.05, stop_event=stop)
        with patch.object(self.session, "worker", side_effect=execute), ThreadPoolExecutor(max_workers=2) as pool:
            watching = pool.submit(watch)
            self.assertTrue(start.wait(5))
            msg = self.commission(pub)
            try:
                single = pool.submit(run_worker, self.session, "video").result(timeout=30)
                self.assertIn(single["status"], {"claimed_elsewhere", "already_claimed", "completed", "idle"})
            finally:
                stop.set()
            self.assertEqual(watching.result(timeout=15)["status"], "stopped")
        self.assertEqual(calls, [msg["message_id"]])
        self.assertEqual(run_worker(self.session, "video")["status"], "idle")

    def test_script_worker_observes_result_as_receipt_not_acceptance(self):
        source = self.publish()
        commission = self.commission(source)
        output = self.store.publish_asset("video", {"script_ref": ref(source), "media_accepted": False},
            kind="production", producer="video", story_id=self.session.story, correlation_id=self.session.run_id)
        msg = self.store.send("production.result", sender="video", recipient="script", category="result",
            story_id=self.session.story, causation_id=commission["message_id"],
            payload={"script": ref(source), "production": ref(output), "media_accepted": False})
        with patch("pianchang.collab_worker._client", side_effect=AssertionError("no model call for receipt")):
            result = run_worker(self.session, "script")
        self.assertEqual(result["last"]["message_id"], msg["message_id"])
        self.assertIs(result["last"]["observation"]["media_accepted"], False)
        self.assertEqual(self.store.receipts(msg["message_id"])[-1]["result"]["observed"]["production"], ref(output))

    def test_script_negotiation_goes_to_model_and_response_returns_to_video_worker(self):
        source = self.publish()
        order = self.commission(source)
        self.store.claim("video", message_id=order["message_id"])
        self.store.ack("video", order["message_id"], "accepted")
        adapter = VideoNegotiation(self.store, script_ref=ref(source), story_id=self.session.story,
                                   production_message_id=order["message_id"])
        mid = adapter.request_script_revision("B003", "offline uncertain staging", "split the shot",
                                              source["asset_id"], "r1")["request_id"]
        model = OfflineDecisions(call("inspect_revision_request"), call("decide_revision_request",
              decision="retain", explanation="use two video shots", instruction=""), FINISH)
        with patch("pianchang.collab_worker._client", return_value=model):
            handled = run_worker(self.session, "script")
        self.assertEqual(handled["status"], "completed")
        self.assertIn("offline uncertain staging", str(model.requests[0]))
        self.assertEqual(self.store.get_message(mid)["deliveries"]["script"], "completed")
        video = run_worker(self.session, "video")
        self.assertEqual(video["last"]["observation"]["response"]["decision"], "retain")
        self.assertEqual(self.store.get_asset("script")["revision_id"], "r1")
        # A previously accepted task is visible for reconciliation and is never executed on restart.
        with patch.object(self.session, "worker", side_effect=AssertionError("would replay pending charge")):
            self.assertEqual(run_worker(self.session, "video")["status"], "already_claimed")
            self.store.ack("video", order["message_id"], "unknown", result={"provider_task": "offline-task"})
            self.assertEqual(run_worker(self.session, "video", watch=True)["status"], "needs_review")


if __name__ == "__main__":
    unittest.main()
