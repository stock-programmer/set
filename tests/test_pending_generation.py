"""Offline known-task recovery; never queries or submits to a provider."""
import json
import ssl
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from urllib.error import URLError
from urllib.request import HTTPSHandler

from pianchang.bailian_video import BailianVideoClient,VideoConfig,VideoError
from trace_system import get_tracer,use_context
from pianchang.video_autonomy import VideoAutonomy,recoverable_order


class PendingGenerationTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.tracer=get_tracer(self.root/'trace');self.enterContext(self.tracer.storage_session())
        self.enterContext(use_context(None,tracer=self.tracer))
        self.task=self.enterContext(self.tracer.trace('offline.pending_generation',kind='test',inputs={'label':'explicit offline mock'}))

    def fixture(self):
        ref={'asset_id':'offline/script','revision_id':'r1','sha256':'a'*64};path=self.root/'job.json'
        data={'task_id':'offline-task','status':'RUNNING','metadata':{'script_asset_ref':ref,'shot_id':'S003','dependency_sha256':'b'*64}}
        path.write_text(json.dumps(data),encoding='utf-8')
        workspace=SimpleNamespace(directory=self.root,ref=ref,impact={'shots':{'S003':{'target':{'sha256':'b'*64}}}},
            repair=SimpleNamespace(plans={}),video_config_for=Mock(),adopt_generated=Mock())
        controller=VideoAutonomy(workspace)
        controller.blocked={'S003':{'task_id':'offline-task','manifest_path':str(path)}}
        return controller,workspace,path

    def test_original_task_is_polled_then_adopted_without_submit_or_budget_reset(self):
        c,w,path=self.fixture();runner=Mock()
        runner.resume.return_value={'status':'RUNNING','task_id':'offline-task'}
        with patch('pianchang.video_agent.VideoRunner',return_value=runner):
            first=c.observe_pending(wait=False);c.observe_pending(wait=False)
            runner.resume.assert_called_once_with(path,wait=False)
            self.assertEqual(first['new_video_posts'],0);self.assertFalse(first['budget_reset'])
            c.blocked['S003']['next_poll_at']=0
            runner.resume.return_value={'status':'SUCCEEDED','task_id':'offline-task','local_video_path':'offline-media.mp4'}
            second=c.observe_pending(wait=False)
        runner.submit.assert_not_called();self.assertEqual(c.blocked,{})
        w.adopt_generated.assert_called_once_with('S003',runner.resume.return_value,repair=None)
        self.assertEqual(second['observations'][0]['status'],'recovered_and_reviewed')

    def test_wrong_frozen_binding_never_queries_or_adopts(self):
        c,w,path=self.fixture();d=json.loads(path.read_text('utf-8'));d['metadata']['script_asset_ref']['sha256']='c'*64
        path.write_text(json.dumps(d),encoding='utf-8')
        with patch('pianchang.video_agent.VideoRunner') as runner:c.observe_pending(wait=False)
        runner.assert_not_called();w.adopt_generated.assert_not_called()
        self.assertIn('绑定不符',c.blocked['S003']['reason'])

    def test_existing_provider_wait_is_recoverable_without_new_commission(self):
        c,w,path=self.fixture();(self.root/'agent').mkdir()
        (self.root/'agent/run.json').write_text(json.dumps({'steps':12,'limits':{'max_steps':56},'result':{'reason':'pi_stopped'}}),encoding='utf-8')
        (self.root/'autonomy.json').write_text(json.dumps({'provider_blockers':c.blocked}),encoding='utf-8')
        self.assertTrue(recoverable_order(self.root))

    def test_tls_context_keeps_verification_without_mutable_default_context(self):
        with patch('ssl._create_default_https_context',side_effect=FileNotFoundError('offline inherited hook')):
            client=BailianVideoClient(VideoConfig(api_key='offline-no-network'))
        for opener in (client._api_opener,client._media_opener):
            context=next(h._context for h in opener.handlers if isinstance(h,HTTPSHandler))
            self.assertTrue(context.check_hostname);self.assertEqual(context.verify_mode,ssl.CERT_REQUIRED)
            self.assertIsNone(context.keylog_filename)

    def test_transport_error_retains_real_cause_and_filename(self):
        client=BailianVideoClient(VideoConfig(api_key='offline-no-network'))
        client._api_opener=Mock();client._api_opener.open.side_effect=URLError(FileNotFoundError(2,'offline missing','offline-keylog.log'))
        with self.assertRaises(VideoError):client.get_task('offline-task')
        row=self.tracer.get_events(self.task.trace_id,names=['http.transport_error'])[0]
        data=self.tracer.resolve(row['data'])
        self.assertEqual((data['cause_type'],data['errno'],data['filename']),('FileNotFoundError',2,'offline-keylog.log'))
        self.assertEqual(data['provider_outcome'],'existing task unchanged')
        client._api_opener.open.assert_called_once()


if __name__=='__main__':unittest.main()
