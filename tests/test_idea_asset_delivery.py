"""One user submission through the real script and asset queues, with offline provider responses."""
import io
import os
import tempfile
import unittest
import wave
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from pianchang.app_service import AppService, read_json
from pianchang.studio_flow import studio_flow
from pianchang.studio_assets import studio_reference_media
from tests.test_reference_assets import offline_png
from tests.test_script_collab_agent import FINISH, OfflineModel, action
from tests.test_story_agent import draft_from_script
from trace_system import use_context


class IdeaAssetDeliveryTests(unittest.TestCase):
    def test_one_idea_selects_playable_assets_and_binds_shots_before_any_page_read(self):
        project = read_json(Path(__file__).resolve().parents[1] / "fixtures" / "story_project.json")
        script = project["script_source"]["script"]
        model = OfflineModel([action("write_draft", **draft_from_script(script)), action("check_script"),
                              action("publish_script"), FINISH])
        png = offline_png((48, 12, 0))
        audio = io.BytesIO()
        with wave.open(audio, "wb") as wav:
            wav.setparams((1, 2, 8000, 0, "NONE", ""))
            wav.writeframes(b"\x00\x00" * 9600)
        sound = audio.getvalue()

        def see(service, brief, criteria, files):
            self.assertEqual(files["candidate"].read_bytes(), png)
            return ([{"criterion_id": k, "verdict": "PASS", "reason": "offline image sample",
                      "frame_ids": list(files)} for k in criteria], {"model": "offline visual observer"})

        def hear(service, data, mime, brief, criteria, *, with_receipt):
            self.assertEqual((data, mime), (sound, "audio/wav"))
            return ([{"criterion_id": k, "verdict": "PASS", "reason": "offline sound sample"}
                     for k in criteria], {"model": "offline listener"})

        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            root = Path(temp)
            (root / ".env").write_text("# offline", encoding="utf-8")
            stack.enter_context(patch.dict(os.environ, {"AGENT_TRACE_DIR": str(root / "offline-traces")}))
            service = AppService(root)
            stack.callback(service.close)
            stack.enter_context(use_context(None, tracer=service.tracer))
            mocks = [
                patch("pianchang.studio_jobs.BailianTextClient", return_value=model),
                patch("pianchang.story_agent.StoryRunner._call", return_value={"data": project}),
                patch("pianchang.studio_assets.compose_reference_prompt", return_value="offline photo"),
                patch("pianchang.studio_assets._request_image", return_value={"output": {"choices": [
                    {"message": {"content": [{"image": "https://offline.invalid/image"}]}}]}}),
                patch("pianchang.studio_assets._download_image", return_value=png),
                patch("pianchang.asset_review._observe_images", side_effect=see),
                patch("pianchang.asset_voice.synthesize_voice", return_value=(sound, "audio/wav", {"model": "offline tts"})),
                patch("pianchang.asset_voice.observe_voice", side_effect=hear),
            ]
            for mock in mocks:
                stack.enter_context(mock)

            submitted = service.submit("studio_script", None, {"idea": script["idea"]})
            service.script_executor.submit(lambda: None).result(timeout=90)
            service.asset_executor.submit(lambda: None).result(timeout=180)
            jobs = {j["id"]: j for j in service.list_jobs()}
            writing = jobs[submitted["id"]]
            self.assertEqual(writing["status"], "succeeded", writing.get("error"))
            output = writing["result"]
            batch = jobs[output["asset_job"]["id"]]
            self.assertEqual(batch["status"], "succeeded", batch.get("error"))
            self.assertEqual(len(jobs), 2)
            pid = output["project_id"]
            flow = studio_flow(service, pid)
            for entity in flow["assets"]["entities"]:
                self.assertEqual({r["purpose"] for r in entity["references"]}, set(entity["purposes"]))
                for ref in entity["references"]:
                    media, mime = studio_reference_media(service, pid, ref["asset_id"], ref["revision_id"])
                    self.assertEqual(media.read_bytes(), sound if mime == "audio/wav" else png)
            self.assertEqual(flow["asset_work"]["counts"], {"approved": 7})
            items = flow["asset_work"]["items"]
            for row in items:
                self.assertEqual(row["review"]["status"], "PASS")
                self.assertEqual(row["approval"]["pin"], row["candidate"])
            for shot in flow["assets"]["shots"]:
                subjects = {shot["scene_id"], *shot["entity_ids"]}
                expected = [row["candidate"] for row in items if row["subject_id"] in subjects]
                self.assertCountEqual(shot["required_refs"], expected)
            # No confirmation request or visit to the asset page precedes video readiness.
            from pianchang.asset_dispatch import check_video_asset_readiness
            from pianchang.studio_assets import studio_project_for_video
            check_video_asset_readiness(service, pid, studio_project_for_video(service, pid)["project"])
