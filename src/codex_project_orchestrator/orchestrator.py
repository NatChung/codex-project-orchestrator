"""Persistent app-server-backed orchestrator controlled by a fixed operator API."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Never

from filelock import FileLock

from .config import compiled, role_model
from .runtime import (
    RPC,
    RPCError,
    RPCUncertainError,
    ReconciliationRequired,
    RuntimeErrorBase,
    _status,
    _thread,
)


_DEVELOPER_INSTRUCTIONS = """You are the persistent isolated project orchestrator.
Read the workspace AGENTS.md and coordinate project work only through the fixed-role
project_agents MCP. Treat each operator prompt as authorization only for its stated
scope. Track queued, running, reported, verified and accepted states separately.
Never bypass a missing tool or permission boundary. Reconcile uncertain prior side
effects before retrying them. Run shell commands with login=false."""


class OrchestratorBusy(RuntimeErrorBase):
    """The persistent orchestrator already has an active turn."""


class OrchestratorRuntime:
    """Own one durable orchestrator thread on the existing app-server."""

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
        self.metadata_path = self.state / "orchestrator.json"
        self.lock_path = self.state / "orchestrator.lock"

    def prompt(self, prompt: str) -> dict[str, Any]:
        prompt = self._text("prompt", prompt)
        self._require_isolated()
        cwd, profile = self._identity()
        with FileLock(str(self.lock_path), timeout=10):
            metadata = self._read_metadata()
            self._validate_metadata(metadata, cwd, profile)
            if metadata.get("status") == "reconciliation_required":
                raise ReconciliationRequired(
                    "orchestrator has an ambiguous prior turn; inspect status and "
                    "acknowledge reconciliation before sending another prompt"
                )
            with self.rpc_factory(self.socket) as rpc:
                thread_id, previous_thread_id = self._ensure_thread(
                    rpc, metadata, cwd, profile
                )
                ready = self._metadata(thread_id, cwd, profile, "idle")
                if previous_thread_id is not None:
                    ready["previous_thread_id"] = previous_thread_id
                self._write_metadata(ready)
                try:
                    result = rpc.request(
                        "turn/start",
                        {
                            "threadId": thread_id,
                            "model": role_model(self.settings, "orchestrator"),
                            "input": [{"type": "text", "text": prompt}],
                            "cwd": str(cwd),
                            "permissions": profile,
                            "approvalPolicy": "never",
                        },
                    )
                except RPCUncertainError as exc:
                    self._mark_ambiguous(ready, exc)
                turn = result.get("turn")
                turn_id = turn.get("id") if isinstance(turn, dict) else None
                if not isinstance(turn_id, str) or not turn_id:
                    raise RPCError("turn/start response omitted turn.id")
                active = self._metadata(thread_id, cwd, profile, "active")
                active["active_turn_id"] = turn_id
                if previous_thread_id is not None:
                    active["previous_thread_id"] = previous_thread_id
                self._write_metadata(active)
                return active

    def status(self) -> dict[str, Any]:
        self._require_isolated()
        cwd, profile = self._identity()
        with FileLock(str(self.lock_path), timeout=10):
            metadata = self._read_metadata()
            if not metadata:
                return {"identity": "orchestrator", "status": "not_started"}
            self._validate_metadata(metadata, cwd, profile)
            if metadata.get("status") == "reconciliation_required":
                return metadata
            with self.rpc_factory(self.socket) as rpc:
                return self._refresh(rpc, metadata, cwd, profile)

    def wait(self, timeout_seconds: float = 120.0) -> dict[str, Any]:
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ValueError("timeout_seconds must be a number")
        if timeout_seconds < 0 or timeout_seconds > 3600:
            raise ValueError("timeout_seconds must be between 0 and 3600")
        deadline = time.monotonic() + timeout_seconds
        while True:
            result = self.status()
            if result.get("status") != "active":
                return result
            if time.monotonic() >= deadline:
                return result
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))

    def result(self) -> dict[str, Any]:
        status = self.status()
        return {
            key: status.get(key)
            for key in (
                "identity",
                "thread_id",
                "status",
                "last_turn_id",
                "last_turn_status",
                "last_response",
                "updated_at",
                "error",
            )
            if key in status
        }

    def steer(self, prompt: str) -> dict[str, Any]:
        prompt = self._text("prompt", prompt)
        cwd, profile = self._identity()
        with FileLock(str(self.lock_path), timeout=10):
            metadata = self._read_metadata()
            self._validate_metadata(metadata, cwd, profile)
            self._refuse_unreconciled(metadata)
            with self.rpc_factory(self.socket) as rpc:
                metadata = self._refresh(rpc, metadata, cwd, profile)
                self._refuse_unreconciled(metadata)
                if metadata.get("status") != "active":
                    raise OrchestratorBusy("orchestrator has no active turn to steer")
                turn_id = self._active_turn_id(metadata)
                result = rpc.request(
                    "turn/steer",
                    {
                        "threadId": metadata["thread_id"],
                        "expectedTurnId": turn_id,
                        "input": [{"type": "text", "text": prompt}],
                    },
                )
                if result.get("turnId") != turn_id:
                    raise RPCError("turn/steer returned an unexpected turn id")
                metadata["updated_at"] = _now()
                self._write_metadata(metadata)
                return metadata

    def interrupt(self) -> dict[str, Any]:
        cwd, profile = self._identity()
        with FileLock(str(self.lock_path), timeout=10):
            metadata = self._read_metadata()
            self._validate_metadata(metadata, cwd, profile)
            self._refuse_unreconciled(metadata)
            with self.rpc_factory(self.socket) as rpc:
                metadata = self._refresh(rpc, metadata, cwd, profile)
                self._refuse_unreconciled(metadata)
                if metadata.get("status") != "active":
                    return metadata
                turn_id = self._active_turn_id(metadata)
                rpc.request(
                    "turn/interrupt",
                    {
                        "threadId": metadata["thread_id"],
                        "turnId": turn_id,
                    },
                )
                metadata["interrupt_requested"] = True
                metadata["updated_at"] = _now()
                self._write_metadata(metadata)
                return metadata

    def acknowledge_reconciliation(self, note: str) -> dict[str, Any]:
        note = self._text("note", note)
        cwd, profile = self._identity()
        with FileLock(str(self.lock_path), timeout=10):
            metadata = self._read_metadata()
            self._validate_metadata(metadata, cwd, profile)
            if metadata.get("status") != "reconciliation_required":
                raise RuntimeErrorBase("orchestrator does not require reconciliation")
            metadata["reconciled_turn_id"] = metadata.get("last_turn_id") or metadata.get("active_turn_id")
            metadata["reconciliation_note"] = note
            metadata["status"] = "idle"
            metadata.pop("active_turn_id", None)
            metadata.pop("error", None)
            metadata["updated_at"] = _now()
            self._write_metadata(metadata)
            return metadata

    def _ensure_thread(
        self,
        rpc: Any,
        metadata: dict[str, Any],
        cwd: Path,
        profile: str,
    ) -> tuple[str, str | None]:
        common = self._thread_params(cwd, profile)
        thread_id = metadata.get("thread_id")
        fingerprint = self._thread_fingerprint(cwd, profile)
        previous_thread_id = None
        if thread_id:
            current = self._refresh(rpc, metadata, cwd, profile)
            metadata = current
            if current.get("status") == "active":
                raise OrchestratorBusy(
                    "orchestrator already has an active turn; use steer or wait"
                )
            if current.get("status") == "reconciliation_required":
                raise ReconciliationRequired(
                    "orchestrator requires reconciliation before another prompt"
                )
            if metadata.get("thread_config_sha256") == fingerprint:
                resumed = rpc.request(
                    "thread/resume", {"threadId": thread_id, **common}
                )
                self._validate_effective(resumed, cwd, profile)
                return thread_id, None
            previous_thread_id = thread_id
        try:
            started = rpc.request("thread/start", common)
        except RPCUncertainError as exc:
            uncertain = (
                dict(metadata)
                if previous_thread_id is not None
                else {
                    "identity": "orchestrator",
                    "cwd": str(cwd),
                    "profile": profile,
                    "thread_config_sha256": fingerprint,
                }
            )
            if previous_thread_id is not None:
                uncertain["pending_thread_config_sha256"] = fingerprint
                uncertain["previous_thread_id"] = previous_thread_id
            self._mark_ambiguous(uncertain, exc)
        self._validate_effective(started, cwd, profile)
        thread = _thread(started)
        thread_id = thread.get("id")
        if not isinstance(thread_id, str) or not thread_id:
            raise RPCError("thread/start response omitted thread.id")
        return thread_id, previous_thread_id

    def _refresh(
        self,
        rpc: Any,
        metadata: dict[str, Any],
        cwd: Path,
        profile: str,
    ) -> dict[str, Any]:
        if metadata.get("status") == "reconciliation_required":
            return metadata
        response = rpc.request(
            "thread/read",
            {"threadId": metadata["thread_id"], "includeTurns": True},
        )
        thread = _thread(response)
        self._validate_thread_cwd(thread, cwd)
        turns = thread.get("turns") if isinstance(thread.get("turns"), list) else []
        last = turns[-1] if turns and isinstance(turns[-1], dict) else {}
        last_id = last.get("id")
        last_status = last.get("status")
        status = _status(thread)
        refreshed = dict(metadata)
        if isinstance(last_id, str):
            refreshed["last_turn_id"] = last_id
        if isinstance(last_status, str):
            refreshed["last_turn_status"] = last_status
        response_text = _last_response(last)
        if response_text is not None:
            refreshed["last_response"] = response_text
        if status == "active":
            active_turns = [
                turn
                for turn in turns
                if isinstance(turn, dict)
                and turn.get("status") in {"inProgress", "active", "running"}
            ]
            active_id = (
                active_turns[0].get("id") if len(active_turns) == 1 else None
            )
            known_id = refreshed.get("active_turn_id")
            if len(active_turns) > 1:
                refreshed["status"] = "reconciliation_required"
                refreshed["error"] = "active thread response contained multiple active turns"
            elif isinstance(active_id, str) and active_id and known_id not in (
                None,
                active_id,
            ):
                refreshed["status"] = "reconciliation_required"
                refreshed["error"] = (
                    "active turn id mismatch: "
                    f"metadata={known_id!r}, server={active_id!r}"
                )
            elif isinstance(active_id, str) and active_id:
                refreshed["active_turn_id"] = active_id
                refreshed["status"] = "active"
            elif isinstance(known_id, str) and known_id:
                refreshed["status"] = "active"
            else:
                refreshed["status"] = "reconciliation_required"
                refreshed["error"] = "active thread response omitted active turn id"
        elif last_status in {"failed", "interrupted"}:
            if refreshed.get("reconciled_turn_id") == last_id:
                refreshed["status"] = "idle"
            else:
                refreshed["status"] = "reconciliation_required"
                error = last.get("error")
                refreshed["error"] = error if isinstance(error, (str, dict)) else last_status
            refreshed.pop("active_turn_id", None)
        elif last_status == "completed" or status in {"idle", "notLoaded"}:
            refreshed["status"] = "idle"
            refreshed.pop("active_turn_id", None)
            refreshed.pop("interrupt_requested", None)
        else:
            refreshed["status"] = status or "unknown"
        refreshed["updated_at"] = _now()
        self._write_metadata(refreshed)
        return refreshed

    def _thread_params(self, cwd: Path, profile: str) -> dict[str, Any]:
        python = self.settings.get("python")
        if not isinstance(python, str) or not Path(python).is_absolute():
            raise RuntimeErrorBase("settings.python must be an absolute path")
        return {
            "cwd": str(cwd),
            "model": role_model(self.settings, "orchestrator"),
            "permissions": profile,
            "approvalPolicy": "never",
            "developerInstructions": _DEVELOPER_INSTRUCTIONS,
            "config": {
                "mcp_servers.project_agents.command": python,
                "mcp_servers.project_agents.args": [
                    "-m",
                    "codex_project_orchestrator",
                    "--state",
                    str(self.state),
                    "mcp",
                    "--role",
                    "orchestrator",
                ],
                "mcp_servers.project_agents.enabled": True,
            },
        }

    def _validate_effective(
        self, response: Mapping[str, Any], cwd: Path, profile: str
    ) -> None:
        self._validate_thread_cwd(response, cwd)
        active = response.get("activePermissionProfile")
        active_id = active.get("id") if isinstance(active, dict) else None
        if active_id != profile:
            raise RuntimeErrorBase(
                f"orchestrator active permission profile mismatch: expected {profile!r}, got {active_id!r}"
            )
        if response.get("approvalPolicy") != "never":
            raise RuntimeErrorBase("orchestrator approval policy mismatch")

    def _validate_thread_cwd(self, value: Mapping[str, Any], cwd: Path) -> None:
        actual = value.get("cwd")
        if actual is None and isinstance(value.get("thread"), dict):
            actual = value["thread"].get("cwd")
        if actual != str(cwd):
            raise RuntimeErrorBase(
                f"orchestrator cwd mismatch: expected {str(cwd)!r}, got {actual!r}"
            )

    def _identity(self) -> tuple[Path, str]:
        spec = self.settings["orchestrator"]
        return Path(spec["cwd"]), spec["profile"]

    def _require_isolated(self) -> None:
        if self.settings.get("mode") != "isolated":
            raise RuntimeErrorBase("persistent orchestrator requires isolated mode")

    def _validate_metadata(
        self, metadata: Mapping[str, Any], cwd: Path, profile: str
    ) -> None:
        if not metadata:
            return
        if metadata.get("identity") != "orchestrator":
            raise RuntimeErrorBase("orchestrator metadata identity mismatch")
        if metadata.get("cwd") != str(cwd):
            raise RuntimeErrorBase("orchestrator metadata cwd mismatch")
        if metadata.get("profile") != profile:
            raise RuntimeErrorBase("orchestrator metadata profile mismatch")

    @staticmethod
    def _refuse_unreconciled(metadata: Mapping[str, Any]) -> None:
        if metadata.get("status") == "reconciliation_required":
            raise ReconciliationRequired(
                "orchestrator requires explicit reconciliation acknowledgement"
            )

    @staticmethod
    def _active_turn_id(metadata: Mapping[str, Any]) -> str:
        turn_id = metadata.get("active_turn_id")
        if not isinstance(turn_id, str) or not turn_id:
            raise RuntimeErrorBase("active orchestrator turn id is unavailable")
        return turn_id

    def _metadata(
        self, thread_id: str, cwd: Path, profile: str, status: str
    ) -> dict[str, Any]:
        return {
            "identity": "orchestrator",
            "thread_id": thread_id,
            "cwd": str(cwd),
            "profile": profile,
            "thread_config_sha256": self._thread_fingerprint(cwd, profile),
            "status": status,
            "updated_at": _now(),
        }

    def _thread_fingerprint(self, cwd: Path, profile: str) -> str:
        payload = {
            "compiled_config": compiled(self.settings, self.state),
            "thread_params": self._thread_params(cwd, profile),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()

    def _read_metadata(self) -> dict[str, Any]:
        if not self.metadata_path.exists():
            return {}
        try:
            value = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeErrorBase(f"invalid orchestrator metadata: {exc}") from exc
        if not isinstance(value, dict):
            raise RuntimeErrorBase("invalid orchestrator metadata: expected object")
        return value

    def _write_metadata(self, value: Mapping[str, Any]) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            dir=self.state, prefix=".orchestrator.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(dict(value), handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.metadata_path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _mark_ambiguous(
        self, metadata: Mapping[str, Any], error: RPCUncertainError
    ) -> Never:
        uncertain = dict(metadata)
        uncertain.update(
            {
                "identity": "orchestrator",
                "status": "reconciliation_required",
                "updated_at": _now(),
                "error": str(error),
            }
        )
        self._write_metadata(uncertain)
        raise ReconciliationRequired(
            "orchestrator operation became ambiguous; manual reconciliation is required"
        ) from error

    @staticmethod
    def _text(name: str, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be non-empty text")
        if len(value.encode("utf-8")) > 1024 * 1024:
            raise ValueError(f"{name} exceeds 1 MiB")
        return value


def _last_response(turn: Mapping[str, Any]) -> str | None:
    items = turn.get("items")
    if not isinstance(items, list):
        return None
    final: str | None = None
    fallback: str | None = None
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "agentMessage":
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        fallback = text
        if item.get("phase") == "final_answer":
            final = text
    return final if final is not None else fallback


def _now() -> str:
    return datetime.now(UTC).isoformat()
