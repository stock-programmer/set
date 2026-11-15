"""Offline checks for the font used to render a black-tail subtitle."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from pianchang import video_repair
from pianchang.media_tools import MediaError, find_ffmpeg
from trace_system import Tracer, use_context


class SubtitleFontTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        tracer = Tracer(Path(folder) / "traces")
        self.enterContext(use_context(None, tracer=tracer))
        self.enterContext(tracer.trace("offline.subtitle_font"))

    def test_macos_fallback_and_missing_font(self):
        font = Path("/System/Library/Fonts/STHeiti Medium.ttc")
        with (patch.object(sys, "platform", "darwin"),
              patch.object(Path, "is_file", autospec=True, side_effect=lambda path: path == font),
              patch.object(video_repair.os, "access", return_value=True) as readable):
            self.assertEqual(video_repair._subtitle_font(), font)
            readable.return_value = False
            with self.assertRaisesRegex(ValueError, "中文字幕字体"):
                video_repair._subtitle_font()

    @unittest.skipUnless(sys.platform == "darwin", "requires installed macOS fonts")
    def test_installed_macos_font_has_a_supported_file_header(self):
        with video_repair._subtitle_font().open("rb") as font:
            # TrueType collection, standalone TrueType or OpenType/CFF.
            self.assertIn(font.read(4), (b"ttcf", b"\x00\x01\x00\x00", b"OTTO"))

    @unittest.skipUnless(sys.platform in ("darwin", "win32"), "requires a supported OS font")
    def test_real_ffmpeg_renders_distinct_chinese_glyphs(self):
        try:
            ffmpeg = find_ffmpeg()
        except MediaError as exc:
            self.skipTest(str(exc))
        if "drawtext" not in subprocess.check_output([ffmpeg, "-hide_banner", "-filters"], text=True):
            self.skipTest("installed FFmpeg does not include the drawtext filter")
        font = video_repair._subtitle_font().resolve().as_posix().replace(":", "\\:")
        frames = []
        for glyph in ("山", "湖"):
            frames.append(subprocess.check_output([
                ffmpeg, "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=black:s=96x96",
                "-vf", f"drawtext=fontfile='{font}':text={glyph}:fontcolor=white:fontsize=48:x=0:y=0",
                "-frames:v", "1", "-pix_fmt", "gray", "-f", "rawvideo", "pipe:1",
            ], stdin=subprocess.DEVNULL, timeout=30))
        self.assertTrue(all(len(frame) == 96 * 96 and max(frame) > 128 for frame in frames))
        self.assertNotEqual(*frames)  # Missing-glyph boxes would be identical.
