"""Offline independent-agent negotiation. All model actions are explicitly simulated."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from pianchang.collab_negotiation import VideoNegotiation, consume_revision_work, run_script_negotiation
from pianchang.collab_store import CollaborationConflict, CollabStore
from pianchang.story_spec import digest_json
from trace_system import Tracer, use_context


class OfflineDecisions:
    agent_backend="offline_legacy"
    def __init__(self, *actions):
        self.actions = iter(actions)
        self.requests = []

    def complete_json(self, messages):
        self.requests.append(copy.deepcopy(messages))
        return {"model": "offline-negotiation-test", "data": next(self.actions)}


def call(name, **arguments):
    return {"type": "tool", "tool": name, "arguments": arguments, "summary": "offline model decision"}


FINISH = {"type": "finish", "outcome": "completed", "summary": "offline finish request"}


class NegotiationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.tracer = Tracer(self.folder / "traces")
        active = use_context(None, self.tracer)
        active.__enter__()
        self.addCleanup(active.__exit__, None, None, None)
        self.store = CollabStore(self.folder / "store.sqlite")
        self.script = json.loads((Path(__file__).resolve().parents[1] / "fixtures/story_script.json").read_text(encoding="utf-8"))
        self.asset = self.store.publish_asset("story/script", self.script, kind="script", producer="script",
            topic="script.revision_published", story_id="story", correlation_id="offline-collaboration")
        self.ref = {k: self.asset[k] for k in ("asset_id", "revision_id", "sha256")}
        job = self.store.send("production.requested", sender="client", recipient="video",
            category="request", payload={"script": self.ref}, story_id="story", causation_id=self.asset["event_id"])
        self.store.claim("video", message_id=job["message_id"])
        self.store.ack("video", job["message_id"], "accepted")
        self.video = VideoNegotiation(self.store, script_ref=self.ref, story_id="story", production_message_id=job["message_id"])
        self.evidence = self.store.publish_asset("story/inspection", {"flag": "offline reported action too long",
            "coverage": "S003 t=2..4 seconds", "simulation": True}, kind="inspection", producer="video",
            story_id="story", dependencies=[{**{k: self.ref[k] for k in ("asset_id", "revision_id")}, "role": "observes"}])

    def request(self):
        return self.video.request_script_revision("B003", "离线证据：时间不足以展示所需动作。", "建议停顿后再举起钥匙。",
            self.evidence["asset_id"], self.evidence["revision_id"])["request_id"]

    def resolve(self, request_id, decision):
        model = OfflineDecisions(FINISH, call("inspect_revision_request"), call("decide_revision_request",
            decision=decision, explanation="offline assessed the observed duration",
            instruction="让陈默停顿一秒再举起钥匙。" if decision != "retain" else ""), FINISH)
        outcome = run_script_negotiation(model, self.folder / "review", store=self.store, request_id=request_id)
        self.assertTrue(outcome["loop_result"]["task_complete"])
        # The actual incoming request is in the first model decision; real evidence is fed into the next.
        self.assertIn("离线证据", str(model.requests[0]))
        self.assertIn("offline reported action too long", str(model.requests[2]))
        self.assertTrue(self.tracer.verify(outcome["trace_id"])["ok"])
        return outcome

    def test_accept_creates_one_pending_job_then_checked_revision_with_causal_receipt(self):
        request_id = self.request()
        self.assertEqual(request_id, self.request())
        self.assertEqual(self.video.read_script_response(request_id)["status"], "pending")
        out = self.resolve(request_id, "accept")
        data = self.video.read_script_response(request_id)
        work_id = data["response"]["revision_work_id"]
        self.assertEqual(data["response"]["publication_status"], "pending_publication")
        self.assertFalse(data["published_revisions"])
        self.assertEqual(self.store.get_asset("story/script")["revision_id"], "r1")
        self.assertEqual(self.store.get_message(work_id)["causation_id"], out["response"]["message_id"])
        recovery = run_script_negotiation(OfflineDecisions(), self.folder / "unused", store=self.store, request_id=request_id)
        self.assertFalse(recovery["model_called"])
        self.assertEqual(out["response"]["message_id"], recovery["response"]["message_id"])
        new_text = "陈默停顿一秒，举起铜钥匙。"
        model = OfflineDecisions(call("read_script"), call("replace_beat", beat_id="B003", text=new_text),
            call("check_script"), call("publish_script"), FINISH)
        published = consume_revision_work(model, self.folder / "revision", store=self.store, message_id=work_id)
        self.assertEqual(published["status"], "completed")
        self.assertEqual(self.store.get_asset("story/script", "r1")["sha256"], digest_json(self.script))
        expected = copy.deepcopy(self.script)
        expected["script_version"] += 1
        expected["episodes"][0]["scenes"][0]["beats"][2]["text"] = new_text
        self.assertEqual(self.store.get_asset("story/script", "r2")["content"], expected)
        observed = self.video.read_script_response(request_id)
        self.assertEqual(observed["published_revisions"][0]["revision_id"], "r2")
        self.assertEqual(observed["revision_work_status"], "completed")
        self.assertEqual(published["publication"]["changed_source_ids"], ["B003"])

    def test_keep_and_alternative_are_responses_without_a_revision_or_job(self):
        for choice in ("retain", "alternative"):
            with self.subTest(choice=choice):
                # Keep the two independent model histories in different directories.
                if choice == "alternative":
                    self.folder = self.folder / choice
                    job = self.store.send("production.requested", sender="client", recipient="video",
                        payload={"script": self.ref}, story_id="story", causation_id=self.asset["event_id"])
                    self.store.claim("video", message_id=job["message_id"])
                    self.store.ack("video", job["message_id"], "accepted")
                    self.video = VideoNegotiation(self.store, script_ref=self.ref, story_id="story", production_message_id=job["message_id"])
                request_id = self.request()
                self.resolve(request_id, choice)
                response = self.video.read_script_response(request_id)
                self.assertEqual(response["response"]["decision"], choice)
                self.assertIsNone(response["response"]["revision_work_id"])
                self.assertFalse(response["published_revisions"])
                self.assertEqual(self.store.get_asset("story/script")["revision_id"], "r1")

    def test_newer_revision_blocks_pending_work_without_replaying_model_or_changing_head(self):
        request_id = self.request()
        out = self.resolve(request_id, "accept")
        work_id = out["response"]["payload"]["revision_work_id"]
        copy_script = copy.deepcopy(self.script)
        copy_script["script_version"] += 1
        self.store.publish_asset("story/script", copy_script, kind="script", producer="script",
            parent_revision="r1", topic="script.revision_published", story_id="story")
        unused = OfflineDecisions()
        out = consume_revision_work(unused, self.folder / "blocked", store=self.store, message_id=work_id)
        self.assertEqual(out["status"], "blocked")
        self.assertEqual(out["check"]["actual"], "r2")
        self.assertFalse(unused.requests)
        self.assertEqual(self.video.read_script_response(request_id)["published_revisions"], [])
        with self.assertRaises(CollaborationConflict):
            consume_revision_work(unused, self.folder / "replay", store=self.store, message_id=work_id)

    def test_unbound_evidence_cannot_generate_a_request(self):
        unrelated = self.store.publish_asset("other-input", {"offline": True}, kind="evidence", producer="video", story_id="story")
        with self.assertRaisesRegex(ValueError, "in_task_scope"):
            self.video.request_script_revision("B003", "issue", "something", unrelated["asset_id"], unrelated["revision_id"])
        with self.assertRaises(ValueError):
            self.video.request_script_revision("B999", "issue", "something", self.ref["asset_id"], self.ref["revision_id"])
        self.assertFalse(self.store.messages(message_type="script.revision_requested"))


if __name__ == "__main__":
    unittest.main()
