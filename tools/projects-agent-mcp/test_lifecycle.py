"""Regression tests for fixed identity, preservation, uncertain outcomes and deduplication."""
import asyncio
import json
import sys
import unittest
from unittest.mock import AsyncMock, patch
from test_worker_model import WorkerModelTests, adapter
from lifecycle import manage, inspect


class LifecycleTests(WorkerModelTests):
    def setUp(self):
        super().setUp()
        (self.base/'identities.json').write_text(json.dumps({k:{'name':k,'registration_token':'test'} for k in ['demo','orchestrator']}))
        self.archived = False
        self.retired = False
        original = self.rpc.call.side_effect
        def call(method, params):
            if method == 'thread/read':
                self.calls.append((method,params))
                return {'thread': {'cwd':'/demo','path':'/archived_sessions/old' if self.archived else '/sessions/old',
                        'status':{'type':'active' if self.active else 'idle'},
                        'turns':[{'id':'running','status':'inProgress'}] if self.active else []}}
            if method in ('thread/archive','thread/unarchive','turn/interrupt'):
                self.calls.append((method, params))
                if method == 'turn/interrupt':self.active=False
                else:self.archived=method=='thread/archive'
                return {}
            return original(method,params)
        self.rpc.call.side_effect = call
        async def mail(tool,args):
            if tool=='retire_agent':self.retired=True
            if tool=='unretire_agent':self.retired=False
            return {'retired_at':'today'} if self.retired else {}
        adapter.mail.side_effect = mail

    def test_restore_archived_retired_keeps_identity_no_task(self):
        self.existing(); self.archived=True; self.retired=True
        result=manage(adapter,'demo','restore','restore-1')
        self.assertFalse(self.archived); self.assertFalse(self.retired)
        self.assertEqual(result['details']['thread_id'],'test-worker')
        self.assertNotIn('turn/start',dict(self.calls))
        self.assertEqual(result['task_execution'],'not_started')

    def test_stop_interrupts_holds_without_deleting(self):
        self.existing();self.active=True
        manage(adapter,'demo','stop','stop-1')
        self.assertFalse(self.active)
        self.assertTrue(json.loads((self.base/'worker-demo.json').read_text())['dispatch_held'])
        self.assertNotIn('thread/archive',dict(self.calls))
        with self.assertRaisesRegex(RuntimeError,'held'):adapter.wake('demo')

    def test_retire_archive_and_idempotency(self):
        self.existing()
        result=manage(adapter,'demo','retire','retire-1')
        self.assertTrue(self.archived);self.assertTrue(self.retired)
        before=len(self.calls)
        self.assertEqual(manage(adapter,'demo','retire','retire-1'),result)
        self.assertEqual(len(self.calls),before)
        with self.assertRaises(ValueError):manage(adapter,'demo','replace','retire-1')

    def test_replacement_keeps_previous_mapping_and_holds_dispatch(self):
        self.existing()
        manage(adapter,'demo','replace','replace-1')
        d=json.loads((self.base/'worker-demo.json').read_text())
        self.assertEqual(d['previous_sessions'],['test-worker'])
        self.assertTrue(d['dispatch_held'])
        self.assertIn('Lifecycle handshake only',dict(self.calls)['turn/start']['input'][0]['text'])
        backup=json.loads((self.base/'management/demo-replace-1.json').read_text())['before']
        self.assertEqual(backup['thread_id'],'test-worker')

    def test_unknown_mutation_cannot_be_retried_with_another_key(self):
        self.rpc.call.side_effect=RuntimeError('transport lost')
        with self.assertRaises(RuntimeError):manage(adapter,'demo','create','first')
        self.assertTrue(json.loads((self.base/'worker-demo.json').read_text())['creation_pending'])
        with self.assertRaisesRegex(RuntimeError,'Unresolved'):manage(adapter,'demo','create','second')
        with self.assertRaisesRegex(RuntimeError,'uncertain'):manage(adapter,'demo','create','first')

    def test_reject_arbitrary_project_operation_and_traversal(self):
        for project,op,key in [('../demo','create','x'),('demo','shell','x'),('demo','create','../x')]:
            with self.assertRaises(ValueError):manage(adapter,project,op,key)
        self.assertEqual(self.calls,[])

    def test_inspect_distinguishes_archive_from_not_loaded(self):
        self.existing();self.archived=True;self.retired=True
        s=inspect(adapter,'demo')
        self.assertEqual(s['mailbox'],'retired');self.assertTrue(s['archived'])

    def test_send_deduplicates_same_payload_and_holds_unknown(self):
        adapter.mail.side_effect=None
        adapter.mail.return_value={'deliveries':[{'payload':{'id':42}}]}
        r=adapter.send_once('demo','subject','body','task-1')
        count=adapter.mail.call_count
        self.assertEqual(adapter.send_once('demo','subject','body','task-1'),r)
        self.assertEqual(adapter.mail.call_count,count)
        self.assertEqual(adapter.admitted_ids('demo'),{1})  # mocked for wake tests
        adapter.mail.side_effect=[{},RuntimeError('transport lost')]
        with self.assertRaises(RuntimeError):adapter.send_once('demo','subject','next','task-1')
        with self.assertRaisesRegex(RuntimeError,'uncertain'):adapter.send_once('demo','subject','next','task-1')

    def test_failed_session_can_be_replaced_without_deleting_mapping(self):
        self.existing()
        original=self.rpc.call.side_effect
        def missing(method,params):
            if method=='thread/read' and params['threadId']=='missing':
                raise RuntimeError('thread not found: missing')
            return original(method,params)
        (self.base/'worker-demo.json').write_text(json.dumps({'thread_id':'missing'}))
        self.rpc.call.side_effect=missing
        result=manage(adapter,'demo','replace','lost-session')
        self.assertEqual(result['state']['previous_sessions'],['missing'])
        self.assertEqual(result['details']['session'],'missing')

    def test_stop_quarantines_unacknowledged_tasks(self):
        self.existing()
        path=adapter.delivery_dir()/'task.json'
        path.write_text(json.dumps({'to':'demo','message_id':9,'status':'sent'}))
        manage(adapter,'demo','stop','hold-task')
        self.assertTrue(json.loads(path.read_text())['held'])

    def test_unresolved_execution_attempt_blocks_second_wake(self):
        self.existing()
        (self.base/'worker-demo.json').write_text(json.dumps({'thread_id':'test-worker','active_task_id':'attempt'}))
        path=adapter.delivery_dir()/'task.json'
        path.write_text(json.dumps({'to':'demo','message_id':1,'task_id':'attempt','status':'sent','attempted':True}))
        with self.assertRaisesRegex(RuntimeError,'unresolved execution attempt'):
            adapter.wake('demo')
        self.assertNotIn('turn/start',dict(self.calls))

    def test_unknown_worker_reply_is_rejected(self):
        with patch.object(adapter,'ROLE','demo'):
            with self.assertRaisesRegex(ValueError,'correlate'):
                adapter.send_once('orchestrator','result','body','unknown-task')

    def test_rotation_is_lazy_and_preserves_old_session(self):
        self.existing()
        result=manage(adapter,'demo','rotate','rotate-1')
        state=json.loads((self.base/'worker-demo.json').read_text())
        self.assertIsNone(state['thread_id'])
        self.assertEqual(state['previous_sessions'],['test-worker'])
        self.assertFalse(state['dispatch_held'])
        self.assertNotIn('thread/start',dict(self.calls))
        self.assertNotIn('turn/start',dict(self.calls))
        self.assertEqual(result['details']['mail_identity'],'unchanged')

    def test_rotation_requires_report_acknowledgement(self):
        self.existing()
        path=adapter.delivery_dir()/'reply.json'
        path.write_text(json.dumps({'from':'demo','to':'orchestrator','status':'sent'}))
        with self.assertRaisesRegex(RuntimeError,'reports remain'):
            manage(adapter,'demo','rotate','rotate-blocked')
        self.assertNotIn('thread/archive',dict(self.calls))

    def test_new_task_cannot_reuse_previous_context(self):
        (self.base/'worker-demo.json').write_text(json.dumps({'thread_id':'test-worker','active_task_id':'previous'}))
        path=adapter.delivery_dir()/'queued.json'
        path.write_text(json.dumps({'to':'demo','message_id':1,'task_id':'next','status':'sent'}))
        with self.assertRaisesRegex(RuntimeError,'rotated'):
            adapter.wake('demo')
        self.assertNotIn('turn/start',dict(self.calls))

    def test_worker_has_no_management_tool(self):
        self.test_tracking_tool_is_only_exposed_to_orchestrator()
        self.assertIn('manage_worker',asyncio.run(adapter.MCP.get_tools()))

if __name__=='__main__':unittest.main()
