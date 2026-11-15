"""Offline/local preview export checks; no model requests or paid APIs."""
import copy
import hashlib
import json
import math
import os
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pianchang import episode_export
from pianchang.episode_export import ExportError, export_preview
from pianchang.media_tools import MediaError, find_ffmpeg, find_ffprobe


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class InputTests(unittest.TestCase):
    def test_bad_inputs_fail_before_any_media_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"local fixture")
            valid = {"shot_id": "S001", "path": source, "sha256": sha256(source)}
            cases = [[], None, [None], [{**valid, "shot_id": ""}], [{**valid, "sha256": ""}],
                     [{**valid, "sha256": "0" * 64}], [{**valid, "path": root / "missing.mp4"}]]
            with patch("pianchang.episode_export._run") as run:
                for clips in cases:
                    with self.subTest(clips=clips), self.assertRaises(ExportError):
                        export_preview(clips, root / "out")
                run.assert_not_called()

    def test_nonempty_output_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"fixture")
            output = root / "export"
            output.mkdir()
            note = output / "note.txt"
            note.write_text("user content", encoding="utf-8")
            with patch("pianchang.episode_export._run") as run, self.assertRaises(ExportError):
                export_preview([{"shot_id": "S001", "path": source, "sha256": sha256(source)}], output)
            run.assert_not_called()
            self.assertEqual(note.read_text(encoding="utf-8"), "user content")

    def test_media_commands_use_argument_lists_without_shell(self):
        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch("pianchang.episode_export.subprocess.run", return_value=completed) as run:
            episode_export._run(["ffmpeg.exe", "-i", "a'; bad name.mp4"])
        self.assertEqual(run.call_args.args[0], ["ffmpeg.exe", "-i", "a'; bad name.mp4"])
        self.assertFalse(run.call_args.kwargs.get("shell", False))

    def test_decoder_error_cannot_produce_successful_validation(self):
        probe = {"video": {"width": 720, "height": 1280, "codec_name": "h264",
                            "pix_fmt": "yuv420p", "avg_frame_rate": "30/1"},
                 "audio": [{"codec_name": "aac", "sample_rate": "48000", "channels": 2}],
                 "duration_s": 1.0, "container_duration_s": 1.0}
        with patch("pianchang.episode_export._probe", return_value=probe), patch("pianchang.episode_export._run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "decoder error")
            with self.assertRaises(ExportError):
                episode_export._verify("ffmpeg", "ffprobe", "preview.mp4", 1.0, 1)
            self.assertIn("-xerror", [str(a) for a in run.call_args.args[0]])
        bad_duration = copy.deepcopy(probe)
        bad_duration["container_duration_s"] = 7.0
        with patch("pianchang.episode_export._probe", return_value=bad_duration), patch("pianchang.episode_export._run") as run:
            with self.assertRaises(ExportError):
                episode_export._verify("ffmpeg", "ffprobe", "preview.mp4", 1.0, 1)
            run.assert_not_called()


class RealExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.ffmpeg, cls.ffprobe = find_ffmpeg(), find_ffprobe()
        except MediaError as exc:
            raise unittest.SkipTest(str(exc))
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.first = cls.root / "red 'camera; a'.mp4"
        cls.second = cls.root / "蓝色 无声.mp4"
        common = [str(cls.ffmpeg), "-nostdin", "-hide_banner", "-v", "error"]
        subprocess.run(common + ["-f", "lavfi", "-i", "color=c=red:s=160x90:r=12:d=0.5",
                                 "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=22050:duration=0.5",
                                 "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                                 "-c:a", "aac", "-ac", "1", str(cls.first)],
                       check=True, capture_output=True, timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        subprocess.run(common + ["-f", "lavfi", "-i", "color=c=blue:s=96x160:r=20:d=0.4",
                                 "-c:v", "libx264", "-pix_fmt", "yuv420p", str(cls.second)],
                       check=True, capture_output=True, timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        cls.clips = [{"shot_id": "S001", "path": str(cls.first), "sha256": sha256(cls.first)},
                     {"shot_id": "S002", "path": str(cls.second), "sha256": sha256(cls.second)}]

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def result(self):
        return export_preview(self.clips, self.root / "ordered-preview")

    def pixel(self, path, time):
        args = [str(self.ffmpeg), "-nostdin", "-hide_banner", "-v", "error", "-ss", str(time),
                "-i", str(path), "-map", "0:v:0", "-frames:v", "1", "-vf", "scale=1:1",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
        result = subprocess.run(args, check=True, capture_output=True, timeout=30,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(len(result.stdout), 3)
        return tuple(result.stdout)

    def test_real_preview_has_correct_format_audio_order_and_no_acceptance_claim(self):
        result = self.result()
        self.assertEqual(result["status"], "PREVIEW_ONLY")
        self.assertIs(result["accepted"], False)
        self.assertEqual(result["metadata"]["width"], 720)
        self.assertEqual(result["metadata"]["height"], 1280)
        self.assertAlmostEqual(result["metadata"]["fps"], 30, places=2)
        self.assertEqual(result["metadata"]["audio_sample_rate"], 48000)
        self.assertEqual(result["metadata"]["audio_channels"], 2)
        self.assertEqual(result["validation"]["decode"], "PASS")
        self.assertAlmostEqual(result["expected_duration_s"], 0.9, places=3)
        self.assertLess(abs(result["total_duration_s"] - 0.9), 0.15)
        self.assertEqual([c["shot_id"] for c in result["clips"]], ["S001", "S002"])
        self.assertEqual([c["audio_policy"] for c in result["clips"]],
                         ["first_source_audio_stream", "generated_silence"])
        early, late = self.pixel(result["preview_path"], 0.2), self.pixel(result["preview_path"], 0.7)
        self.assertGreater(early[0], early[2] + 20)
        self.assertGreater(late[2], late[0] + 20)
        self.assertEqual(sha256(result["preview_path"]), result["preview_sha256"])
        self.assertEqual([sha256(c["path"]) for c in self.clips], [c["sha256"] for c in self.clips])
        saved = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        self.assertEqual(saved, result)
        self.assertEqual(sorted(p.name for p in Path(result["preview_path"]).parent.iterdir()),
                         ["export.json", "preview.mp4"])

    def test_same_inputs_reuse_verified_preview_without_transcoding_again(self):
        first = self.result()
        with patch("pianchang.episode_export._run") as run:
            self.assertEqual(self.result(), first)
            run.assert_not_called()

    def test_source_audio_survives_and_silent_shot_has_silence(self):
        result = self.result()

        def rms(start):
            args = [str(self.ffmpeg), "-nostdin", "-hide_banner", "-v", "error", "-ss", str(start),
                    "-i", result["preview_path"], "-map", "0:a:0", "-t", "0.15", "-ac", "1",
                    "-ar", "48000", "-f", "s16le", "pipe:1"]
            audio = subprocess.run(args, check=True, capture_output=True, timeout=30,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            self.assertGreater(len(audio), 1000)
            values = struct.unpack("<" + "h" * (len(audio) // 2), audio)
            return math.sqrt(sum(value * value for value in values) / len(values))

        voiced, silent = rms(0.1), rms(0.65)
        self.assertGreater(voiced, 500)
        self.assertLess(silent, 50)

    def test_reversed_order_cannot_reuse_previous_export_directory(self):
        self.result()
        with self.assertRaises(ExportError):
            export_preview(list(reversed(self.clips)), self.root / "ordered-preview")

    def test_modified_preview_hash_is_rejected_and_existing_file_preserved(self):
        result = self.result()
        path = Path(result["preview_path"])
        original = path.read_bytes()
        try:
            path.write_bytes(b"replaced video")
            with self.assertRaises(ExportError):
                self.result()
            self.assertEqual(path.read_bytes(), b"replaced video")
        finally:
            path.write_bytes(original)

    def test_invalid_mp4_is_refused_without_export_artifacts(self):
        invalid = self.root / "not-video.mp4"
        invalid.write_bytes(b"not a video")
        output = self.root / "invalid-export"
        with self.assertRaises(ExportError):
            export_preview([{"shot_id": "broken", "path": invalid, "sha256": sha256(invalid)}], output)
        self.assertFalse((output / "preview.mp4").exists())
        self.assertFalse((output / "export.json").exists())


if __name__ == "__main__":
    unittest.main()
