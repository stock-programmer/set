"""Explicit offline metadata-probe mocks; no provider exchanges."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pianchang.reference_assets import _media_info
from trace_system import Tracer, use_context


class ProbeCacheTests(unittest.TestCase):
    def test_same_bytes_reuse_metadata_and_changed_bytes_are_probed_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);tool=root/"offline-ffprobe.exe";tool.write_bytes(b"offline mock, never executed")
            first=root/"first";second=root/"second"
            first.write_bytes(b"offline mocked image 1");second.write_bytes(first.read_bytes())
            probe={"streams":[{"codec_name":"mjpeg","width":240,"height":320}]}
            tracer=Tracer(root/"traces")
            with use_context(None,tracer=tracer),tracer.trace("offline.probe_cache") as task, \
                 patch("pianchang.reference_assets.find_ffprobe",return_value=tool),patch("pianchang.reference_assets._probe",return_value=probe) as call:
                self.assertEqual(_media_info(first,"image/jpeg"),{"width":240,"height":320})
                self.assertEqual(_media_info(second,"image/jpeg"),{"width":240,"height":320})
                self.assertEqual(call.call_count,1)
                second.write_bytes(b"offline mocked image 2")
                _media_info(second,"image/jpeg")
                self.assertEqual(call.call_count,2)
            self.assertTrue(tracer.verify(task.trace_id)["ok"])


if __name__=="__main__":unittest.main()
