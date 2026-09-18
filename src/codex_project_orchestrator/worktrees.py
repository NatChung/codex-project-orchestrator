"""Operator-owned Git worktree leases for dynamically isolated workers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from filelock import FileLock

from .config import ID, WORKER_MODEL
from .runtime import RuntimeErrorBase


TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class WorktreeError(RuntimeErrorBase):
    """A worktree lease could not be created or validated."""


class WorktreeRegistry:
    def __init__(self, settings: Mapping[str, Any], state: Path) -> None:
        self.settings = dict(settings)
        self.state = Path(state)
        self.path = self.state / "worktrees.json"
        self.lock = self.state / "worktrees.lock"

    def create(self, project: str, task_id: str, ref: str = "HEAD") -> dict[str, Any]:
        if project not in self.settings.get("workers", {}):
            raise WorktreeError(f"unknown registered base project: {project}")
        if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
            raise ValueError("task_id must use 1-128 letters, numbers, dot, underscore or hyphen")
        if not isinstance(ref, str) or not ref.strip() or ref != ref.strip() or len(ref) > 512:
            raise ValueError("ref must be non-empty text without surrounding whitespace")
        source = Path(self.settings["workers"][project]["cwd"])
        root = worktree_root(self.settings)
        worker_id = _worker_id(project, task_id)
        destination = root / worker_id
        with FileLock(str(self.lock), timeout=10):
            entries = self._read()
            existing = entries.get(worker_id)
            if existing is not None:
                if (
                    existing.get("base_project") == project
                    and existing.get("task_id") == task_id
                    and existing.get("requested_ref") == ref
                ):
                    return existing
                raise WorktreeError(f"worktree worker id collision: {worker_id}")
            self._validate_source(source)
            commit = self._git(
                source,
                "rev-parse",
                "--verify",
                "--end-of-options",
                ref + "^{commit}",
            ).strip()
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if destination.exists():
                raise WorktreeError(f"unregistered worktree destination exists: {destination}")
            self._git(source, "worktree", "add", "--detach", str(destination), commit)
            try:
                control, common = _git_layout(destination)
                entry = {
                    "worker_id": worker_id,
                    "base_project": project,
                    "task_id": task_id,
                    "requested_ref": ref,
                    "start_commit": commit,
                    "cwd": str(destination),
                    "profile": "worktree-" + project,
                    "model": self.settings["workers"][project].get("model", WORKER_MODEL),
                    "dynamic_worktree": True,
                    "git_control_dir": str(control),
                    "git_common_dir": str(common),
                    "created_at": datetime.now(UTC).isoformat(),
                }
                entries[worker_id] = entry
                self._write(entries)
                return entry
            except Exception:
                subprocess.run(
                    ["git", "-C", str(source), "worktree", "remove", "--force", str(destination)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                raise

    def list(self, project: str | None = None) -> list[dict[str, Any]]:
        entries = list(self._read().values())
        if project is not None:
            if project not in self.settings.get("workers", {}):
                raise WorktreeError(f"unknown registered base project: {project}")
            entries = [item for item in entries if item.get("base_project") == project]
        return sorted(entries, key=lambda item: (item.get("created_at", ""), item["worker_id"]))

    def workers(self) -> dict[str, dict[str, Any]]:
        return {key: dict(value) for key, value in self._read().items()}

    def _read(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorktreeError(f"invalid worktree registry: {exc}") from exc
        if not isinstance(value, dict):
            raise WorktreeError("invalid worktree registry: expected object")
        result: dict[str, dict[str, Any]] = {}
        for worker_id, entry in value.items():
            if not isinstance(worker_id, str) or not ID.fullmatch(worker_id) or not isinstance(entry, dict):
                raise WorktreeError("invalid worktree registry entry")
            self._validate_entry(worker_id, entry)
            result[worker_id] = dict(entry)
        return result

    def _write(self, entries: Mapping[str, Any]) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=self.state, prefix=".worktrees.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(dict(entries), handle, sort_keys=True, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _validate_source(self, source: Path) -> None:
        top = Path(self._git(source, "rev-parse", "--show-toplevel").strip()).resolve()
        if top != source:
            raise WorktreeError(f"registered project must be its Git top level: {source}")

    def _validate_entry(self, worker_id: str, entry: Mapping[str, Any]) -> None:
        if entry.get("worker_id") != worker_id or entry.get("base_project") not in self.settings.get("workers", {}):
            raise WorktreeError(f"worktree registry identity mismatch: {worker_id}")
        cwd = Path(str(entry.get("cwd", "")))
        root = worktree_root(self.settings)
        if not cwd.is_absolute() or cwd.parent != root or cwd.name != worker_id or not cwd.is_dir():
            raise WorktreeError(f"invalid worktree path for {worker_id}")
        control, common = _git_layout(cwd)
        if str(control) != entry.get("git_control_dir") or str(common) != entry.get("git_common_dir"):
            raise WorktreeError(f"Git metadata changed for worktree {worker_id}")

    @staticmethod
    def _git(source: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(source), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode:
            detail = completed.stderr.strip() or completed.stdout.strip() or "git command failed"
            raise WorktreeError(detail[:1000])
        return completed.stdout


def worktree_root(settings: Mapping[str, Any]) -> Path:
    configured = settings.get("worktree_root")
    if configured is None:
        configured = str(Path(settings["orchestrator"]["cwd"]).parent / "worktrees")
    return Path(configured)


def effective_workers(settings: Mapping[str, Any], state: Path) -> dict[str, dict[str, Any]]:
    workers = {key: dict(value) for key, value in settings.get("workers", {}).items()}
    dynamic = WorktreeRegistry(settings, state).workers()
    collision = workers.keys() & dynamic.keys()
    if collision:
        raise WorktreeError("dynamic worker collides with registered worker: " + sorted(collision)[0])
    workers.update(dynamic)
    return workers


def _worker_id(project: str, task_id: str) -> str:
    digest = hashlib.sha256((project + "\0" + task_id).encode()).hexdigest()[:12]
    prefix = project[: 48 - len("-wt-") - len(digest)].rstrip("-")
    return f"{prefix}-wt-{digest}"


def _git_layout(cwd: Path) -> tuple[Path, Path]:
    gitfile = cwd / ".git"
    if gitfile.is_symlink() or not gitfile.is_file():
        raise WorktreeError(f"expected linked-worktree .git file: {gitfile}")
    line = gitfile.read_text(encoding="utf-8").strip()
    if not line.startswith("gitdir: "):
        raise WorktreeError(f"invalid linked-worktree .git file: {gitfile}")
    control = Path(line.removeprefix("gitdir: "))
    if not control.is_absolute():
        control = gitfile.parent / control
    control = control.resolve()
    common_file = control / "commondir"
    if not control.is_dir() or not common_file.is_file() or common_file.is_symlink():
        raise WorktreeError("linked worktree Git control metadata is missing")
    common = Path(common_file.read_text(encoding="utf-8").strip())
    if not common.is_absolute():
        common = control / common
    common = common.resolve()
    if not common.is_dir() or control.parent != common / "worktrees":
        raise WorktreeError("unexpected linked worktree Git directory relationship")
    return control, common
