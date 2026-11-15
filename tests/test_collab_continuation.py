"""Offline: evaluate the proposed scope from immutable receipts before any model is contacted."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pianchang.collab_continuation import consume_change, prepare_change
from pianchang.collaboration_workflow import Collaboration, ref
from trace_system import Tracer, use_context


class ContinuationTests(unittest.TestCase):
    def test_queued_change_uses_real_source_dependencies_and_stale_base_blocks_model(self):
        with tempfile.TemporaryDirectory() as temp:
            tracer = Tracer(Path(temp) / "offline-traces")
            with use_context(None, tracer=tracer):
                session = Collaboration(Path(temp) / "offline-store", "c"*32)
                with tracer.trace("offline.cooperation.lifecycle", inputs={"beat":"B003","change":"在门口回望"}) as root:
                    session.write(id=session.run_id, trace_id=root.trace_id, stages=[], status="blocked")
                    story = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "story_script.json").read_text(encoding="utf8"))
                    common = {"story_id":session.story, "correlation_id":session.run_id}
                    script = session.store.publish_asset("offline-script",story,kind="script",producer="script",**common)
                    source = {"script_ref":ref(script),"impact":{"shots":{
                        f"S00{i}":{"target":{"source_ids":[f"B00{i}"]}} for i in range(1,4)}},
                        "project":{"shots":[{"id":f"S00{i}"} for i in range(1,4)]}}
                    prod = session.store.publish_asset("offline-video",source,kind="production",producer="video",**common)
                    result = session.store.send("production.result",sender="video",recipient="script",category="result",
                                                payload={"script":ref(script),"production":ref(prod)},**common)
                first = prepare_change(session,prod,result)
                self.assertEqual(prepare_change(session,prod,result)["message_id"],first["message_id"])
                proposal = session.store.get_asset(first["payload"]["proposal"]["asset_id"])["content"]
                self.assertEqual(proposal["direct_consumers"],["S003"])
                self.assertEqual(proposal["adjacent_checks"],[["S002","S003"]])
                session.store.publish_asset("offline-script",{**story,"script_version":2},kind="script",producer="script",
                                            parent_revision="r1",**common)
                with patch.object(session,"script_task",side_effect=AssertionError("Unexpected model call")):
                    with self.assertRaisesRegex(ValueError,"过期"):
                        consume_change(session,first)
                self.assertEqual(session.store.get_message(first["message_id"])["deliveries"]["script"],"blocked")
