"""Mechanical media inspection tests; synthetic/local media only, no APIs."""
import copy
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pianchang import media_tools
from pianchang.media_tools import MediaError, find_ffmpeg, find_ffprobe, inspect_media


class MediaValidationTests(unittest.TestCase):
    def test_invalid_input_and_limits_fail_before_starting_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "source.mp4"
            media.write_bytes(b"fake-source")
            with patch("pianchang.media_tools._run") as run:
                for settings in ({"interval_s": 0}, {"interval_s": float("nan")},
                                 {"interval_s": float("inf")}, {"interval_s": True},
                                 {"max_frames": 0}, {"max_frames": 101}, {"max_frames": True}):
                    with self.subTest(settings=settings), self.assertRaises(MediaError):
                        inspect_media(media, Path(directory) / "out", **settings)
                with self.assertRaises(MediaError):
                    inspect_media(Path(directory) / "missing.mp4", Path(directory) / "out")
                run.assert_not_called()

    def test_explicit_tool_path_is_used_or_fails_without_silent_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "custom tool.exe"
            binary.write_bytes(b"fixture")
            with patch.dict(os.environ, {"FFMPEG_PATH": str(binary), "FFPROBE_PATH": str(binary)}):
                self.assertEqual(find_ffmpeg(), binary.resolve())
                self.assertEqual(find_ffprobe(), binary.resolve())
            with patch.dict(os.environ, {"FFMPEG_PATH": str(binary) + ".missing"}):
                with self.assertRaises(MediaError):
                    find_ffmpeg()

    def test_nonempty_output_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "source.mp4"
            media.write_bytes(b"video")
            output = root / "out"
            output.mkdir()
            note = output / "user-note.txt"
            note.write_text("keep me", encoding="utf-8")
            with self.assertRaises(MediaError):
                inspect_media(media, output)
            self.assertEqual(note.read_text(encoding="utf-8"), "keep me")

    def test_actual_pts_and_cap_do_not_relabel_requested_times_as_frame_times(self):
        index = media_tools._frame_index({"frames": [
            {"best_effort_timestamp_time": "2.000"},
            {"best_effort_timestamp_time": "2.040"},
            {"best_effort_timestamp_time": "2.080"},
            {"best_effort_timestamp_time": "2.120"}]})
        selected, sampling = media_tools._sample(index, 0.05, 3)
        self.assertEqual([f["requested_time_s"] for f in selected], [0.0, 0.05, 0.12])
        self.assertEqual([f["time_s"] for f in selected], [0.0, 0.04, 0.12])
        self.assertEqual([f["pts_time_s"] for f in selected], [2.0, 2.04, 2.12])
        self.assertTrue(sampling["capped"])
        self.assertEqual(sampling["coverage_note"], media_tools.COVERAGE_NOTE)
        with self.assertRaises(MediaError):
            media_tools._frame_index({"frames": [{}]})


class MockInspectionTests(unittest.TestCase):
    def fake_run(self, args, *, timeout):
        args = [str(a) for a in args]
        self.commands.append(args)
        if "-version" in args:
            return subprocess.CompletedProcess(args, 0, "ffmpeg test fixture version\n", "")
        if "-show_streams" in args:
            body = {"streams": [{"codec_type": "video", "codec_name": "h264", "width": 160,
                                  "height": 120, "duration": "1.2"}, {"codec_type": "audio"}]}
            return subprocess.CompletedProcess(args, 0, json.dumps(body), "")
        if "-show_frames" in args:
            body = {"frames": [{"best_effort_timestamp_time": str(2.0 + n / 10)} for n in range(12)]}
            return subprocess.CompletedProcess(args, 0, json.dumps(body), "")
        if "null" in args:
            if self.decode_timeout:
                raise subprocess.TimeoutExpired(args, timeout)
            return subprocess.CompletedProcess(args, self.decode_code, "", self.decode_error)
        if "-vf" in args:
            if self.extract_failure:
                return subprocess.CompletedProcess(args, 1, "", "encoder failed")
            Path(args[-1]).write_bytes(b"\xff\xd8synthetic-jpeg\xff\xd9")
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)

    def setUp(self):
        self.commands = []
        self.decode_code = 0
        self.decode_error = ""
        self.decode_timeout = False
        self.extract_failure = False
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.media = self.root / "video with spaces.mp4"
        self.media.write_bytes(b"this is a mock source, not real media")
        self.output = self.root / "inspect"
        self.patches = [patch("pianchang.media_tools.find_ffmpeg", return_value=Path("ffmpeg.exe")),
                        patch("pianchang.media_tools.find_ffprobe", return_value=Path("ffprobe.exe")),
                        patch("pianchang.media_tools._run", side_effect=self.fake_run)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    def test_decode_failure_is_recorded_and_never_passes(self):
        self.decode_code = 1
        self.decode_error = "Invalid data found when processing input"
        result = inspect_media(self.media, self.output, interval_s=0.5)
        self.assertEqual(result["decode"]["status"], "FAIL")
        self.assertEqual(result["inspection_status"], "FAIL")
        self.assertEqual(result["media_sha256"], hashlib.sha256(self.media.read_bytes()).hexdigest())
        self.assertEqual(result["metadata"]["audio_stream_count"], 1)
        decode = next(c for c in self.commands if "null" in c)
        self.assertIn("-xerror", decode)
        self.assertIn("explode", decode)
        self.assertIn(str(self.media.resolve()), decode)
        self.assertEqual(json.loads((self.output / "inspection.json").read_text(encoding="utf-8")), result)

    def test_decode_timeout_and_failed_extraction_are_unknown_not_pass(self):
        self.decode_timeout = True
        result = inspect_media(self.media, self.output)
        self.assertEqual(result["decode"]["status"], "UNKNOWN")
        self.assertEqual(result["inspection_status"], "UNKNOWN")

    def test_missing_frames_do_not_become_successful_artifacts(self):
        self.extract_failure = True
        result = inspect_media(self.media, self.output)
        self.assertEqual(result["decode"]["status"], "PASS")
        self.assertEqual(result["inspection_status"], "UNKNOWN")
        self.assertFalse(result["sampling"]["extraction_complete"])
        self.assertEqual(result["frames"], [])
        self.assertTrue(result["sampling"]["extraction_errors"])

    def test_matching_cache_reused_and_modified_frames_or_source_rejected(self):
        result = inspect_media(self.media, self.output)
        self.commands.clear()
        self.assertEqual(inspect_media(self.media, self.output), result)
        self.assertEqual(self.commands, [])
        first = Path(result["frames"][0]["path"])
        original = first.read_bytes()
        first.write_bytes(b"edited")
        with self.assertRaises(MediaError):
            inspect_media(self.media, self.output)
        first.write_bytes(original)
        with self.assertRaises(MediaError):
            inspect_media(self.media, self.output, interval_s=0.5)
        self.media.write_bytes(b"replacement")
        with self.assertRaises(MediaError):
            inspect_media(self.media, self.output)

    def test_cache_cannot_silently_lose_a_frame_inventory_entry(self):
        result = inspect_media(self.media, self.output)
        result["frames"].pop()
        (self.output / "inspection.json").write_text(json.dumps(result), encoding="utf-8")
        with self.assertRaises(MediaError):
            inspect_media(self.media, self.output)


class RealMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.ffmpeg, cls.ffprobe = find_ffmpeg(), find_ffprobe()
        except MediaError as exc:
            raise unittest.SkipTest(str(exc))
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.media = cls.root / "synthetic source.mp4"
        args = [str(cls.ffmpeg), "-nostdin", "-hide_banner", "-v", "error", "-f", "lavfi", "-i",
                "testsrc2=size=160x120:rate=10:duration=1.2", "-f", "lavfi", "-i",
                "sine=frequency=440:sample_rate=8000:duration=1.2", "-shortest", "-c:v", "libx264",
                "-pix_fmt", "yuv420p", "-c:a", "aac", str(cls.media)]
        subprocess.run(args, check=True, capture_output=True, timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_real_full_decode_and_pts_bound_jpegs(self):
        result = inspect_media(self.media, self.root / "real", interval_s=0.3)
        self.assertEqual(result["decode"]["status"], "PASS")
        self.assertEqual(result["inspection_status"], "PASS")
        self.assertEqual(result["metadata"]["width"], 160)
        self.assertEqual(result["metadata"]["height"], 120)
        self.assertEqual(result["metadata"]["audio_stream_count"], 1)
        self.assertTrue(result["sampling"]["extraction_complete"])
        self.assertEqual([f["time_s"] for f in result["frames"]], [0.0, 0.3, 0.6, 0.9, 1.1])
        for frame in result["frames"]:
            self.assertEqual(frame["time_s"], round(frame["source_frame_index"] / 10, 9))
            data = Path(frame["path"]).read_bytes()
            self.assertTrue(data.startswith(b"\xff\xd8"))
            self.assertEqual(hashlib.sha256(data).hexdigest(), frame["sha256"])

    def test_real_sample_cap_is_explicit_and_retains_last_actual_frame(self):
        result = inspect_media(self.media, self.root / "capped", interval_s=0.1, max_frames=3)
        self.assertEqual(len(result["frames"]), 3)
        self.assertTrue(result["sampling"]["capped"])
        self.assertEqual(result["frames"][-1]["time_s"], 1.1)
        self.assertEqual(result["frames"][-1]["source_frame_index"], 11)

    def test_real_nonvideo_input_fails_without_success_manifest(self):
        invalid = self.root / "invalid.mp4"
        invalid.write_bytes(b"this is not a video")
        output = self.root / "invalid-inspection"
        with self.assertRaises(MediaError):
            inspect_media(invalid, output)
        self.assertFalse((output / "inspection.json").exists())


if __name__ == "__main__":
    unittest.main()
