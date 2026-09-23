import tempfile
import unittest
from pathlib import Path
import tomllib
from project_mode import dump
from setup_orch import setup


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.root = self.home / 'work/projects'
        (self.root / '.codex').mkdir(parents=True)
        self.path = self.root / '.codex/config.toml'
        self.config = {
            'default_permissions': ':danger-full-access',
            'model': 'keep-local-choice',
            'permissions': {'orch': {'filesystem': {
                ':minimal': 'read', '/Users/source': 'deny',
                '/Users/source/projects/.codex': 'read',
                '/Users/source/projects/AGENTS.md': 'write',
                '/opt/homebrew': 'read',
            }}},
            'mcp_servers': {'project_agents': {
                'command': '/Users/source/.local/bin/python',
                'args': ['/Users/source/.local/share/adapter.py', 'orchestrator'],
            }},
        }
        self.path.write_text(dump(self.config))
        (self.root / 'projects-mode.toml').write_text('mode="isolated"\n')

    def test_preview_apply_backup_and_idempotence(self):
        before = self.path.read_bytes()
        self.assertEqual(setup(self.root, self.home)['default_setup'], 'needs_apply')
        self.assertEqual(self.path.read_bytes(), before)
        result = setup(self.root, self.home, True)
        self.assertEqual(Path(result['backup']).read_bytes(), before)
        data = tomllib.loads(self.path.read_text())
        self.assertEqual(data['model'], 'keep-local-choice')
        fs = data['permissions']['orch']['filesystem']
        self.assertEqual(fs[str(self.home)], 'deny')
        self.assertEqual(fs[str(self.root / '.codex')], 'read')
        self.assertNotIn('/Users/source', fs)
        self.assertEqual(data['mcp_servers']['project_agents']['command'], str(self.home / '.local/bin/python'))
        self.assertFalse(result['adapter_files_present'])
        applied = self.path.read_bytes()
        self.assertEqual(setup(self.root, self.home, True)['default_setup'], 'already_configured')
        self.assertEqual(self.path.read_bytes(), applied)

    def test_existing_snapshot_blocks_changes(self):
        (self.root / '.projects-mode-state.json').write_text('{}')
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'snapshot'):
            setup(self.root, self.home, True)
        self.assertEqual(self.path.read_bytes(), before)

    def test_local_mode_and_legacy_settings_rejected(self):
        self.config['sandbox_mode'] = 'danger-full-access'
        self.path.write_text(dump(self.config))
        with self.assertRaisesRegex(ValueError, 'Legacy'):
            setup(self.root, self.home, True)
        (self.root / 'projects-mode.toml').write_text('mode="local"\n')
        with self.assertRaisesRegex(ValueError, 'Mode is local'):
            setup(self.root, self.home, True)


if __name__ == '__main__':
    unittest.main()
