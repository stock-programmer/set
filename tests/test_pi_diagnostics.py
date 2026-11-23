"""Explicit offline frozen-reference diagnostics; no provider/model calls."""
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pianchang.bailian_video import load_config
from pi_bridge.diagnostics import production_diagnostics
from trace_system import event, get_tracer, use_context


class PiDiagnosticsTests(unittest.TestCase):
    def test_all_shots_are_diagnosed_with_real_counts_and_frozen_pin(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder);(path/".env").write_text("# explicit offline fixture\n",encoding="utf-8")
            tracer=get_tracer(path/"traces")
            config=replace(load_config(path/".env"),model="wan2.7-r2v",reference_model=None)
            def refs(count):
                return [{"asset_id":"offline/"+str(i),"revision_id":"r1","sha256":"a"*64,
                         "media_sha256":"b"*64,"subject_id":"char:"+str(i),"purpose":"portrait"} for i in range(count)]
            pin={"asset_id":"offline/spec","revision_id":"r1","sha256":"c"*64}
            asset={**pin,"story_id":"offline-story","content":{"shots":[{"id":"S001","required_refs":refs(8)},
                    {"id":"S002","required_refs":refs(6)},{"id":"S003","required_refs":refs(5)}]}}
            service=SimpleNamespace(tracer=tracer,lock=threading.RLock(),video_config=config,state={"jobs":[
                {"id":"source-job","project_id":"offline-project","status":"failed","error":"retained original failure","trace_id":"d"*32},
                {"id":"another-story","project_id":"other-project","status":"failed","error":"must not leak"}]})
            session={"id":"offline-session","project_id":"offline-project","story_id":"offline-story"}
            with use_context(None,tracer=tracer),tracer.trace("test.pi.diagnostics",inputs={"label":"explicit offline fixture"}) as task,\
                    patch("pianchang.studio_assets.studio_project_for_video",return_value={"store_path":path/"store.sqlite3"}),\
                    patch("pianchang.video_execution.current_commission",return_value={"job_id":"source-job","commission_id":"order","plan":{}}),\
                    patch("pi_bridge.diagnostics.CollabStore") as store:
                store.return_value.get_message.return_value={"payload":{"template":pin}}
                store.return_value.get_asset.return_value=asset
                with tracer.trace("test.prior_conversation") as chat:
                    event("pi.model.receipt", {"model": "offline-text"}, kind="model")
                service.state["jobs"].append({"id":"prior-chat", "kind":"pi_video", "session_id":session["id"],
                    "project_id":session["project_id"], "status":"interrupted", "message":"all calls failed",
                    "trace_id":chat.trace_id})
                result=production_diagnostics(service,session)
                self.assertEqual([s["image_count"] for s in result["shots"]],[8,6,5])
                self.assertEqual([s["status"] for s in result["shots"]],["FAIL","FAIL","PASS"])
                self.assertIn("8 张",result["shots"][0]["error"])
                self.assertEqual([j["id"] for j in result["jobs"]],["source-job", "prior-chat"])
                self.assertEqual(result["jobs"][0]["error"], "retained original failure")
                prior_chat = result["jobs"][1]
                self.assertNotIn("message", prior_chat)
                self.assertEqual(prior_chat["scope"], "conversation_without_production_handoff")
                self.assertEqual(prior_chat["observed_model_response"]["model"], "offline-text")
                self.assertFalse(result["preflight_ready"])
                store.return_value.get_asset.assert_called_once_with(pin["asset_id"],pin["revision_id"])
            events=tracer.get_events(task.trace_id,names=["video.reference_check"])
            failed=[tracer.resolve(e["data"]) for e in events if tracer.resolve(e["data"]).get("status")=="FAIL"]
            self.assertEqual([c["details"]["image_count"] for c in failed],[8,6])


if __name__=="__main__":unittest.main()
