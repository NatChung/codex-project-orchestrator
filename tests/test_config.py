import hashlib
import json
import tempfile
import tomllib
import unittest
from pathlib import Path

from codex_project_orchestrator.config import compiled, initialize, load, require_probe, role_model, validate


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.alpha = self.root / 'alpha'
        self.beta = self.root / 'beta'
        self.alpha.mkdir()
        self.beta.mkdir()
        self.state = self.root / 'state'
        self.orch = self.root / 'orch'

    def initialize(self, **kwargs):
        return initialize(self.state, self.orch, ['alpha=' + str(self.alpha), 'beta=' + str(self.beta)], **kwargs)

    def test_initialization_preserves_projects(self):
        original = self.alpha / 'AGENTS.md'
        original.write_text('existing project rules')
        settings = self.initialize()
        self.assertEqual(settings, load(self.state))
        self.assertEqual(original.read_text(), 'existing project rules')
        self.assertEqual(list(self.beta.iterdir()), [])
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        self.assertEqual(settings['worktree_root'], str(self.root / 'worktrees'))

    def test_profiles_and_external_state(self):
        config = tomllib.loads(compiled(self.initialize(), self.state))
        self.assertEqual(config['default_permissions'], 'orch')
        self.assertFalse(config['permissions']['worker-alpha']['network']['enabled'])
        fs = config['permissions']['worker-alpha']['filesystem']
        self.assertEqual(fs[str(self.alpha)], 'write')
        self.assertEqual(fs[str(self.beta)], 'deny')
        self.assertEqual(fs[str(self.state)], 'deny')
        self.assertEqual(fs[str(self.alpha / '.codex')], 'read')
        self.assertEqual(config['projects'][str(self.alpha)]['trust_level'], 'untrusted')
        worktree = config['permissions']['worktree-alpha']
        self.assertFalse(worktree['network']['enabled'])
        self.assertEqual(worktree['filesystem'][str(self.alpha)], 'deny')
        self.assertEqual(worktree['filesystem'][str(self.alpha / '.git')], 'write')
        self.assertEqual(worktree['filesystem'][':workspace_roots']['.'], 'write')
        self.assertEqual(worktree['filesystem'][':workspace_roots']['.git'], 'read')
        tools = config['mcp_servers']['project_agents']['tools']
        self.assertIn('create_worktree_worker', tools)
        self.assertIn('list_worktree_workers', tools)

    def test_worktree_root_cannot_overlap_projects_or_state(self):
        settings = self.initialize()
        for invalid in (self.alpha, self.state, Path.home()):
            with self.subTest(invalid=invalid):
                changed = dict(settings)
                changed['worktree_root'] = str(invalid)
                with self.assertRaises(ValueError):
                    validate(changed, self.state)

    def test_reinitialize_refused(self):
        self.initialize()
        with self.assertRaises(ValueError):
            self.initialize()

    def test_role_models_defaults_overrides_and_legacy_settings(self):
        settings = self.initialize()
        self.assertEqual(settings['orchestrator']['model'], 'gpt-6-astra')
        self.assertEqual(settings['workers']['alpha']['model'], 'gpt-5.6-sol')
        self.assertEqual(tomllib.loads(compiled(settings, self.state))['model'], 'gpt-6-astra')
        settings['orchestrator']['model'] = 'synthetic-orch'
        settings['workers']['alpha']['model'] = 'synthetic-worker'
        self.assertEqual(tomllib.loads(compiled(settings, self.state))['model'], 'synthetic-orch')
        self.assertEqual(role_model(settings, 'alpha'), 'synthetic-worker')
        del settings['workers']['alpha']['model']
        self.assertEqual(role_model(settings, 'alpha'), 'gpt-5.6-sol')
        del settings['orchestrator']['model']
        self.assertEqual(role_model(settings, 'orchestrator'), 'gpt-6-astra')
        for invalid in ('', '  ', ' padded ', None, 123):
            settings['workers']['alpha']['model'] = invalid
            with self.assertRaises(ValueError):
                validate(settings, self.state)

    def test_overlapping_projects_refused(self):
        child = self.alpha / 'child'
        child.mkdir()
        with self.assertRaises(ValueError):
            initialize(self.state, self.orch, ['alpha=' + str(self.alpha), 'child=' + str(child)])
        self.assertFalse(self.state.exists())

    def test_state_inside_worker_refused(self):
        with self.assertRaises(ValueError):
            initialize(self.alpha / 'state', self.orch, ['alpha=' + str(self.alpha)])

    def test_invalid_ids_and_duplicates(self):
        for values in [['../bad=' + str(self.alpha)], ['orchestrator=' + str(self.alpha)], ['x=' + str(self.alpha), 'x=' + str(self.beta)]]:
            with self.assertRaises(ValueError):
                initialize(self.state, self.orch, values)

    def test_runtime_read_cannot_grant_other_projects(self):
        with self.assertRaises(ValueError):
            self.initialize(runtime_read=[str(self.beta)])

    def test_symlink_alias_overlap(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.alpha, target_is_directory=True)
        with self.assertRaises(ValueError):
            initialize(self.state, self.orch, ['alpha=' + str(self.alpha), 'alias=' + str(alias)])

    def test_probe_required_and_bound_to_service_and_config(self):
        settings = self.initialize()
        with self.assertRaises(RuntimeError):
            require_probe(settings, self.state)
        (self.state / 'service.json').write_text(json.dumps({'pid': 123}))
        receipt = {'ok': True, 'service_pid': 123, 'config_sha256': hashlib.sha256(compiled(settings, self.state).encode()).hexdigest()}
        (self.state / 'probe.json').write_text(json.dumps(receipt))
        require_probe(settings, self.state)
        (self.state / 'service.json').write_text(json.dumps({'pid': 124}))
        with self.assertRaises(RuntimeError):
            require_probe(settings, self.state)
        (self.state / 'service.json').write_text(json.dumps({'pid': 123}))
        (self.state / 'codex-home/config.toml').write_text('default_permissions = ":danger-full-access"')
        with self.assertRaises(RuntimeError):
            require_probe(settings, self.state)


if __name__ == '__main__':
    unittest.main()
