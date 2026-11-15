"""Offline protocol, concurrent publication, and delivery recovery tests. No provider API."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from pianchang.collab_store import CollaborationConflict, CollabStore
from trace_system import TraceError, get_tracer, trace


class CollabStoreTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        base = Path(folder.name)
        env = patch.dict(os.environ, AGENT_TRACE_DIR=str(base / "trace"))
        env.start()
        self.addCleanup(env.stop)
        self.path = base / "business.sqlite"
        self.store = CollabStore(self.path)

    def script(self, *, text="open door", parent=None):
        return self.store.publish_asset("script-1", {"B001": text, "simulation": "offline"}, kind="script",
            producer="script", parent_revision=parent, story_id="story-1", topic="script.revision_published",
            correlation_id="offline-demo")

    def test_versioned_dependency_and_notification_stay_bound_to_original(self):
        self.store.subscribe("video", "script.revision_published", story_id="story-1")
        self.store.subscribe("video", "script.revision_published")  # same recipient, one delivery
        self.store.subscribe("other", "script.revision_published", story_id="another-story")
        first = self.script()
        self.store.publish_asset("shot-1", {"media_sha256": "offline"}, kind="shot", producer="video",
            dependencies=[{"asset_id": "script-1", "revision_id": "r1", "role": "story_beat", "source_id": "B001"}])
        second = self.script(text="close door", parent="r1")
        self.assertNotEqual(first["sha256"], second["sha256"])
        self.assertEqual(self.store.get_asset("script-1", "r1")["content"]["B001"], "open door")
        self.assertEqual(self.store.get_asset("script-1")["revision_id"], "r2")
        self.assertEqual(self.store.dependents("script-1", "r1"),
            [{"asset_id": "shot-1", "revision_id": "r1", "role": "story_beat", "source_id": "B001"}])
        self.assertFalse(self.store.dependents("script-1", "r2"))
        events = self.store.inbox("video")
        self.assertEqual([m["message_id"] for m in events], [first["event_id"], second["event_id"]])
        self.assertTrue(all(m["category"] == "event" and m["recipient"] is None for m in events))
        self.assertFalse(self.store.inbox("other"))

    def test_racing_authors_only_one_commit_for_same_parent(self):
        self.script()
        start = threading.Barrier(2)
        def publish(text):
            start.wait()
            try:
                return self.script(parent="r1", text=text)["revision_id"]
            except CollaborationConflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(pool.map(publish, ["offline-left", "offline-right"]), ["r2", "conflict"])
        self.assertEqual(len(self.store.revisions("script-1")), 2)
        self.assertEqual(len(self.store.messages("offline-demo")), 2)

    def test_failed_dependency_or_trace_does_not_publish_partial_revision(self):
        self.script()
        for dependency in ({"asset_id": "missing", "revision_id": "r1", "role": "uses"},
                           {"asset_id": "bad", "revision_id": "r1", "role": "cycle"}):
            with self.assertRaises(ValueError):
                self.store.publish_asset("bad", {}, kind="media", producer="video", dependencies=[dependency])
        with patch("pianchang.collab_store.event", side_effect=[None, TraceError("offline trace write failure")]):
            with self.assertRaises(TraceError):
                self.script(parent="r1", text="new candidate")
        self.assertEqual(len(self.store.messages()), 1)
        self.assertEqual(self.store.get_asset("script-1")["revision_id"], "r1")
        with self.assertRaises(KeyError):
            self.store.get_asset("bad")

    def test_request_idempotence_and_concurrent_claim_restart_unknown(self):
        pub = self.script()
        args = dict(sender="producer", recipient="video", story_id="story-1", category="request",
                    causation_id=pub["event_id"], message_id="offline-task-1", payload={"revision_id": "r1"})
        task = self.store.send("production.requested", **args)
        self.assertEqual(task["correlation_id"], "offline-demo")
        self.assertEqual(task, self.store.send("production.requested", **args))
        with self.assertRaises(CollaborationConflict):
            self.store.send("production.requested", **{**args, "payload": {"revision_id": "r2"}})
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: self.store.claim("video"), range(2)))
        self.assertEqual([c["message_id"] for c in claims if c], ["offline-task-1"])
        with self.assertRaises(CollaborationConflict):
            self.store.ack("video", "offline-task-1", "completed")
        self.store.ack("video", "offline-task-1", "accepted")
        context = {"provider_job": "existing-offline-provider-task", "check_first": True}
        self.store.ack("video", "offline-task-1", "unknown", result=context)
        reopened = CollabStore(self.path)
        self.assertIsNone(reopened.claim("video", message_id=task["message_id"]))
        self.assertEqual(reopened.inbox("video", "unknown")[0]["payload"]["revision_id"], "r1")
        reopened.ack("video", task["message_id"], "accepted")  # delayed duplicate ACK cannot regress status
        receipts = reopened.receipts(task["message_id"])
        self.assertEqual([r["status"] for r in receipts], ["received", "accepted", "unknown"])
        self.assertEqual(receipts[-1]["result"], context)
        self.assertTrue(all("trace_id" in r["trace_context"] for r in receipts))

    def test_protocol_rejects_implicit_production_or_fabricated_publication(self):
        with self.assertRaises(ValueError):
            self.store.send("production.requested", sender="offline", category="event")
        with self.assertRaises(ValueError):
            self.store.send("script.revision_published", sender="offline")
        with self.assertRaises(ValueError):
            self.store.send("work", sender="offline", category="request")
        with self.assertRaises(ValueError):
            self.store.send("result", sender="offline", causation_id="unknown")
        self.assertFalse(self.store.messages())

    def test_causal_chain_links_separate_execution_roots(self):
        with trace("offline.script.root") as script_trace:
            pub = self.script()
        with trace("offline.video.root", links=[{"trace_id": pub["trace_id"], "relation": "consumes"}]) as video_trace:
            result = self.store.send("production.result", category="result", sender="video", recipient="script",
                                     causation_id=pub["event_id"], payload={"status": "offline-test"})
        self.assertEqual(pub["trace_id"], script_trace.trace_id)
        self.assertEqual(result["trace_id"], video_trace.trace_id)
        self.assertNotEqual(pub["trace_id"], result["trace_id"])
        self.assertEqual(len(self.store.messages("offline-demo")), 2)

    def test_delivery_bundle_rolls_back_every_revision_result_and_completion_receipt(self):
        script = self.script()
        job = self.store.send("production.requested", sender="client", recipient="video",
                              causation_id=script["event_id"], payload={"offline": True})
        self.store.claim("video", message_id=job["message_id"])
        self.store.ack("video", job["message_id"], "accepted")
        refs = [{"asset_id": "script-1", "revision_id": "r1", "role": "script"}]
        for asset_id in ("media", "checks", "production"):
            self.store.publish_asset(asset_id, {"offline": 1}, kind=asset_id, producer="video",
                                     story_id="story-1", dependencies=refs)
        baseline = self.store.messages()
        observer = CollabStore(self.path)
        with self.assertRaisesRegex(RuntimeError, "after receipt"):
            with trace("offline.failed_package") as failed:
                with self.store.transaction():
                    for asset_id in ("media", "checks", "production"):
                        self.store.publish_asset(asset_id, {"offline": 2}, kind=asset_id, producer="video",
                            story_id="story-1", parent_revision="r1", dependencies=refs)
                    result = self.store.send("production.result", sender="video", recipient="script", category="result",
                                             payload={"production": "production@r2"}, causation_id=job["message_id"])
                    self.store.ack("video", job["message_id"], "completed", result={"message_id": result["message_id"]})
                    # A nested same-thread handle sees staged data, another thread sees the committed version.
                    with CollabStore(self.path).transaction():
                        self.assertEqual(observer.get_asset("production")["revision_id"], "r2")
                    with ThreadPoolExecutor(max_workers=1) as reader:
                        self.assertEqual(reader.submit(observer.get_asset, "production").result()["revision_id"], "r1")
                    raise RuntimeError("offline failure after receipt")
        self.assertEqual(self.store.messages(), baseline)
        for asset_id in ("media", "checks", "production"):
            self.assertEqual(self.store.get_asset(asset_id)["revision_id"], "r1")
        self.assertEqual([r["status"] for r in self.store.receipts(job["message_id"])], ["received", "accepted"])
        details = get_tracer().get_trace(failed.trace_id)
        names = [ev["name"] for ev in details["events"]]
        self.assertIn("collab.transaction.staged", names)
        self.assertIn("collab.transaction.rolled_back", names)
        self.assertNotIn("collab.transaction.committed", names)
        self.assertNotIn("collab.message.published", names)

    def test_successful_bundle_is_visible_after_commit_and_caught_failure_poisoned(self):
        with trace("offline.successful_package") as successful:
            with self.store.transaction():
                self.script()
                task = self.store.send("production.requested", sender="client", recipient="video",
                                       payload={"asset_id": "script-1", "revision_id": "r1"})
            self.assertEqual(self.store.get_message(task["message_id"])["payload"]["revision_id"], "r1")
        events = get_tracer().get_trace(successful.trace_id)["events"]
        self.assertEqual(sum(ev["name"] == "collab.transaction.committed" for ev in events), 1)
        with self.assertRaisesRegex(CollaborationConflict, "rollback"):
            with self.store.transaction():
                self.script(parent="r1", text="new draft")
                try:
                    self.store.send("production.requested", sender="client", category="event")
                except ValueError:
                    pass
        self.assertEqual(self.store.get_asset("script-1")["revision_id"], "r1")


if __name__ == "__main__":
    unittest.main()
