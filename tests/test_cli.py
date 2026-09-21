from __future__ import annotations

import tempfile
import tomllib
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from codex_project_orchestrator import cli
from codex_project_orchestrator.config import initialize, load
from codex_project_orchestrator.services import environment


class CliSettingsTests(unittest.TestCase):
    def test_child_environment_does_not_inherit_credentials(self):
        with patch.dict('os.environ', {'PATH': '/usr/bin', 'HOME': '/synthetic/home', 'AWS_SECRET_ACCESS_KEY': 'test-secret', 'SSH_AUTH_SOCK': '/synthetic/agent', 'DATABASE_URL': 'test-db', 'CUSTOM_TOKEN': 'test-token'}, clear=True):
            env = environment(Path('/synthetic/state'))
        self.assertEqual(set(env), {'PATH', 'HOME', 'CODEX_HOME'})

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.alpha = self.root / "alpha"
        self.beta = self.root / "beta"
        self.alpha.mkdir()
        self.beta.mkdir()
        self.state = self.root / "state"
        self.orchestrator = self.root / "orchestrator"
        initialize(
            self.state,
            self.orchestrator,
            [f"alpha={self.alpha}", f"beta={self.beta}"],
        )

    def args(self, *arguments: str):
        return cli.parser().parse_args(["--state", str(self.state), *arguments])

    def snapshot(self) -> dict[str, tuple[str, bytes | None]]:
        result: dict[str, tuple[str, bytes | None]] = {}
        for path in sorted(self.state.rglob("*")):
            relative = str(path.relative_to(self.state))
            if path.is_symlink():
                result[relative] = ("symlink", str(path.readlink()).encode())
            elif path.is_dir():
                result[relative] = ("directory", None)
            else:
                result[relative] = ("file", path.read_bytes())
        return result

    def test_local_mode_requires_explicit_full_access_flag(self) -> None:
        settings_before = (self.state / "settings.toml").read_bytes()
        config_before = (self.state / "codex-home" / "config.toml").read_bytes()

        with self.assertRaisesRegex(ValueError, "allow-full-access"):
            cli.run(self.args("mode", "local", "--dry-run"))

        self.assertEqual(settings_before, (self.state / "settings.toml").read_bytes())
        self.assertEqual(
            config_before, (self.state / "codex-home" / "config.toml").read_bytes()
        )

        result = cli.run(
            self.args("mode", "local", "--allow-full-access", "--dry-run")
        )
        self.assertEqual("local", result["mode"])
        self.assertFalse(result["written"])
        preview = tomllib.loads(result["config_preview"])
        self.assertEqual(":danger-full-access", preview["default_permissions"])
        self.assertEqual("isolated", load(self.state)["mode"])

    def test_apply_dry_run_does_not_mutate_runtime_state(self) -> None:
        before = self.snapshot()

        result = cli.run(self.args("apply", "--dry-run"))

        self.assertFalse(result["written"])
        self.assertEqual(before, self.snapshot())

    def test_mode_dry_run_does_not_mutate_runtime_state(self) -> None:
        before = self.snapshot()

        result = cli.run(
            self.args("mode", "local", "--allow-full-access", "--dry-run")
        )

        self.assertFalse(result["written"])
        self.assertEqual("local", result["mode"])
        self.assertEqual(before, self.snapshot())

    def test_apply_refuses_to_change_settings_while_owned_service_runs(self) -> None:
        settings_before = (self.state / "settings.toml").read_bytes()
        config_before = (self.state / "codex-home" / "config.toml").read_bytes()

        with patch("codex_project_orchestrator.services.owned", return_value=12345):
            with self.assertRaisesRegex(ValueError, "Stop the worker service"):
                cli.run(self.args("apply"))

        self.assertEqual(settings_before, (self.state / "settings.toml").read_bytes())
        self.assertEqual(
            config_before, (self.state / "codex-home" / "config.toml").read_bytes()
        )

    def test_mode_rolls_back_compiled_config_when_settings_write_fails(self) -> None:
        settings_path = self.state / "settings.toml"
        config_path = self.state / "codex-home" / "config.toml"
        settings_before = settings_path.read_bytes()
        config_before = config_path.read_bytes()
        real_atomic = cli.atomic

        def fail_settings_write(path: Path, text: str) -> None:
            if Path(path) == settings_path:
                raise OSError("injected settings write failure")
            real_atomic(path, text)

        with (
            patch("codex_project_orchestrator.services.owned", return_value=None),
            patch("codex_project_orchestrator.cli.atomic", side_effect=fail_settings_write),
        ):
            with self.assertRaisesRegex(OSError, "injected settings write failure"):
                cli.run(self.args("mode", "local", "--allow-full-access"))

        self.assertEqual(settings_before, settings_path.read_bytes())
        self.assertEqual(config_before, config_path.read_bytes())
        self.assertEqual("isolated", load(self.state)["mode"])

    def test_ask_runs_prompt_on_persistent_orchestrator(self) -> None:
        args = self.args("ask", "delegate the synthetic task")
        completed = {
            "identity": "orchestrator",
            "thread_id": "thread-1",
            "status": "idle",
            "last_response": "done",
        }
        with (
            patch("codex_project_orchestrator.services.status", return_value={"ready": True}),
            patch("codex_project_orchestrator.cli.require_probe"),
            patch("codex_project_orchestrator.orchestrator.OrchestratorRuntime") as runtime,
        ):
            runtime.return_value.wait.return_value = completed
            self.assertEqual(cli.run(args), {"orchestrator": completed})
        runtime.return_value.prompt.assert_called_once_with("delegate the synthetic task")
        runtime.return_value.wait.assert_called_once_with(1200)

    def test_ask_reads_stdin_and_rejects_empty_prompt(self) -> None:
        args = self.args("ask")
        with (
            patch("codex_project_orchestrator.services.status", return_value={"ready": True}),
            patch("codex_project_orchestrator.cli.require_probe"),
            patch("sys.stdin", StringIO("  ")),
        ):
            with self.assertRaisesRegex(ValueError, "Provide an orchestrator prompt"):
                cli.run(args)


if __name__ == "__main__":
    unittest.main()
