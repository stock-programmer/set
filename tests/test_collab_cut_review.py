"""Offline media test using synthetic colored frames and tones, with no model calls."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from pianchang.acceptance import file_sha256
from pianchang.bailian_text import TextError
from pianchang.collab_cut_review import _ffmpeg, inspect_cut, observe_cut
from pianchang.episode_export import export_preview
from pianchang.media_tools import find_ffmpeg, find_ffprobe, MediaError
from pianchang.story_spec import digest_json
from trace_system import Tracer, use_context


class CutReviewTests(unittest.TestCase):
    def test_cut_observations_need_both_frames_and_uncertain_calls_are_not_replayed(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            tracer = Tracer(folder/"traces")
            with use_context(None,tracer=tracer):
                client = Mock(config=SimpleNamespace(model="offline-observer",base_url="offline-endpoint"))
                report = {"frames":[{"preview_time_s":n,"sha256":str(n)*64,"path":str(folder/str(n))}
                                    for n in (1,2)],"preview_sha256":"a"*64,
                          "shots":[{"id":"L"},{"id":"R"}],"state_check":{"expected":{}}}
                prod = {"project":{"shots":[{"id":i,"scene_id":"room","description":"offline"} for i in ("L","R")]}}
                client.complete_observations.side_effect = TextError("explicit offline network error")
                self.assertEqual(observe_cut(client,prod,report,folder/"failed")["status"],"UNKNOWN")
                self.assertTrue(observe_cut(client,prod,report,folder/"failed")["cached"])
                self.assertEqual(client.complete_observations.call_count,1)
                client.complete_observations.side_effect = None
                client.complete_observations.return_value = {"data":{"observations":[
                    {"criterion_id":k,"frame_ids":["left_last"] if k=="costume" else ["left_last","right_first"],
                     "verdict":"PASS","reason":"offline answer"} for k in ("costume","space","prop_state")]}}
                result = observe_cut(client,prod,report,folder/"success")
                self.assertEqual(result["rows"][0]["verdict"],"UNKNOWN")
                self.assertEqual(result["rows"][1]["preview_times_s"],[1,2])
                self.assertTrue(observe_cut(client,prod,report,folder/"success")["cached"])
                self.assertEqual(client.complete_observations.call_count,2)

    def test_frames_audio_and_time_are_bound_to_actual_final_preview(self):
        try:
            find_ffmpeg()
            find_ffprobe()
        except MediaError as exc:
            self.skipTest(str(exc))
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            tracer = Tracer(folder / "offline-traces")
            with use_context(None,tracer=tracer):
                project = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "story_project.json").read_text(encoding="utf8"))
                script_ref = {"asset_id":"offline-story","revision_id":"r2","sha256":digest_json(project["script_source"]["script"])}
                clips, shots = [], {}
                for shot, color in zip(project["shots"], ("yellow","red","blue")):
                    sid = shot["id"]
                    path = folder / (sid + ".mp4")
                    _ffmpeg("-f","lavfi","-i",f"color={color}:s=64x64:r=30:d=1",
                            "-f","lavfi","-i","sine=frequency=440:sample_rate=48000:duration=1",
                            "-c:v","libx264","-c:a","aac","-shortest",path)
                    digest = file_sha256(path)
                    clips.append({"shot_id":sid,"path":str(path),"sha256":digest})
                    shots[sid] = {"script_ref":script_ref,"operation":"generated" if sid=="S003" else "reused","media_sha256":digest}
                manifest = export_preview(clips,folder / "preview")
                preview = {**manifest,"script_ref":script_ref,"export_manifest_sha256":digest_json(manifest)}
                production={"project":project,"preview":preview,"script_ref":script_ref,"shots":shots}
                report = inspect_cut(production,"S002","S003",folder/"cut")
                self.assertEqual(report["cut"],{"preview_time_s":2.0,"frame_index":60,"fps":30})
                self.assertEqual([f["preview_frame_index"] for f in report["frames"]],[59,60])
                self.assertNotEqual(report["frames"][0]["sha256"],report["frames"][1]["sha256"])
                self.assertEqual(report["window"]["cut_in_window_s"],1)
                self.assertEqual(report["audio"]["stream"]["channels"],2)
                self.assertLess(report["audio"]["left"]["mean_volume_dbfs"],0)
                self.assertEqual(report["audiovisual_continuity"],"UNKNOWN")
                with self.assertRaisesRegex(ValueError,"timeline.adjacent"):
                    inspect_cut(production,"S001","S003",folder/"wrong-cut")
                Path(preview["preview_path"]).write_bytes(b"tampered offline test video")
                with self.assertRaisesRegex(ValueError,"preview.file"):
                    inspect_cut(production,"S002","S003",folder/"tampered-cut")
                self.assertFalse((folder/"tampered-cut").exists())
