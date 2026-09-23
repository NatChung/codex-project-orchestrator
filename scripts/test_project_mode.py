"""Regression tests use temporary fixtures, never the user's configurations."""
import importlib.util
import json
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('project_mode', Path(__file__).with_name('project_mode.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class ModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'projects'
        self.base = Path(self.tmp.name) / 'service'
        self.base.mkdir()
        self.patches = patch.multiple(m, ROOT=self.root, BASE=self.base,
                                     MODE=self.root/'projects-mode.toml', STATE=self.root/'.projects-mode-state.json')
        self.patches.start()
        self.addCleanup(self.patches.stop)
        self.root.mkdir()
        m.MODE.write_text('mode = "isolated"\n')
        reg = {f'p{i}': {'cwd': str(self.root/f'p{i}'), 'profile': f'worker-p{i}'} for i in range(12)}
        (self.base/'registry.json').write_text(json.dumps(reg))
        self.originals = {}
        for p in m.targets():
            p.parent.mkdir(parents=True)
            profile = 'orch' if p.parent.parent == self.root else 'worker-'+p.parent.parent.name
            raw = '# preserve comments on roundtrip\n'+m.dump({
                'default_permissions': profile, 'approval_policy': 'never',
                'features': {'plugins': False, 'network_proxy': True},
                'permissions': {profile: {'filesystem': {str(self.root): 'deny'}, 'network': {'enabled': False}}},
                'mcp_servers': {'codegraph': {'enabled': False}, 'project_agents': {'enabled': True},
                                'connector': {'url': 'https://example.test/mcp'}},
                'model': 'unchanged'})
            p.write_text(raw)
            self.originals[p] = raw
        m.MODE.write_text('mode = "isolated"\n')

    def test_roundtrip_and_idempotence_all_thirteen(self):
        m.switch('local')
        for p in m.targets():
            d = tomllib.loads(p.read_text())
            self.assertEqual(d['default_permissions'], ':danger-full-access')
            self.assertNotIn('codegraph', d['mcp_servers'])
            self.assertTrue(d['features']['plugins'])
            self.assertEqual(d['mcp_servers']['connector']['url'], 'https://example.test/mcp')
        self.assertEqual(m.switch('local')['changed'], [])
        self.assertTrue(m.status()['consistent'])
        m.switch('isolated')
        for p, raw in self.originals.items():
            self.assertEqual(p.read_text(), raw)

    def test_unrelated_edits_survive(self):
        m.switch('local')
        p = m.targets()[1]
        d = tomllib.loads(p.read_text())
        d['model'] = 'user-new-model'
        d['mcp_servers']['connector']['url'] = 'https://new.example.test/mcp'
        p.write_text(m.dump(d))
        m.switch('isolated')
        m.switch('local')
        actual = tomllib.loads(p.read_text())
        self.assertEqual(actual['model'], 'user-new-model')
        self.assertEqual(actual['mcp_servers']['connector'], d['mcp_servers']['connector'])

    def test_drift_aborts_before_any_write(self):
        m.switch('local')
        p = m.targets()[-1]
        p.write_text(p.read_text().replace(':danger-full-access', ':read-only'))
        before = {p: p.read_text() for p in m.targets()}
        with self.assertRaisesRegex(ValueError, 'drift'):
            m.switch('isolated')
        self.assertEqual(before, {p: p.read_text() for p in m.targets()})
        self.assertEqual(m.read_mode(), 'local')

    def test_exception_rolls_back(self):
        m.switch('local')
        before = {p: p.read_text() for p in [*m.targets(), m.MODE, m.STATE]}
        real = m.atomic
        calls = 0
        def fail_once(p, text):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError('synthetic disk failure')
            return real(p, text)
        with patch.object(m, 'atomic', fail_once):
            with self.assertRaises(OSError):
                m.switch('isolated')
        self.assertEqual(before, {p: p.read_text() for p in before})

    def test_registry_change_rejected_and_dry_run_read_only(self):
        self.assertEqual(m.switch('local', True)['configs'], 13)
        self.assertFalse(m.STATE.exists())
        self.assertEqual(m.read_mode(), 'isolated')
        m.switch('local')
        (self.base/'registry.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'Registry changed'):
            m.switch('isolated')

    def test_nested_legacy_sandbox_without_mailbox(self):
        p = self.root/'p0/nested/.codex/config.toml'
        p.parent.mkdir(parents=True)
        raw = 'sandbox_mode = "workspace-write"\n'
        p.write_text(raw)
        m.MODE.write_text(m.MODE.read_text()+'extra_configs = ["p0/nested/.codex/config.toml"]\n')
        m.switch('local')
        d = tomllib.loads(p.read_text())
        self.assertNotIn('sandbox_mode', d)
        self.assertNotIn('mcp_servers', d)
        self.assertEqual(d['default_permissions'], ':danger-full-access')
        self.assertEqual(len(m.targets()), 14)
        m.switch('isolated')
        self.assertEqual(p.read_text(), raw)

    def test_consistent_full_access_snapshot_is_not_isolated(self):
        m.switch('isolated')
        p = self.root/'.codex/config.toml'
        p.write_text(p.read_text().replace('"default_permissions" = "orch"', '"default_permissions" = ":danger-full-access"'))
        state = json.loads(m.STATE.read_text())
        for change in state['files'][str(p)]['changes']:
            if change['keys'] == ['default_permissions']:
                change['isolated'] = ':danger-full-access'
        m.STATE.write_text(json.dumps(state))
        before = p.read_text()
        with self.assertRaisesRegex(ValueError, 'Invalid isolated Orch'):
            m.status()
        with self.assertRaisesRegex(ValueError, 'Invalid isolated Orch'):
            m.switch('isolated')
        self.assertEqual(p.read_text(), before)

    def test_missing_profile_and_legacy_override_rejected(self):
        m.switch('isolated')
        p = self.root/'.codex/config.toml'
        original = p.read_text()
        for invalid in ('missing_profile', 'legacy_override'):
            with self.subTest(invalid=invalid):
                data = tomllib.loads(original)
                if invalid == 'missing_profile':
                    data.pop('permissions')
                else:
                    data['sandbox_mode'] = 'danger-full-access'
                p.write_text(m.dump(data))
                with self.assertRaisesRegex(ValueError, 'Invalid isolated Orch'):
                    m.status()


if __name__ == '__main__':
    unittest.main()
