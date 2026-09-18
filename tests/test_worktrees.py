from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from codex_project_orchestrator.worktrees import (
    WorktreeError,
    WorktreeRegistry,
    effective_workers,
)


class WorktreeRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self._git("init")
        self._git("config", "user.name", "Synthetic Test")
        self._git("config", "user.email", "synthetic@example.invalid")
        (self.source / "README.md").write_text("synthetic\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-m", "initial")
        self.orch = self.root / "orch"
        self.orch.mkdir()
        self.state = self.root / "state"
        self.settings = {
            "orchestrator": {"cwd": str(self.orch), "profile": "orch"},
            "workers": {
                "alpha": {
                    "cwd": str(self.source),
                    "profile": "worker-alpha",
                    "model": "synthetic-worker",
                }
            },
            "worktree_root": str(self.root / "worktrees"),
            "runtime_read": [str(self.root / "runtime")],
        }
        (self.root / "runtime").mkdir()

    def _git(self, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self.source), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    def test_create_is_idempotent_and_registers_linked_worktree(self) -> None:
        registry = WorktreeRegistry(self.settings, self.state)
        created = registry.create("alpha", "task-1")
        repeated = registry.create("alpha", "task-1")
        self.assertEqual(created, repeated)
        self.assertEqual(created["profile"], "worktree-alpha")
        self.assertEqual(created["model"], "synthetic-worker")
        self.assertTrue(Path(created["cwd"]).is_dir())
        self.assertTrue((Path(created["cwd"]) / ".git").is_file())
        self.assertEqual([created], registry.list("alpha"))
        self.assertIn(created["worker_id"], effective_workers(self.settings, self.state))

    def test_entry_records_only_its_worktree_and_git_metadata(self) -> None:
        created = WorktreeRegistry(self.settings, self.state).create("alpha", "task-2")
        self.assertEqual(created["cwd"], str(Path(created["cwd"]).resolve()))
        self.assertTrue(Path(created["git_common_dir"]).is_dir())
        self.assertTrue(Path(created["git_control_dir"]).is_dir())
        self.assertNotEqual(created["cwd"], str(self.source))

    def test_rejects_unknown_project_invalid_task_and_changed_ref(self) -> None:
        registry = WorktreeRegistry(self.settings, self.state)
        with self.assertRaisesRegex(WorktreeError, "unknown"):
            registry.create("missing", "task")
        with self.assertRaises(ValueError):
            registry.create("alpha", "bad task")
        registry.create("alpha", "task", "HEAD")
        with self.assertRaisesRegex(WorktreeError, "collision"):
            registry.create("alpha", "task", "HEAD~0")


if __name__ == "__main__":
    unittest.main()
