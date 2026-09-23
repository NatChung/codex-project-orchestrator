"""Operator configuration must fail closed, never infer a public issue tracker."""
import os
import json
import subprocess
import sys
import tomllib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from operator_settings import operator_settings
from tracking_io import GitHubIssues

class OperatorSettingsTests(unittest.TestCase):
    def test_explicit_private_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'operator.toml'
            path.write_text('workspace="/example/work"\nruntime="/example/state"\n')
            with patch.dict(os.environ, {'PROJECTS_AGENT_SETTINGS': str(path)}):
                self.assertEqual(operator_settings()['workspace'], '/example/work')
    def test_relative_operator_paths_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'operator.toml'
            path.write_text('workspace="work"\nruntime="/example/state"\n')
            with patch.dict(os.environ, {'PROJECTS_AGENT_SETTINGS': str(path)}):
                with self.assertRaises(ValueError):
                    operator_settings()
    def test_missing_tracker_does_not_request_credentials(self):
        with patch('tracking_io.operator_settings', return_value={}), patch('tracking_io.subprocess.run') as run:
            with self.assertRaises(KeyError):
                GitHubIssues()
            run.assert_not_called()

    def test_installer_dry_run_and_private_config_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            workspace = base / 'work'; runtime = base / 'state'; home = base / 'home'
            for target in (workspace / '.codex', runtime, home / '.codex'):
                target.mkdir(parents=True)
            (home / '.codex/config.toml').write_text('')
            config = workspace / '.codex/config.toml'
            config.write_text('[permissions.orch.filesystem]\n[features]\n[mcp_servers.project_agents]\nenabled_tools=[]\n')
            (runtime / 'registry.json').write_text('{}')
            (runtime / 'runtime.toml').write_text('[permissions.orch.filesystem]\n[features]\n')
            (workspace / '.projects-mode-state.json').write_text(json.dumps({'files': {str(config): {'changes': []}}}))
            settings = base / 'private.toml'
            settings.write_text(f'workspace={json.dumps(str(workspace))}\nruntime={json.dumps(str(runtime))}\n')
            env = dict(os.environ, HOME=str(home), PROJECTS_AGENT_SETTINGS=str(settings))
            command = [sys.executable, str(Path(__file__).with_name('install_management.py'))]
            subprocess.run(command, env=env, check=True, capture_output=True)
            self.assertFalse((runtime / 'operator.toml').exists())
            subprocess.run(command + ['--apply'], env=env, check=True, capture_output=True)
            self.assertEqual(tomllib.loads((runtime / 'operator.toml').read_text())['runtime'], str(runtime))
            self.assertTrue((runtime / 'operator_settings.py').exists())
            self.assertTrue((runtime / 'backups').is_dir())
