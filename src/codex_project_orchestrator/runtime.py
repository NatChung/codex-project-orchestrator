"""Codex app-server RPC and isolated worker lifecycle management.

The runtime deliberately keeps durable state small.  Mailbox contents and Codex
event streams don't belong in worker metadata; the only durable runtime fact we
need is which configured worker owns which Codex thread.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Never, Self

from filelock import FileLock

from .config import role_model


class RuntimeErrorBase(RuntimeError):
    """Base class for errors callers may safely present to an operator."""


class RPCError(RuntimeErrorBase):
    """The app-server returned a JSON-RPC error."""


class RPCUncertainError(RuntimeErrorBase):
    """The connection failed after an operation may have reached the server."""


class ReconciliationRequired(RuntimeErrorBase):
    """Automatic retry could repeat worker side effects."""


class RPC(AbstractContextManager["RPC"]):
    """Small synchronous JSON-RPC client for a Unix websocket app-server."""

    def __init__(self, socket: Path, *, timeout: float = 15.0) -> None:
        self.socket = Path(socket)
        self.timeout = timeout
        self._websocket: Any = None
        self._next_id = 1

    def __enter__(self) -> Self:
        try:
            from websockets.sync.client import unix_connect

            self._websocket = unix_connect(
                str(self.socket),
                uri="ws://localhost",
                open_timeout=self.timeout,
                compression=None,
                max_size=30_000_000,
            )
            self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "codex-project-orchestrator",
                        "version": "0.1.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
            self.notify("initialized")
            return self
        except Exception as exc:
            self._close()
            if isinstance(exc, RuntimeErrorBase):
                raise
            raise RPCUncertainError(
                f"could not initialize app-server websocket {self.socket}: {exc}"
            ) from exc

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._close()

    def _close(self) -> None:
        websocket, self._websocket = self._websocket, None
        if websocket is not None:
            websocket.close()

    def _send(self, message: Mapping[str, Any]) -> None:
        if self._websocket is None:
            raise RPCUncertainError("app-server websocket is not connected")
        try:
            self._websocket.send(json.dumps(message, separators=(",", ":")))
        except Exception as exc:
            raise RPCUncertainError(f"app-server send failed: {exc}") from exc

    def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = dict(params)
        self._send(message)

    def call(
        self, method: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Compatibility name used by service and doctor code."""
        return self.request(method, params)

    def request(
        self, method: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": dict(params or {}),
            }
        )
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RPCUncertainError(
                    f"timed out waiting for app-server response to {method}"
                )
            try:
                raw = self._websocket.recv(timeout=remaining)
                message = json.loads(raw)
            except Exception as exc:
                raise RPCUncertainError(
                    f"app-server disconnected while waiting for {method}: {exc}"
                ) from exc
            if not isinstance(message, dict):
                continue
            # App-server requests must always receive an answer.  This headless
            # client has no authority to approve or collect user input.
            if "method" in message and "id" in message:
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {
                            "code": -32000,
                            "message": "headless orchestrator denies server requests",
                        },
                    }
                )
                continue
            # Notifications are intentionally discarded rather than persisted
            # as a raw event log.
            if message.get("id") != request_id:
                continue
            if "error" in message:
                error = message["error"]
                raise RPCError(f"{method} failed: {error}")
            result = message.get("result", {})
            if not isinstance(result, dict):
                raise RPCError(f"{method} returned a non-object result")
            return result


_DEVELOPER_INSTRUCTIONS = """You are the registered isolated project worker.
Read the local AGENTS.md before doing project work. Use project_agents.fetch_inbox
to fetch authorized tasks for this worker identity. Process only those authorized
tasks. Reply on project_agents using the same task_id with concise results and
verifiable evidence, then acknowledge that task. If there are no tasks, take no
actions. Run shell commands with login=false. Do not re-execute side effects when
the prior execution state is uncertain; report that manual reconciliation is
required instead. Delivery is not an exactly-once guarantee."""

_WAKE_PROMPT = (
    "Check project_agents.fetch_inbox now and process the authorized inbox "
    "according to the worker instructions."
)


class Runtime:
    """Own persistent Codex threads for registered isolated project workers."""

    def __init__(
        self,
        settings: Mapping[str, Any],
        state: Path,
        *,
        rpc_factory: Callable[[Path], AbstractContextManager[Any]] = RPC,
    ) -> None:
        self.settings = dict(settings)
        self.state = Path(state)
        self.socket = self.state / "app.sock"
        self.rpc_factory = rpc_factory

    def wake(self, project: str) -> dict[str, Any]:
        if self.settings.get("mode") != "isolated":
            raise RuntimeErrorBase("workers can only be woken in isolated mode")
        from .worktrees import effective_workers
        workers = effective_workers(self.settings, self.state)
        if project not in workers:
            raise RuntimeErrorBase(f"unknown registered worker: {project}")
        worker = workers[project]
        cwd = Path(worker["cwd"])
        profile = worker["profile"]
        if not cwd.is_absolute():
            raise RuntimeErrorBase(f"worker {project} cwd must be absolute")
        if not isinstance(profile, str) or not profile:
            raise RuntimeErrorBase(f"worker {project} profile is invalid")

        self.state.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.state / f"worker-{project}.lock"), timeout=10):
            metadata = self._read_metadata(project)
            if metadata:
                self._validate_metadata(project, metadata, cwd, profile)
            if metadata.get("status") == "reconciliation_required":
                raise ReconciliationRequired(
                    f"worker {project} has an ambiguous prior operation; "
                    "manual reconciliation is required"
                )
            with self.rpc_factory(self.socket) as rpc:
                return self._wake_locked(rpc, project, cwd, profile, metadata)

    def _wake_locked(
        self,
        rpc: Any,
        project: str,
        cwd: Path,
        profile: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        thread_id = metadata.get("thread_id")
        common = self._thread_params(project, cwd, profile)
        if thread_id:
            read = rpc.request(
                "thread/read", {"threadId": thread_id, "includeTurns": True}
            )
            read_thread = _thread(read)
            self._validate_thread_cwd(project, read_thread, cwd)
            if _status(read_thread) == "active":
                active = self._metadata(project, thread_id, cwd, profile, "active")
                self._write_metadata(project, active)
                return active
            self._refuse_ambiguous_turn(project, read_thread)
            resumed = rpc.request("thread/resume", {"threadId": thread_id, **common})
            self._validate_effective(project, resumed, cwd, profile)
            thread = _thread(resumed)
            self._refuse_ambiguous_turn(project, thread)
        else:
            try:
                started = rpc.request("thread/start", common)
            except RPCUncertainError as exc:
                self._mark_ambiguous(project, cwd, profile, metadata, exc)
            self._validate_effective(project, started, cwd, profile)
            thread = _thread(started)
            thread_id = thread.get("id")
            if not isinstance(thread_id, str) or not thread_id:
                raise RPCError("thread/start response omitted thread.id")

        # The thread identity is durable before the turn can cause project
        # side effects.  os.replace in _write_metadata makes this atomic.
        ready = self._metadata(project, thread_id, cwd, profile, "idle")
        self._write_metadata(project, ready)
        from .doctor import probe_worktree
        from .worktrees import effective_workers

        worker = effective_workers(self.settings, self.state)[project]
        if worker.get("dynamic_worktree"):
            probe_worktree(self.settings, self.state, worker, rpc)
        turn_params = {
            "threadId": thread_id,
            "model": common["model"],
            "input": [{"type": "text", "text": _WAKE_PROMPT}],
            "cwd": str(cwd),
            "permissions": profile,
            "approvalPolicy": "never",
        }
        try:
            rpc.request("turn/start", turn_params)
        except RPCUncertainError as exc:
            self._mark_ambiguous(project, cwd, profile, ready, exc)
        active = self._metadata(project, thread_id, cwd, profile, "active")
        self._write_metadata(project, active)
        return active

    def _mark_ambiguous(
        self,
        project: str,
        cwd: Path,
        profile: str,
        metadata: Mapping[str, Any],
        error: RPCUncertainError,
    ) -> Never:
        uncertain = dict(metadata)
        uncertain.update(
            {
                "project": project,
                "cwd": str(cwd),
                "profile": profile,
                "status": "reconciliation_required",
                "updated_at": _now(),
                "error": str(error),
            }
        )
        self._write_metadata(project, uncertain)
        raise ReconciliationRequired(
            f"worker {project} operation became ambiguous; manual reconciliation "
            "is required before another wake"
        ) from error

    def worker_status(self, project: str | None = None) -> Any:
        """Return lifecycle metadata, refreshing thread state without turn content."""
        if project is not None:
            from .worktrees import effective_workers
            workers = effective_workers(self.settings, self.state)
            if project not in workers:
                raise RuntimeErrorBase(f"unknown registered worker: {project}")
            worker = workers[project]
            cwd = Path(worker["cwd"])
            profile = worker["profile"]
            with FileLock(str(self.state / f"worker-{project}.lock"), timeout=10):
                metadata = self._read_metadata(project)
                if not metadata:
                    return {"project": project, "status": "not_started"}
                self._validate_metadata(project, metadata, cwd, profile)
                if metadata.get("status") == "reconciliation_required":
                    return metadata
                with self.rpc_factory(self.socket) as rpc:
                    result = rpc.request(
                        "thread/read",
                        {"threadId": metadata["thread_id"], "includeTurns": False},
                    )
                thread = _thread(result)
                self._validate_thread_cwd(project, thread, cwd)
                refreshed = self._metadata(
                    project,
                    metadata["thread_id"],
                    cwd,
                    profile,
                    _status(thread) or "unknown",
                )
                self._write_metadata(project, refreshed)
                return refreshed
        from .worktrees import effective_workers

        return {
            name: self.worker_status(name)
            for name in effective_workers(self.settings, self.state)
        }

    def _thread_params(
        self, project: str, cwd: Path, profile: str
    ) -> dict[str, Any]:
        python = self.settings.get("python")
        if not isinstance(python, str) or not Path(python).is_absolute():
            raise RuntimeErrorBase("settings.python must be an absolute path")
        from .worktrees import effective_workers
        worker = effective_workers(self.settings, self.state)[project]
        config = {
            "mcp_servers.project_agents.args": [
                "-m",
                "codex_project_orchestrator",
                "--state",
                str(self.state),
                "mcp",
                "--role",
                project,
            ]
        }
        model = worker.get("model")
        if not isinstance(model, str) or not model:
            model = role_model(self.settings, project)
        result = {
            "cwd": str(cwd),
            "model": model,
            "permissions": profile,
            "approvalPolicy": "never",
            "developerInstructions": _DEVELOPER_INSTRUCTIONS,
            "config": config,
        }
        return result

    def _validate_effective(
        self,
        project: str,
        response: Mapping[str, Any],
        cwd: Path,
        profile: str,
    ) -> None:
        self._validate_thread_cwd(project, response, cwd)
        active_profile = response.get("activePermissionProfile")
        active_id = (
            active_profile.get("id") if isinstance(active_profile, dict) else None
        )
        if active_id != profile:
            raise RuntimeErrorBase(
                f"worker {project} active permission profile mismatch: "
                f"expected {profile!r}, got {active_id!r}"
            )
        if response.get("approvalPolicy") != "never":
            raise RuntimeErrorBase(
                f"worker {project} approval policy mismatch: expected 'never', "
                f"got {response.get('approvalPolicy')!r}"
            )

    def _validate_thread_cwd(
        self, project: str, value: Mapping[str, Any], cwd: Path
    ) -> None:
        actual = value.get("cwd")
        if actual is None and isinstance(value.get("thread"), dict):
            actual = value["thread"].get("cwd")
        if actual != str(cwd):
            raise RuntimeErrorBase(
                f"worker {project} cwd mismatch: expected {str(cwd)!r}, got {actual!r}"
            )

    def _refuse_ambiguous_turn(self, project: str, thread: Mapping[str, Any]) -> None:
        turns = thread.get("turns", [])
        if turns and isinstance(turns[-1], dict):
            status = turns[-1].get("status")
            if status in {"failed", "interrupted"}:
                raise ReconciliationRequired(
                    f"worker {project} last turn was {status}; reconcile any "
                    "unacknowledged task before waking it again"
                )

    def _validate_metadata(
        self,
        project: str,
        metadata: Mapping[str, Any],
        cwd: Path,
        profile: str,
    ) -> None:
        if metadata.get("project") != project:
            raise RuntimeErrorBase(f"worker {project} metadata identity mismatch")
        if metadata.get("cwd") != str(cwd):
            raise RuntimeErrorBase(f"worker {project} metadata cwd mismatch")
        if metadata.get("profile") != profile:
            raise RuntimeErrorBase(f"worker {project} metadata profile mismatch")

    def _metadata(
        self,
        project: str,
        thread_id: str,
        cwd: Path,
        profile: str,
        status: str,
    ) -> dict[str, Any]:
        return {
            "project": project,
            "thread_id": thread_id,
            "cwd": str(cwd),
            "profile": profile,
            "status": status,
            "updated_at": _now(),
        }

    def _metadata_path(self, project: str) -> Path:
        return self.state / f"worker-{project}.json"

    def _read_metadata(self, project: str) -> dict[str, Any]:
        path = self._metadata_path(project)
        if not path.exists():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeErrorBase(f"invalid worker metadata {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise RuntimeErrorBase(f"invalid worker metadata {path}: expected object")
        return value

    def _write_metadata(self, project: str, value: Mapping[str, Any]) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        destination = self._metadata_path(project)
        fd, temporary = tempfile.mkstemp(
            dir=self.state, prefix=f".{destination.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(dict(value), handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _thread(response: Mapping[str, Any]) -> dict[str, Any]:
    thread = response.get("thread")
    if not isinstance(thread, dict):
        raise RPCError("app-server response omitted thread")
    return thread


def _status(thread: Mapping[str, Any]) -> str | None:
    status = thread.get("status")
    if isinstance(status, dict):
        return status.get("type")
    return status if isinstance(status, str) else None


def _now() -> str:
    return datetime.now(UTC).isoformat()
