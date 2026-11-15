"""Offline observations keep current evidence while bounding duplicated reports."""
import json
import tempfile
import unittest
from pathlib import Path
from pi_bridge.context import video_observation,reference_observation
from pianchang.story_spec import digest_json
from trace_system import get_tracer,use_context


class PiContextTests(unittest.TestCase):
    def test_targeted_patch_keeps_each_actual_prior_checkpoint(self):
        from pi_bridge.script_tools import StoryTools
        with tempfile.TemporaryDirectory() as folder:
            directory=Path(folder);tracer=get_tracer(directory/"trace")
            with use_context(None,tracer=tracer),tracer.trace("test.pi.patch_history",inputs={"label":"explicit offline fixture"}):
                workspace=StoryTools.__new__(StoryTools)
                original={"title":"original offline title","shots":[{"id":"S001"}]}
                workspace.spec=original;workspace.directory=directory;workspace.spec_path=directory/"candidate.json"
                workspace.check_spec=lambda:{"structure_ready":True,"label":"offline validation fixture"}
                workspace.patch_spec(json.dumps([{"path":["title"],"value":"first offline patch"}]))
                previous=workspace.spec
                workspace.patch_spec(json.dumps([{"path":["title"],"value":"second offline patch"}]))
                for snapshot in (original,previous):
                    archive=directory/("spec-history-"+digest_json(snapshot)+".json")
                    self.assertEqual(json.loads(archive.read_text("utf-8")),snapshot)
                self.assertEqual(json.loads(workspace.spec_path.read_text("utf-8"))["title"],"second offline patch")

    def test_current_failures_and_evidence_survive_model_projection(self):
        with tempfile.TemporaryDirectory() as folder:
            tracer=get_tracer(Path(folder)/"trace")
            with use_context(None,tracer=tracer),tracer.trace("test.pi.context",inputs={"label":"explicit offline fixture"}):
                issue={"subject_id":"char:A","purpose":"voice","status":"needs_attention","attempts":[],
                    "review":{"brief":{"duplicate_script":"historical text "*10000},"verdict":"UNKNOWN",
                    "observation_error":"actual parser failure","trace_id":"offline-trace","review_ref":{"asset_id":"offline-review","revision_id":"r1"},
                    "checks":[{"path":"voice","required":True,"status":"UNKNOWN","actual":"no decoded observation"},
                              {"path":"format","required":True,"status":"PASS","actual":"WAV"}]}}
                state={"project_id":"offline-project","asset_work":{"counts":{"approved":13,"needs_attention":2},
                    "items":[issue,{"subject_id":"prop:B","purpose":"prop","status":"needs_attention"}],"jobs":[]},
                    "preview_gate":{"ready":False}}
                short=video_observation(state)
                self.assertEqual(short["observation_id"],digest_json(state))
                self.assertEqual(short["asset_work"]["counts"]["approved"],13)
                actual=short["asset_work"]["issues"][0]
                self.assertEqual(actual["observation_error"],"actual parser failure")
                self.assertEqual(actual["trace_id"],"offline-trace")
                self.assertEqual(actual["checks"][0]["status"],"UNKNOWN")
                self.assertEqual(len(actual["checks"]),1)
                self.assertFalse(short["preview_gate"]["ready"])
                self.assertLess(len(json.dumps(short)),5000)
                self.assertEqual(len(reference_observation(issue)["checks"]),2)

    def test_reference_and_script_revision_numbers_have_separate_namespaces(self):
        with tempfile.TemporaryDirectory() as folder:
            tracer=get_tracer(Path(folder)/"trace")
            with use_context(None,tracer=tracer),tracer.trace("test.pi.reference_provenance",inputs={"label":"explicit offline fixture"}):
                source={"asset_id":"offline/script","revision_id":"r1","sha256":"a"*64}
                item={"subject_id":"prop:Bread","purpose":"prop","status":"needs_attention",
                    "candidate":{"asset_id":"offline/bread","revision_id":"r2"},"review":{"brief":{"script_ref":source}}}
                value={"script":{"script_ref":source},"asset_work":{"items":[item]}}
                issue=video_observation(value)["asset_work"]["issues"][0]
                self.assertEqual(issue["source_script_validation"]["status"],"PASS")
                value["script"]["script_ref"]={**source,"sha256":"b"*64}
                self.assertEqual(video_observation(value)["asset_work"]["issues"][0]["source_script_validation"]["status"],"FAIL")


if __name__=="__main__":unittest.main()
