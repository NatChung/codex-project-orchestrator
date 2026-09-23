"""Offline lifecycle regression checks; never contacts workers or mail."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
import asyncio
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('worker_adapter', ROOT / 'project_agents.py')
adapter = importlib.util.module_from_spec(spec)
read_text = Path.read_text
def read_fixture(path, *args, **kwargs):
    return '{}' if path == ROOT / 'registry.json' else read_text(path, *args, **kwargs)

with patch.object(sys, 'argv', ['project_agents.py', 'orchestrator']), \
        patch.object(Path, 'read_text', read_fixture):
    sys.modules[spec.name] = adapter
    spec.loader.exec_module(adapter)


class WorkerModelTests(unittest.TestCase):
    def test_tracking_tool_is_only_exposed_to_orchestrator(self):
        self.assertIn('track_task', asyncio.run(adapter.MCP.get_tools()))
        worker_spec = importlib.util.spec_from_file_location('tracking_worker_fixture', ROOT / 'project_agents.py')
        worker = importlib.util.module_from_spec(worker_spec)
        def fixture(path, *args, **kwargs):
            return '{"demo": {}}' if path == ROOT / 'registry.json' else read_text(path, *args, **kwargs)
        with patch.object(sys, 'argv', ['project_agents.py', 'demo']), patch.object(Path, 'read_text', fixture):
            worker_spec.loader.exec_module(worker)
        self.assertNotIn('track_task', asyncio.run(worker.MCP.get_tools()))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.policy = self.base / 'worker-model.toml'
        self.policy.write_text((ROOT / 'worker-model.toml').read_text())
        (self.base / 'runtime.toml').write_text('[permissions.worker-demo.filesystem]\n"/demo" = "write"\n')
        self.mode = self.base / 'projects-mode.toml'
        self.mode.write_text('mode = "isolated"\n')
        (self.base / 'identities.json').write_text(json.dumps({'demo': {'name':'Demo','registration_token':'test'}}))
        self.calls = []
        self.active = False
        self.actual_model = 'gpt-5.6-sol'
        self.actual_effort = 'medium'
        self.rpc = Mock()
        self.rpc.call.side_effect = self.call
        for target, value in [
            ('operator_settings', lambda: {'workspace': str(self.base), 'communications_worker': 'communications'}),
            ('BASE', self.base),
            ('REG', {'demo': {'cwd': '/demo', 'profile': 'worker-demo'}}),
            ('RPC', Mock(return_value=self.rpc)),
            ('admitted_ids', Mock(return_value={1})),
            ('Path', lambda p: self.mode if str(p).endswith('/projects-mode.toml') else Path(p)),
        ]:
            p = patch.object(adapter, target, value)
            p.start()
            self.addCleanup(p.stop)

        from unittest.mock import AsyncMock
        p = patch.object(adapter, 'mail', AsyncMock(return_value={}))
        p.start(); self.addCleanup(p.stop)

    def call(self, method, params):
        self.calls.append((method, params))
        if method == 'thread/read':
            return {'thread': {'cwd':'/demo', 'status': {'type': 'active' if self.active else 'idle'}}}
        if method in ('thread/start', 'thread/resume'):
            return {'thread': {'id': 'test-worker'}, 'cwd': '/demo',
                    'activePermissionProfile': {'id': 'worker-demo'},
                    'model': self.actual_model, 'reasoningEffort': self.actual_effort}
        if method == 'turn/start':
            return {'turn': {'id': 'test-turn'}}
        raise AssertionError(method)

    def existing(self):
        (self.base / 'worker-demo.json').write_text(json.dumps({'thread_id': 'test-worker'}))

    def assert_routing(self, method):
        calls = dict(self.calls)
        self.assertEqual(calls[method]['model'], 'gpt-5.6-sol')
        self.assertEqual(calls[method]['config']['model_reasoning_effort'], 'medium')
        self.assertEqual(calls[method]['permissions'], 'worker-demo')
        self.assertEqual(calls[method]['config']['permissions.worker-demo']['filesystem']['/demo'], 'write')
        self.assertEqual(calls['turn/start']['model'], 'gpt-5.6-sol')
        self.assertEqual(calls['turn/start']['effort'], 'medium')
        state = json.loads((self.base / 'worker-demo.json').read_text())
        self.assertEqual((state['model'], state['reasoning_effort']), ('gpt-5.6-sol', 'medium'))

    def test_new_worker(self):
        adapter.wake('demo')
        self.assert_routing('thread/start')
        self.assertIn('mcp_servers.project_agents.args', dict(self.calls)['thread/start']['config'])

    def test_existing_worker(self):
        self.existing()
        adapter.wake('demo')
        self.assert_routing('thread/resume')

    def test_routing_is_injected_for_new_and_resumed_workers(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                self.calls.clear()
                if existing:
                    self.existing()
                adapter.wake('demo')
                text = dict(self.calls)['turn/start']['input'][0]['text']
                self.assertIn('handled by communications through orchestrator', text)
                self.assertIn('Do not execute communication skills', text)

    def test_communication_executor_has_distinct_route(self):
        text = adapter.communication_route('communications')
        self.assertIn('designated executor', text)
        self.assertIn('not Apps connectors', text)
        self.assertIn('unknown send result', text)
        self.assertNotIn('Do not execute communication skills', text)

    def test_runtime_permission_update_is_passed_on_resume(self):
        adapter.wake('demo')
        self.calls.clear()
        (self.base / 'runtime.toml').write_text(
            '[permissions.worker-demo.filesystem]\n"/demo" = "write"\n"/shared/rules" = "read"\n')
        adapter.wake('demo')
        permission = dict(self.calls)['thread/resume']['config']['permissions.worker-demo']
        self.assertEqual(permission['filesystem']['/shared/rules'], 'read')

    def test_active_worker_untouched(self):
        self.existing()
        self.active = True
        before = (self.base / 'worker-demo.json').read_text()
        self.assertEqual(adapter.wake('demo')['status'], 'already_active')
        self.assertEqual([m for m, _ in self.calls], ['thread/read'])
        self.assertEqual((self.base / 'worker-demo.json').read_text(), before)

    def test_mismatches_prevent_turn(self):
        for model, effort in [('gpt-6-astra', 'medium'), ('gpt-5.6-sol', 'high')]:
            with self.subTest(model=model, effort=effort):
                self.calls.clear()
                self.actual_model, self.actual_effort = model, effort
                with self.assertRaisesRegex(RuntimeError, 'model/effort mismatch'):
                    adapter.wake('demo')
                self.assertNotIn('turn/start', dict(self.calls))
                self.rpc.close.assert_called()

    def test_local_mode_blocks_wake(self):
        self.mode.write_text('mode = "local"\n')
        with self.assertRaisesRegex(RuntimeError, 'local mode'):
            adapter.wake('demo')
        self.assertEqual(self.calls, [])

    def test_policy_reload(self):
        adapter.wake('demo')
        self.calls.clear()
        self.policy.write_text('model = "gpt-5.6-sol"\nreasoning_effort = "high"\n')
        self.actual_effort = 'high'
        adapter.wake('demo')
        self.assertEqual(dict(self.calls)['turn/start']['effort'], 'high')

    def test_invalid_policy_prevents_start(self):
        self.policy.write_text('model = ""\nreasoning_effort = "medium"\n')
        with self.assertRaises(ValueError):
            adapter.wake('demo')
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
