from __future__ import annotations

import json
import tempfile
import unittest
from collections import deque
from pathlib import Path
from typing import Any, Self
from unittest.mock import patch

from codex_project_orchestrator.runtime import (
    RPC,
    ReconciliationRequired,
    RPCUncertainError,
    Runtime,
    RuntimeErrorBase,
)


def make_settings(root: Path, mode: str = "isolated") -> dict[str, Any]:
    cwd = root / "alpha"
    cwd.mkdir(parents=True)
    return {
        "mode": mode,
        "orchestrator": {"cwd": str(root), "profile": "orch"},
        "workers": {"alpha": {"cwd": str(cwd), "profile": "worker-alpha"}},
        "python": str(root / "venv/bin/python"),
    }


def app_response(
    cwd: str,
    profile: str = "worker-alpha",
    status: str = "idle",
    turns: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "cwd": cwd,
        "approvalPolicy": "never",
        "activePermissionProfile": {"id": profile},
        "thread": {
            "id": "thread-1",
            "cwd": cwd,
            "status": {"type": status},
            "turns": turns or [],
        },
    }


class FakeRPC:
    def __init__(self, replies: dict[str, Any]) -> None:
        self.replies = replies
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, params))
        value = self.replies[method]
        if isinstance(value, Exception):
            raise value
        return value


def factory(rpc: FakeRPC):
    return lambda _socket: rpc


def write_metadata(state: Path, configured: dict[str, Any], **overrides: Any) -> None:
    state.mkdir(parents=True, exist_ok=True)
    worker = configured["workers"]["alpha"]
    value = {
        "project": "alpha",
        "thread_id": "thread-1",
        "cwd": worker["cwd"],
        "profile": worker["profile"],
        "status": "idle",
    }
    value.update(overrides)
    (state / "worker-alpha.json").write_text(json.dumps(value), encoding="utf-8")


class RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_mode_and_unknown_project_gate_before_connect(self) -> None:
        called = False

        def no_connect(_socket: Path):
            nonlocal called
            called = True
            raise AssertionError

        runtime = Runtime(
            make_settings(self.root, "local"),
            self.root / "state",
            rpc_factory=no_connect,
        )
        with self.assertRaisesRegex(RuntimeErrorBase, "isolated mode"):
            runtime.wake("alpha")
        second = self.root / "second"
        runtime = Runtime(
            make_settings(second), self.root / "state2", rpc_factory=no_connect
        )
        with self.assertRaisesRegex(RuntimeErrorBase, "unknown registered worker"):
            runtime.wake("missing")
        self.assertFalse(called)

    def test_persisted_profile_and_cwd_mismatch_refused_before_connect(self) -> None:
        for field, bad in (("profile", "wrong"), ("cwd", "/wrong")):
            with self.subTest(field=field):
                state = self.root / field
                configured = make_settings(self.root / (field + "-root"))
                write_metadata(state, configured, **{field: bad})
                with self.assertRaisesRegex(RuntimeErrorBase, field + " mismatch"):
                    Runtime(
                        configured,
                        state,
                        rpc_factory=lambda _: self.fail("must not connect"),
                    ).wake("alpha")

    def test_effective_profile_and_cwd_mismatch_never_starts_turn(self) -> None:
        configured = make_settings(self.root)
        expected_cwd = configured["workers"]["alpha"]["cwd"]
        for reply, message in (
            (app_response(expected_cwd, profile="wrong"), "profile mismatch"),
            (app_response("/wrong"), "cwd mismatch"),
        ):
            with self.subTest(message=message):
                rpc = FakeRPC({"thread/start": reply})
                with self.assertRaisesRegex(RuntimeErrorBase, message):
                    Runtime(
                        configured,
                        self.root / (message.split()[0]),
                        rpc_factory=factory(rpc),
                    ).wake("alpha")
                self.assertEqual([item[0] for item in rpc.calls], ["thread/start"])

    def test_active_thread_is_preserved_without_new_turn(self) -> None:
        configured = make_settings(self.root)
        state = self.root / "state"
        write_metadata(state, configured)
        cwd = configured["workers"]["alpha"]["cwd"]
        rpc = FakeRPC(
            {"thread/read": {"thread": app_response(cwd, status="active")["thread"]}}
        )
        result = Runtime(configured, state, rpc_factory=factory(rpc)).wake("alpha")
        self.assertEqual(result["status"], "active")
        self.assertEqual([item[0] for item in rpc.calls], ["thread/read"])

    def test_thread_identity_persists_and_resumes(self) -> None:
        configured = make_settings(self.root)
        state = self.root / "state"
        cwd = configured["workers"]["alpha"]["cwd"]
        first = FakeRPC({"thread/start": app_response(cwd), "turn/start": {"turn": {}}})
        Runtime(configured, state, rpc_factory=factory(first)).wake("alpha")
        persisted = json.loads((state / "worker-alpha.json").read_text())
        self.assertEqual(persisted["thread_id"], "thread-1")
        params = first.calls[0][1]
        self.assertEqual(params["model"], "gpt-5.6-sol")
        self.assertEqual(first.calls[1][1]["model"], "gpt-5.6-sol")
        self.assertEqual(params["permissions"], "worker-alpha")
        self.assertEqual(params["approvalPolicy"], "never")
        self.assertEqual(
            params["config"]["mcp_servers.project_agents.args"],
            [
                "-m",
                "codex_project_orchestrator",
                "--state",
                str(state),
                "mcp",
                "--role",
                "alpha",
            ],
        )
        second = FakeRPC(
            {
                "thread/read": {"thread": app_response(cwd)["thread"]},
                "thread/resume": app_response(cwd),
                "turn/start": {"turn": {}},
            }
        )
        configured["workers"]["alpha"]["model"] = "synthetic-worker-override"
        Runtime(configured, state, rpc_factory=factory(second)).wake("alpha")
        self.assertEqual(second.calls[1][1]["model"], "synthetic-worker-override")
        self.assertEqual(second.calls[2][1]["model"], "synthetic-worker-override")
        self.assertEqual(
            [item[0] for item in second.calls],
            ["thread/read", "thread/resume", "turn/start"],
        )

    def test_worker_status_refreshes_metadata_without_turns(self) -> None:
        configured = make_settings(self.root)
        state = self.root / "state"
        self.assertEqual(
            Runtime(configured, state).worker_status("alpha"),
            {"project": "alpha", "status": "not_started"},
        )
        write_metadata(state, configured)
        cwd = configured["workers"]["alpha"]["cwd"]
        rpc = FakeRPC(
            {"thread/read": {"thread": app_response(cwd, status="idle")["thread"]}}
        )
        result = Runtime(configured, state, rpc_factory=factory(rpc)).worker_status(
            "alpha"
        )
        self.assertEqual(result["status"], "idle")
        self.assertEqual(
            rpc.calls,
            [("thread/read", {"threadId": "thread-1", "includeTurns": False})],
        )

    def test_dynamic_worker_probes_and_uses_same_permission_profile(self) -> None:
        configured = make_settings(self.root)
        cwd = configured["workers"]["alpha"]["cwd"]
        dynamic = {
            "alpha": {
                "worker_id": "alpha",
                "cwd": cwd,
                "profile": "worktree-alpha",
                "model": "synthetic-dynamic",
                "dynamic_worktree": True,
                "git_common_dir": str(self.root / "git-common"),
                "git_control_dir": str(self.root / "git-control"),
            }
        }
        rpc = FakeRPC(
            {
                "thread/start": app_response(cwd, profile="worktree-alpha"),
                "turn/start": {"turn": {}},
            }
        )
        with (
            patch(
                "codex_project_orchestrator.worktrees.effective_workers",
                return_value=dynamic,
            ),
            patch("codex_project_orchestrator.doctor.probe_worktree") as probe,
        ):
            Runtime(
                configured, self.root / "dynamic-state", rpc_factory=factory(rpc)
            ).wake("alpha")
        probe.assert_called_once()
        started = rpc.calls[0][1]
        turn = rpc.calls[1][1]
        self.assertEqual(started["permissions"], "worktree-alpha")
        self.assertEqual(turn["permissions"], "worktree-alpha")
        self.assertNotIn("sandboxPolicy", turn)

    def test_failed_turn_requires_reconciliation(self) -> None:
        configured = make_settings(self.root)
        state = self.root / "state"
        write_metadata(state, configured)
        cwd = configured["workers"]["alpha"]["cwd"]
        thread = app_response(cwd, turns=[{"status": "failed"}])["thread"]
        rpc = FakeRPC({"thread/read": {"thread": thread}})
        with self.assertRaisesRegex(ReconciliationRequired, "last turn was failed"):
            Runtime(configured, state, rpc_factory=factory(rpc)).wake("alpha")
        self.assertEqual([item[0] for item in rpc.calls], ["thread/read"])

    def test_disconnect_marks_ambiguous_and_blocks_retry(self) -> None:
        configured = make_settings(self.root)
        state = self.root / "state"
        cwd = configured["workers"]["alpha"]["cwd"]
        rpc = FakeRPC(
            {
                "thread/start": app_response(cwd),
                "turn/start": RPCUncertainError("closed"),
            }
        )
        with self.assertRaisesRegex(ReconciliationRequired, "ambiguous"):
            Runtime(configured, state, rpc_factory=factory(rpc)).wake("alpha")
        metadata = json.loads((state / "worker-alpha.json").read_text())
        self.assertEqual(metadata["thread_id"], "thread-1")
        self.assertEqual(metadata["status"], "reconciliation_required")
        with self.assertRaisesRegex(ReconciliationRequired, "manual reconciliation"):
            Runtime(
                configured, state, rpc_factory=lambda _: self.fail("must not connect")
            ).wake("alpha")


class FakeWebSocket:
    def __init__(self, incoming: list[Any]) -> None:
        self.incoming = deque(incoming)
        self.sent: list[dict[str, Any]] = []

    def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def recv(self, *, timeout: float) -> str:
        value = self.incoming.popleft()
        if isinstance(value, Exception):
            raise value
        return json.dumps(value)


class RPCTests(unittest.TestCase):
    def test_server_requests_are_answered_and_response_ids_matched(self) -> None:
        websocket = FakeWebSocket(
            [
                {"jsonrpc": "2.0", "id": 99, "method": "item/tool/requestUserInput"},
                {"jsonrpc": "2.0", "method": "thread/status/changed"},
                {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}},
            ]
        )
        rpc = RPC(Path("/tmp/app.sock"), timeout=1)
        rpc._websocket = websocket
        self.assertEqual(rpc.call("test", {}), {"ok": True})
        self.assertEqual(websocket.sent[1]["id"], 99)
        self.assertEqual(websocket.sent[1]["error"]["code"], -32000)

    def test_disconnect_is_uncertain(self) -> None:
        rpc = RPC(Path("/tmp/app.sock"), timeout=1)
        rpc._websocket = FakeWebSocket([EOFError("closed")])
        with self.assertRaisesRegex(RPCUncertainError, "disconnected"):
            rpc.request("thread/start", {})


if __name__ == "__main__":
    unittest.main()
