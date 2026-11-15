"""Explicit offline interruption/reconciliation tests; no provider requests."""
import json
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch

from pianchang.bailian_text import TextConfig
from pi_bridge.runtime import PiLoop
from pi_bridge.recovery import reconcile_checkpoint,reconcile_design,strategy_external_usage
from trace_system import TraceError,event,get_tracer,span,use_context


class InterruptionTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.tracer=get_tracer(self.root/'trace');self.enterContext(self.tracer.storage_session())
        self.enterContext(use_context(None,tracer=self.tracer))
        self.task=self.enterContext(self.tracer.trace('offline.pi.interruption',kind='test',inputs={'label':'explicit offline fixture'}))
        self.client=SimpleNamespace(config=TextConfig(api_key='offline-no-network'))

    def loop(self,**kwargs):
        return PiLoop(self.client,[],self.root/'agent',snapshot={},finish_check=lambda:{'complete':False},**kwargs)

    def test_original_trace_failure_survives_and_checkpoint_retains_pending_and_budget(self):
        loop=self.loop();loop.record={'status':'running','steps':9,'video_generations':3,
            'pending_tool':{'tool':'design_repair_strategy','call_id':'offline-call'},'limits':{'max_video_generations':9}}
        original=TraceError('offline primary storage failure')
        with patch.object(loop,'_run',side_effect=original),patch('pi_bridge.runtime.persist',side_effect=AssertionError('Emergency checkpoint must not depend on Trace writes')):
            with self.assertRaises(TraceError) as raised:loop.run('offline goal')
        self.assertIs(raised.exception,original)
        saved=json.loads((self.root/'agent/run.json').read_text('utf-8'))
        self.assertEqual(saved['status'],'interrupted');self.assertTrue(saved['trace_incomplete'])
        self.assertEqual(saved['pending_tool']['call_id'],'offline-call')
        self.assertEqual((saved['steps'],saved['video_generations'],saved['limits']['max_video_generations']),(9,3,9))

    def test_prepare_failure_also_saves_terminal_state(self):
        loop=self.loop(prepare=lambda:(_ for _ in ()).throw(ValueError('offline preparation failure')))
        with self.assertRaisesRegex(ValueError,'offline preparation'):loop.run('offline goal')
        saved=json.loads((self.root/'agent/run.json').read_text('utf-8'))
        self.assertEqual(saved['status'],'interrupted');self.assertFalse(saved['trace_incomplete'])
        self.assertFalse(saved['result']['task_complete'])

    def test_failed_final_span_commit_cannot_leave_a_completed_checkpoint(self):
        loop=self.loop();original_end=self.tracer.end_span
        def completed(*args,**kwargs):
            loop.record={'status':'completed','steps':9,'video_generations':3}
            return {'task_complete':True}
        def fail_end(context,**kwargs):
            if kwargs.get('status')=='OK':raise TraceError('offline final Trace commit failed')
            return original_end(context,**kwargs)
        with patch.object(loop,'_run',side_effect=completed),patch.object(self.tracer,'end_span',side_effect=fail_end):
            with self.assertRaisesRegex(TraceError,'offline final Trace'):loop.run('offline goal')
        saved=json.loads((self.root/'agent/run.json').read_text('utf-8'))
        self.assertEqual(saved['status'],'interrupted');self.assertFalse(saved['result']['task_complete'])

    def test_failed_checkpoint_storage_does_not_mask_primary_failure(self):
        loop=self.loop();primary=TraceError('offline primary failure')
        with patch.object(loop,'_run',side_effect=primary),patch.object(loop,'_interrupted_checkpoint',side_effect=OSError('offline read-only disk')):
            with self.assertRaises(TraceError) as raised:loop.run('offline goal')
        self.assertIs(raised.exception,primary);self.assertIn('OSError',raised.exception.__notes__[0])

    def test_cleanup_escalates_to_kill_and_revokes_gateway_even_when_tracing_close_fails(self):
        class Process:
            returncode=None;stdin=Mock();stdout=Mock();stderr=Mock()
            def poll(self):return self.returncode
            def wait(self,timeout):
                if self.returncode is None:raise subprocess.TimeoutExpired(['offline-node'],timeout)
                return self.returncode
            def terminate(self):pass
            def kill(self):self.returncode=-9
        process=Process();gateway=SimpleNamespace(close=Mock(side_effect=TraceError('offline cleanup Trace failure')),_release=Mock())
        receipt,error=self.loop()._shutdown_runtime(process,Mock(),gateway)
        self.assertTrue(receipt['confirmed_exit']);self.assertEqual(receipt['returncode'],-9)
        self.assertIsInstance(error,TraceError);gateway._release.assert_called_once()
        self.assertEqual([e['phase'] for e in receipt['errors']],['close','terminate','gateway_close'])

    def stopped_fixture(self):
        worker=self.tracer.start_span('agent.task.video',kind='agent',new_trace=True,inputs={'label':'explicit offline failed worker'})
        self.tracer.end_span(worker.context,status='ERROR',error={'type':'TimeoutExpired','message':'offline secondary shutdown timeout',
            'stack':'offline fixture: trace_system.core.TraceError: original storage failure'})
        work=self.root/'outputs/collaborations/runs'/('1'*32)/('video-spec-'+'2'*32);(work/'agent').mkdir(parents=True)
        record={'backend':'pi','status':'running','trace_context':worker.context,'steps':9,'video_generations':3,'limits':{'max_steps':56,'max_video_generations':9}}
        (work/'agent/run.json').write_text(json.dumps(record),encoding='utf-8')
        source={'id':'source','kind':'studio_video','status':'failed','project_id':'project','message_id':'2'*32,'run_id':'1'*32,
            '_autonomy_enabled':True,'_autonomy_body':{'plan_id':'offline-frozen'}}
        owner={'id':'owner','status':'interrupted','project_id':'project','commission_id':'2'*32}
        service=SimpleNamespace(root=self.root,tracer=self.tracer,lock=threading.RLock(),state={'jobs':[source,owner]})
        return service,source,owner,work

    def test_closed_error_root_reconciles_stale_running_without_budget_reset(self):
        service,source,owner,work=self.stopped_fixture()
        result=reconcile_checkpoint(service,source,work);self.assertTrue(result['changed'])
        record=json.loads((work/'agent/run.json').read_text('utf-8'))
        self.assertEqual(record['status'],'interrupted');self.assertEqual(record['video_generations'],3)
        self.assertEqual(record['failure']['primary_error'],'original storage failure')
        self.assertTrue(list((work/'agent').glob('before-reconcile-*.json')))
        self.assertFalse(reconcile_checkpoint(service,source,work)['changed'])

    def test_active_owner_prevents_reconciliation_even_with_old_failed_root(self):
        service,source,owner,work=self.stopped_fixture();owner['status']='running'
        self.assertEqual(reconcile_checkpoint(service,source,work)['reason'],'owner_active')
        self.assertEqual(json.loads((work/'agent/run.json').read_text('utf-8'))['status'],'running')

    def test_reboot_owner_receipt_reconciles_open_child_without_inventing_an_end(self):
        service,source,owner,work=self.stopped_fixture()
        child=self.tracer.start_span('offline.open_child',kind='agent',new_trace=True,inputs={'label':'offline reboot fixture'})
        record=json.loads((work/'agent/run.json').read_text('utf-8'));record['trace_context']=child.context
        (work/'agent/run.json').write_text(json.dumps(record),encoding='utf-8')
        self.assertFalse(reconcile_checkpoint(service,source,work)['changed'],'An absent child end proves nothing by itself')
        parent=self.tracer.start_span('offline.owner_restart',kind='task',new_trace=True,inputs={'label':'offline startup fixture'})
        with use_context(parent.context,tracer=self.tracer):
            event('task.interrupted_detected',{'job_id':source['id'],'reason':'offline fixture: exclusive process lock reacquired'})
        self.tracer.end_span(parent.context,status='UNKNOWN');source['trace_id']=parent.trace_id
        self.assertTrue(reconcile_checkpoint(service,source,work)['changed'])
        self.assertEqual(self.tracer.get_events(child.trace_id,span_id=child.span_id)[-1]['kind'],'span.start')

    def test_project_scheduler_recovers_original_order_after_reconciling_checkpoint(self):
        from pianchang.video_autonomy import enqueue_recovery
        service,source,owner,work=self.stopped_fixture()
        source['error']='offline prior failure'
        service.executor=Mock();service._save=Mock();service._execute=Mock();service._project=Mock(return_value=(None,{'label':'offline frozen project'}))
        self.assertEqual(enqueue_recovery(service)['jobs'],['source'])
        self.assertEqual(source['message_id'],'2'*32);self.assertEqual(source['status'],'queued')
        self.assertIsNone(source['error']);self.assertEqual(source['previous_execution']['error'],'offline prior failure')
        self.assertEqual(json.loads((work/'agent/run.json').read_text('utf-8'))['video_generations'],3)
        service.executor.submit.assert_called_once()

    def test_trace_failure_requires_explicit_health_recovery_and_preserves_original_budget(self):
        from pianchang.video_autonomy import enqueue_recovery
        service,source,owner,work=self.stopped_fixture();source['trace_incomplete']=True
        service.executor=Mock();service._save=Mock();service._execute=Mock();service._project=Mock(return_value=(None,{'label':'offline frozen project'}))
        self.assertEqual(enqueue_recovery(service)['jobs'],[])
        self.assertEqual(enqueue_recovery(service,recover_trace_failures=True)['jobs'],['source'])
        self.assertFalse(source['trace_incomplete']);self.assertTrue(source['trace_recovery_probe'])
        self.assertEqual(json.loads((work/'agent/run.json').read_text('utf-8'))['limits']['max_video_generations'],9)

    def test_unconfirmed_design_is_charged_once_and_not_replayed(self):
        with span('pi.loop.execute_tool',kind='tool'):
            event('pi.tool.request',{'tool':'design_repair_strategy','call_id':'offline-design','arguments':{'shot_id':'S002'}})
            with span('video.autonomy.design_strategy',kind='tool'):
                with span('bailian.text.complete_json',kind='model'):
                    event('http.request',{'url':'https://offline.invalid/chat/completions','wire_body_sha256':'f'*64,'body':{'label':'offline fixture; never sent'}})
        record={'recovery_origin':self.task.context,'pending_tool':{'tool':'design_repair_strategy','call_id':'offline-design','arguments':{'shot_id':'S002'}}}
        first=reconcile_design(self.root,record,self.tracer);second=reconcile_design(self.root,record,self.tracer)
        self.assertEqual(first,second);self.assertEqual(strategy_external_usage(self.root),1)
        self.assertEqual(first['new_video_posts'],0);self.assertEqual(first['request_evidence'][0]['wire_body_sha256'],'f'*64)
        with span('pi.loop.execute_tool',kind='tool'):
            event('pi.tool.request',{'tool':'design_repair_strategy','call_id':'offline-reserved-design','arguments':{'shot_id':'S002'}})
            with span('video.autonomy.design_strategy',kind='tool'):
                with span('pi.loop.before_model',kind='validation'):
                    event('offline.pi.step_reserved',{'label':'explicit offline fixture','steps':10})
                with span('bailian.text.complete_json',kind='model'):
                    event('http.request',{'url':'https://offline.invalid/chat/completions','wire_body_sha256':'e'*64,'body':{'label':'offline; never sent'}})
        record['pending_tool']['call_id']='offline-reserved-design'
        charged=reconcile_design(self.root,record,self.tracer)
        self.assertEqual(charged['external_calls_reserved'],0)
        self.assertEqual(len(charged['already_reserved_pi_steps']),1)
        self.assertEqual(strategy_external_usage(self.root),1,'A helper already charged to Pi steps is not charged twice')


if __name__=='__main__':unittest.main()
