from __future__ import annotations

import json
import tempfile
import unittest
from collections import deque
from pathlib import Path
from typing import Any, Self

from codex_project_orchestrator.orchestrator import (
    OrchestratorBusy,
    OrchestratorRuntime,
)
from codex_project_orchestrator.runtime import ReconciliationRequired, RPCUncertainError


def settings(root: Path) -> dict[str, Any]:
    orch = root / "orch"
    worker = root / "worker"
    orch.mkdir(parents=True)
    worker.mkdir()
    return {
        "mode": "isolated",
        "python": str(root / "python"),
        "orchestrator": {"cwd": str(orch), "profile": "orch", "model": "orch-model"},
        "workers": {"alpha": {"cwd": str(worker), "profile": "worker-alpha"}},
    }


def response(
    cwd: str,
    *,
    status: str = "idle",
    turns: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "cwd": cwd,
        "approvalPolicy": "never",
        "activePermissionProfile": {"id": "orch"},
        "thread": {
            "id": "orch-thread",
            "cwd": cwd,
            "status": {"type": status},
            "turns": turns or [],
        },
    }


class FakeRPC:
    def __init__(self, replies: dict[str, Any]) -> None:
        self.replies = {
            method: deque(value if isinstance(value, list) else [value])
            for method, value in replies.items()
        }
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, params))
        value = self.replies[method].popleft()
        if isinstance(value, Exception):
            raise value
        return value


def factory(rpc: FakeRPC):
    return lambda _socket: rpc


class OrchestratorRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = settings(self.root)
        self.state = self.root / "state"
        self.cwd = self.settings["orchestrator"]["cwd"]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_prompt_persists_thread_and_uses_orchestrator_profile(self) -> None:
        rpc = FakeRPC(
            {
                "thread/start": response(self.cwd),
                "turn/start": {"turn": {"id": "turn-1"}},
            }
        )
        result = OrchestratorRuntime(
            self.settings, self.state, rpc_factory=factory(rpc)
        ).prompt("delegate work")
        self.assertEqual(result["thread_id"], "orch-thread")
        self.assertEqual(result["active_turn_id"], "turn-1")
        params = rpc.calls[0][1]
        self.assertEqual(params["permissions"], "orch")
        self.assertEqual(params["model"], "orch-model")
        self.assertEqual(params["cwd"], self.cwd)
        self.assertEqual(
            params["config"]["mcp_servers.project_agents.args"][-2:],
            ["--role", "orchestrator"],
        )
        self.assertEqual(rpc.calls[1][0], "turn/start")
        persisted = json.loads((self.state / "orchestrator.json").read_text())
        self.assertEqual(persisted["status"], "active")

    def test_status_captures_final_answer_and_next_prompt_resumes(self) -> None:
        initial = FakeRPC(
            {
                "thread/start": response(self.cwd),
                "turn/start": {"turn": {"id": "turn-1"}},
            }
        )
        runtime = OrchestratorRuntime(
            self.settings, self.state, rpc_factory=factory(initial)
        )
        runtime.prompt("first")
        completed_turn = {
            "id": "turn-1",
            "status": "completed",
            "items": [
                {"type": "agentMessage", "phase": "commentary", "text": "working"},
                {"type": "agentMessage", "phase": "final_answer", "text": "done"},
            ],
        }
        status_rpc = FakeRPC(
            {"thread/read": {"thread": response(self.cwd, turns=[completed_turn])["thread"]}}
        )
        result = OrchestratorRuntime(
            self.settings, self.state, rpc_factory=factory(status_rpc)
        ).status()
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["last_response"], "done")

        resumed = FakeRPC(
            {
                "thread/read": {"thread": response(self.cwd, turns=[completed_turn])["thread"]},
                "thread/resume": response(self.cwd),
                "turn/start": {"turn": {"id": "turn-2"}},
            }
        )
        OrchestratorRuntime(
            self.settings, self.state, rpc_factory=factory(resumed)
        ).prompt("second")
        self.assertEqual(
            [method for method, _ in resumed.calls],
            ["thread/read", "thread/resume", "turn/start"],
        )

    def test_active_status_wins_over_previous_completed_turn(self) -> None:
        initial = FakeRPC(
            {
                "thread/start": response(self.cwd),
                "turn/start": {"turn": {"id": "turn-2"}},
            }
        )
        OrchestratorRuntime(
            self.settings, self.state, rpc_factory=factory(initial)
        ).prompt("start")
        previous = {"id": "turn-1", "status": "completed", "items": []}
        active = FakeRPC(
            {
                "thread/read": {
                    "thread": response(
                        self.cwd, status="active", turns=[previous]
                    )["thread"]
                }
            }
        )
        result = OrchestratorRuntime(
            self.settings, self.state, rpc_factory=factory(active)
        ).status()
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["active_turn_id"], "turn-2")

    def test_configuration_change_starts_a_fresh_thread(self) -> None:
        initial = FakeRPC(
            {
                "thread/start": response(self.cwd),
                "turn/start": {"turn": {"id": "turn-1"}},
            }
        )
        OrchestratorRuntime(
            self.settings, self.state, rpc_factory=factory(initial)
        ).prompt("first")
        changed = dict(self.settings)
        changed["orchestrator"] = dict(self.settings["orchestrator"])
        changed["orchestrator"]["model"] = "changed-model"
        fresh = FakeRPC(
            {
                "thread/start": response(self.cwd),
                "turn/start": {"turn": {"id": "turn-2"}},
            }
        )
        OrchestratorRuntime(
            changed, self.state, rpc_factory=factory(fresh)
        ).prompt("second")
        self.assertEqual(
            [method for method, _ in fresh.calls],
            ["thread/start", "turn/start"],
        )
        self.assertEqual(fresh.calls[0][1]["model"], "changed-model")

    def test_active_turn_can_be_steered_and_interrupted(self) -> None:
        initial = FakeRPC(
            {
                "thread/start": response(self.cwd),
                "turn/start": {"turn": {"id": "turn-1"}},
            }
        )
        OrchestratorRuntime(
            self.settings, self.state, rpc_factory=factory(initial)
        ).prompt("start")
        active_thread = response(
            self.cwd,
            status="active",
            turns=[{"id": "turn-1", "status": "inProgress", "items": []}],
        )["thread"]
        steering = FakeRPC(
            {
                "thread/read": [
                    {"thread": active_thread},
                    {"thread": active_thread},
                ],
                "turn/steer": {"turnId": "turn-1"},
                "turn/interrupt": {},
            }
        )
        runtime = OrchestratorRuntime(
            self.settings, self.state, rpc_factory=factory(steering)
        )
        runtime.steer("focus on tests")
        runtime.interrupt()
        self.assertEqual(
            [method for method, _ in steering.calls],
            ["thread/read", "turn/steer", "thread/read", "turn/interrupt"],
        )

    def test_failed_turn_requires_explicit_reconciliation(self) -> None:
        initial = FakeRPC(
            {
                "thread/start": response(self.cwd),
                "turn/start": {"turn": {"id": "turn-1"}},
            }
        )
        OrchestratorRuntime(
            self.settings, self.state, rpc_factory=factory(initial)
        ).prompt("start")
        failed = {
            "thread": response(
                self.cwd,
                turns=[{"id": "turn-1", "status": "failed", "items": []}],
            )["thread"]
        }
        runtime = OrchestratorRuntime(
            self.settings,
            self.state,
            rpc_factory=factory(FakeRPC({"thread/read": failed})),
        )
        self.assertEqual(runtime.status()["status"], "reconciliation_required")
        with self.assertRaises(ReconciliationRequired):
            OrchestratorRuntime(
                self.settings, self.state, rpc_factory=lambda _: self.fail("no connect")
            ).prompt("retry")
        reconciled = OrchestratorRuntime(
            self.settings, self.state, rpc_factory=lambda _: self.fail("no connect")
        ).acknowledge_reconciliation("checked worker mailbox")
        self.assertEqual(reconciled["status"], "idle")

    def test_disconnect_after_turn_start_marks_ambiguous(self) -> None:
        rpc = FakeRPC(
            {
                "thread/start": response(self.cwd),
                "turn/start": RPCUncertainError("closed"),
            }
        )
        with self.assertRaises(ReconciliationRequired):
            OrchestratorRuntime(
                self.settings, self.state, rpc_factory=factory(rpc)
            ).prompt("delegate")
        metadata = json.loads((self.state / "orchestrator.json").read_text())
        self.assertEqual(metadata["status"], "reconciliation_required")
        self.assertEqual(metadata["thread_id"], "orch-thread")


if __name__ == "__main__":
    unittest.main()
