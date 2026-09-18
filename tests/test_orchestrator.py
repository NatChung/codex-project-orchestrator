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
    thread_id: str = "orch-thread",
) -> dict[str, Any]:
    return {
        "cwd": cwd,
        "approvalPolicy": "never",
        "activePermissionProfile": {"id": "orch"},
        "thread": {
            "id": thread_id,
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

    def test_configuration_change_rotates_only_after_old_thread_completes(self) -> None:
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
                "thread/read": {
                    "thread": response(
                        self.cwd,
                        turns=[{"id": "turn-1", "status": "completed", "items": []}],
                    )["thread"]
                },
                "thread/start": response(self.cwd, thread_id="orch-thread-2"),
                "turn/start": {"turn": {"id": "turn-2"}},
            }
        )
        OrchestratorRuntime(
            changed, self.state, rpc_factory=factory(fresh)
        ).prompt("second")
        self.assertEqual(
            [method for method, _ in fresh.calls],
            ["thread/read", "thread/start", "turn/start"],
        )
        self.assertEqual(fresh.calls[1][1]["model"], "changed-model")
        persisted = json.loads((self.state / "orchestrator.json").read_text())
        self.assertEqual(persisted["thread_id"], "orch-thread-2")
        self.assertEqual(persisted["previous_thread_id"], "orch-thread")

    def test_configuration_change_refuses_to_rotate_active_thread(self) -> None:
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
        active_rpc = FakeRPC(
            {
                "thread/read": {
                    "thread": response(
                        self.cwd,
                        status="active",
                        turns=[{"id": "turn-1", "status": "inProgress", "items": []}],
                    )["thread"]
                }
            }
        )
        with self.assertRaises(OrchestratorBusy):
            OrchestratorRuntime(
                changed, self.state, rpc_factory=factory(active_rpc)
            ).prompt("second")
        self.assertEqual([method for method, _ in active_rpc.calls], ["thread/read"])

    def test_configuration_change_refuses_to_rotate_failed_thread(self) -> None:
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
        failed_rpc = FakeRPC(
            {
                "thread/read": {
                    "thread": response(
                        self.cwd,
                        turns=[{"id": "turn-1", "status": "failed", "items": []}],
                    )["thread"]
                }
            }
        )
        with self.assertRaises(ReconciliationRequired):
            OrchestratorRuntime(
                changed, self.state, rpc_factory=factory(failed_rpc)
            ).prompt("second")
        self.assertEqual([method for method, _ in failed_rpc.calls], ["thread/read"])

    def test_uncertain_rotation_preserves_previous_thread_identity(self) -> None:
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
        rotation = FakeRPC(
            {
                "thread/read": {
                    "thread": response(
                        self.cwd,
                        turns=[{"id": "turn-1", "status": "completed", "items": []}],
                    )["thread"]
                },
                "thread/start": RPCUncertainError("closed"),
            }
        )
        with self.assertRaises(ReconciliationRequired):
            OrchestratorRuntime(
                changed, self.state, rpc_factory=factory(rotation)
            ).prompt("second")
        persisted = json.loads((self.state / "orchestrator.json").read_text())
        self.assertEqual(persisted["status"], "reconciliation_required")
        self.assertEqual(persisted["thread_id"], "orch-thread")
        self.assertEqual(persisted["previous_thread_id"], "orch-thread")
        self.assertIn("pending_thread_config_sha256", persisted)

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

    def test_controls_cannot_clear_reconciliation_without_acknowledgement(self) -> None:
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
        runtime = OrchestratorRuntime(
            self.settings,
            self.state,
            rpc_factory=lambda _: self.fail("control must not connect before ack"),
        )
        with self.assertRaises(ReconciliationRequired):
            runtime.steer("continue")
        with self.assertRaises(ReconciliationRequired):
            runtime.interrupt()
        metadata = json.loads((self.state / "orchestrator.json").read_text())
        self.assertEqual(metadata["status"], "reconciliation_required")
        self.assertNotIn("reconciliation_note", metadata)

    def test_acknowledged_active_turn_recovers_id_for_controls(self) -> None:
        uncertain = FakeRPC(
            {
                "thread/start": response(self.cwd),
                "turn/start": RPCUncertainError("closed"),
            }
        )
        with self.assertRaises(ReconciliationRequired):
            OrchestratorRuntime(
                self.settings, self.state, rpc_factory=factory(uncertain)
            ).prompt("delegate")
        OrchestratorRuntime(
            self.settings, self.state, rpc_factory=lambda _: self.fail("no connect")
        ).acknowledge_reconciliation("verified the original turn is still active")
        active_thread = response(
            self.cwd,
            status="active",
            turns=[{"id": "turn-recovered", "status": "inProgress", "items": []}],
        )["thread"]
        control = FakeRPC(
            {
                "thread/read": [
                    {"thread": active_thread},
                    {"thread": active_thread},
                ],
                "turn/steer": {"turnId": "turn-recovered"},
                "turn/interrupt": {},
            }
        )
        runtime = OrchestratorRuntime(
            self.settings, self.state, rpc_factory=factory(control)
        )
        runtime.steer("continue")
        runtime.interrupt()
        self.assertEqual(control.calls[1][1]["expectedTurnId"], "turn-recovered")
        self.assertEqual(control.calls[3][1]["turnId"], "turn-recovered")

    def test_active_thread_without_recoverable_turn_id_fails_safe(self) -> None:
        uncertain = FakeRPC(
            {
                "thread/start": response(self.cwd),
                "turn/start": RPCUncertainError("closed"),
            }
        )
        with self.assertRaises(ReconciliationRequired):
            OrchestratorRuntime(
                self.settings, self.state, rpc_factory=factory(uncertain)
            ).prompt("delegate")
        OrchestratorRuntime(
            self.settings, self.state, rpc_factory=lambda _: self.fail("no connect")
        ).acknowledge_reconciliation("checked remote state")
        active = FakeRPC(
            {"thread/read": {"thread": response(self.cwd, status="active")["thread"]}}
        )
        with self.assertRaises(ReconciliationRequired):
            OrchestratorRuntime(
                self.settings, self.state, rpc_factory=factory(active)
            ).steer("continue")
        self.assertEqual([method for method, _ in active.calls], ["thread/read"])

    def test_inconsistent_active_turn_ids_require_reconciliation(self) -> None:
        cases = {
            "mismatch": [
                {"id": "turn-other", "status": "inProgress", "items": []}
            ],
            "multiple": [
                {"id": "turn-1", "status": "inProgress", "items": []},
                {"id": "turn-2", "status": "inProgress", "items": []},
            ],
        }
        for name, turns in cases.items():
            with self.subTest(name=name):
                state = self.root / ("state-" + name)
                initial = FakeRPC(
                    {
                        "thread/start": response(self.cwd),
                        "turn/start": {"turn": {"id": "turn-1"}},
                    }
                )
                OrchestratorRuntime(
                    self.settings, state, rpc_factory=factory(initial)
                ).prompt("start")
                active = FakeRPC(
                    {
                        "thread/read": {
                            "thread": response(
                                self.cwd, status="active", turns=turns
                            )["thread"]
                        }
                    }
                )
                with self.assertRaises(ReconciliationRequired):
                    OrchestratorRuntime(
                        self.settings, state, rpc_factory=factory(active)
                    ).interrupt()
                self.assertEqual(
                    [method for method, _ in active.calls], ["thread/read"]
                )


if __name__ == "__main__":
    unittest.main()
