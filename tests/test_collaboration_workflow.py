"""Offline service integration: durable messages, separately traced agents and versioned delivery."""
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from pianchang.acceptance import file_sha256
from pianchang.collaboration_workflow import Collaboration, list_runs
from tests.test_script_collab_agent import FINISH, OfflineModel, action
from trace_system import Tracer, use_context


class WorkflowTests(unittest.TestCase):
    def test_two_private_contexts_exchange_pinned_results_and_reuse_original_media(self):
        self._exercise(resume=False)

    def test_resume_consumes_pending_change_without_repeating_first_production(self):
        self._exercise(resume=True)

    def _exercise(self, *, resume):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tracer = Tracer(root / "offline-traces")
            with use_context(None, tracer=tracer):
                env_file = root / "offline.env"
                env_file.write_text("# Offline fixture: both model interfaces are simulated.\n", encoding="utf8")
                session = Collaboration(root / "outputs/collaborations", uuid.uuid4().hex, env_file=env_file)
                project = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "story_project.json").read_text(encoding="utf8"))
                original = project["script_source"]["script"]
                replacement = original["episodes"][0]["scenes"][0]["beats"][-1]["text"] + " 他停步回望。"
                finish = [action("check_script"), action("publish_script"), FINISH]
                models = [OfflineModel([action("read_script"), *finish]),
                          OfflineModel([action("replace_beat", beat_id="B003", text=replacement), *finish])]
                observed = []

                def offline_video(**inputs):
                    observed.append(inputs["inbound_message"])
                    revision = inputs["script_ref"]["revision_id"]
                    shots, reports, changes = {}, {}, {}
                    previous = inputs["previous"]
                    folder = inputs["directory"]
                    folder.mkdir(parents=True, exist_ok=True)
                    for sid in ["S001", "S002", "S003"]:
                        reuse = bool(previous and sid != "S003")
                        shots[sid] = {"operation": "reused" if reuse else "generated",
                                      "media_sha256": previous["shots"][sid]["media_sha256"] if reuse else revision + sid}
                        reports[sid] = {"unresolved": [], "media_accepted": True, "mode":"explicit offline mock acceptance"}
                        changes[sid] = {"target":{"source_ids": ["B003"] if sid == "S003" else ["B001"]},
                                        "changed_components": [] if reuse else ["sources"]}
                    preview = folder / "offline-preview.mp4"
                    preview.write_bytes(b"offline stub for testing delivery bindings")
                    production = {"script_ref": inputs["script_ref"], "project": project, "impact": {
                        "regenerate": ["S003"] if previous else list(shots), "reuse": ["S001","S002"] if previous else [],
                        "changed_sources": ["B003"] if previous else [], "shots": changes, "recheck_edges":[["S002","S003"]]},
                        "reports": reports, "shots": shots, "preview":{"preview_path":str(preview),
                        "preview_sha256":file_sha256(preview)}, "media_accepted": True}
                    return {"production": production, "loop":{"summary":"explicit offline simulation"}}

                with patch("pianchang.collaboration_workflow.BailianTextClient", side_effect=models), \
                     patch("pianchang.video_collab_agent.run_video_agent", side_effect=offline_video):
                    if resume:
                        original_task = session.script_task
                        def interrupt_before_change(**kwargs):
                            if kwargs["revision"] == "r2":
                                raise RuntimeError("offline interruption")
                            return original_task(**kwargs)
                        with patch.object(session, "script_task", side_effect=interrupt_before_change), \
                             self.assertRaisesRegex(RuntimeError, "offline interruption"):
                            session.run(project)
                        from pianchang.collab_continuation import continue_run
                        continue_run(session, local_only=True)
                        self.assertEqual([m["script"]["revision_id"] for m in observed], ["r1"])
                        continue_run(session, local_only=False)
                    else:
                        session.run(project)
                self.assertEqual([m["script"]["revision_id"] for m in observed], ["r1", "r2"])
                ledger = session.store.messages(session.run_id)
                for order in [m for m in ledger if m["message_type"] == "production.requested"]:
                    states = [r["status"] for r in session.store.receipts(order["message_id"])]
                    self.assertEqual(states, ["received", "accepted", "completed"])
                with self.assertRaisesRegex(ValueError, "已被领取"):
                    session.worker(observed[0]["message_id"])
                revisions = session.store.revisions(session.story + "/production")
                p1 = session.store.get_asset(session.story + "/production", "r1")["content"]
                p2 = session.store.get_asset(session.story + "/production", "r2")["content"]
                self.assertEqual(len(revisions), 2)
                self.assertEqual(p1["media_refs"]["S001"], p2["media_refs"]["S001"])
                self.assertNotEqual(p1["media_refs"]["S003"], p2["media_refs"]["S003"])
                class Service:
                    def asset(self, path):
                        return "/assets/offline_preview"
                service = Service()
                service.root = root
                record = list_runs(service)["runs"][0]
                self.assertEqual(record["status"], "completed")
                self.assertTrue(all(tracer.verify(s["trace_id"])["ok"] for s in record["stages"]))
                Path(p2["preview"]["preview_path"]).write_bytes(b"tampered offline fixture")
                self.assertIsNone(list_runs(service)["runs"][0]["productions"][-1]["preview_url"])
