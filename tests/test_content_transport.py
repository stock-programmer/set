"""Explicit offline FFmpeg evidence: no model/provider requests."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pianchang.content_review import _asset,_ffmpeg,fit_video_input
from trace_system import get_tracer,use_context


class ContentTransportTests(unittest.TestCase):
    def test_oversized_complete_video_is_fitted_without_cropping_or_changing_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);tracer=get_tracer(root/'trace')
            with tracer.storage_session(),use_context(None,tracer=tracer),tracer.trace('offline.content.transport',kind='test',inputs={'label':'explicit offline media fixture'}):
                source=root/'uncompressed.avi'
                _ffmpeg(['-f','lavfi','-i','testsrc=size=576x576:rate=10','-t','1','-c:v','rawvideo','-pix_fmt','bgr24',str(source)])
                original=_asset(source);times=[n/10 for n in range(10)]
                video={**original,'id':'V0','fps':10,'duration_s':1.0,'frame_times_s':times,
                    'binding':{'source':original,'fps':10,'width':576,'include_audio':False},'scope':'explicit offline whole clip at 10fps'}
                self.assertGreater(original['bytes'],9*1024*1024//4*3)
                fitted=fit_video_input(video,root/'prepared')
                self.assertLessEqual(fitted['bytes'],9*1024*1024//4*3)
                self.assertEqual(fitted['frame_times_s'],times)
                self.assertEqual(fitted['binding']['source'],original)
                self.assertEqual(fitted['binding']['transport_parent'],original)
                self.assertEqual(_asset(source),original)
                with patch('pianchang.content_review._ffmpeg',side_effect=AssertionError('Cached evidence must not re-encode')):
                    self.assertEqual(fit_video_input(video,root/'prepared'),fitted)


if __name__=='__main__':unittest.main()
