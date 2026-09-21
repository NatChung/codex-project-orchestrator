from __future__ import annotations

import errno
import json
import socket
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import patch

from codex_project_orchestrator.config import TESTED_CODEX, compiled
from codex_project_orchestrator.doctor import diagnose


class FakeRPC:
    handler: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None
    fail = False

    def __init__(self, _socket: Path) -> None:
        pass

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("synthetic RPC failure")
        if method != "command/exec":
            raise AssertionError(method)
        targets = json.loads(params["command"][3])
        handler = type(self).handler
        observed = handler(targets) if handler else successful(targets)
        return {"exitCode": 0, "stdout": json.dumps(observed), "stderr": ""}


def successful(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for target in targets:
        item = {
            key: value for key, value in target.items() if key not in {"path", "port"}
        }
        item["outcome"] = "allowed" if item["expect"] else "denied"
        item["errno"] = None if item["expect"] else errno.EACCES
        output.append(item)
    return output


class DoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.state = root / "state"
        self.state.mkdir(mode=0o700)
        (self.state / "codex-home").mkdir()
        self.orchestrator = root / "orchestrator"
        self.worker = root / "worker"
        self.orchestrator.mkdir()
        self.worker.mkdir()
        self.settings = {
            "mode": "isolated",
            "codex": "codex",
            "python": sys.executable,
            "runtime_read": [],
            "orchestrator": {"cwd": str(self.orchestrator), "profile": "orch"},
            "workers": {"alpha": {"cwd": str(self.worker), "profile": "worker-alpha"}},
        }
        (self.state / "codex-home/config.toml").write_text(
            compiled(self.settings, self.state), encoding="utf-8"
        )
        (self.state / "service.json").write_text(
            '{"pid": 4321, "generation": "generation-a"}', encoding="utf-8"
        )
        self.app_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.app_listener.bind(str(self.state / "app.sock"))
        self.app_listener.listen(1)
        FakeRPC.handler = None
        FakeRPC.fail = False

    def tearDown(self) -> None:
        self.app_listener.close()
        FakeRPC.handler = None
        FakeRPC.fail = False
        self.temporary.cleanup()

    def run_doctor(self) -> dict[str, Any]:
        with (
            patch("codex_project_orchestrator.doctor.RPC", FakeRPC),
            patch(
                "codex_project_orchestrator.doctor.shutil.which",
                return_value="/bin/codex",
            ),
            patch(
                "codex_project_orchestrator.doctor.subprocess.run",
                return_value=SimpleNamespace(stdout="codex-cli " + TESTED_CODEX),
            ),
        ):
            return diagnose(self.settings, self.state, probe=True)

    def test_probes_protected_paths_network_and_app_socket_with_clean_evidence(
        self,
    ) -> None:
        for cwd in (self.orchestrator, self.worker):
            (cwd / ".codex").mkdir()
            (cwd / ".git").write_text("gitdir: external\n", encoding="utf-8")
        result = self.run_doctor()
        self.assertTrue(result["ok"])
        for row in result["sandbox_probes"]:
            by_name = {check["name"]: check for check in row["checks"]}
            for name in (
                "protected:.codex",
                "protected:.git",
                "protected:.agents",
                "network:loopback",
                "state:app-socket",
            ):
                self.assertEqual(by_name[name]["outcome"], "denied")
                self.assertEqual(by_name[name]["errno"], errno.EACCES)
            self.assertTrue(all("path" not in check for check in row["checks"]))
            self.assertTrue(all("port" not in check for check in row["checks"]))
        for cwd in (self.orchestrator, self.worker):
            self.assertEqual((cwd / ".git").read_text(), "gitdir: external\n")
            self.assertEqual(list((cwd / ".codex").iterdir()), [])
            self.assertFalse((cwd / ".agents").exists())
        receipt = json.loads((self.state / "probe.json").read_text())
        self.assertEqual(receipt["service_pid"], 4321)
        self.assertEqual(receipt["service_generation"], "generation-a")
        self.assertTrue(receipt["ok"])

    def test_inconclusive_connection_fails_instead_of_counting_as_denied(self) -> None:
        def inconclusive(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
            output = successful(targets)
            for item in output:
                if item["name"] == "network:loopback":
                    item["outcome"] = "inconclusive"
                    item["errno"] = errno.ECONNREFUSED
            return output

        FakeRPC.handler = inconclusive
        result = self.run_doctor()
        self.assertFalse(result["ok"])
        self.assertFalse(json.loads((self.state / "probe.json").read_text())["ok"])

    def test_service_change_during_probe_writes_no_receipt(self) -> None:
        changed = False

        def restart_service(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
            nonlocal changed
            if not changed:
                (self.state / "service.json").write_text(
                    '{"pid": 4321, "generation": "generation-b"}',
                    encoding="utf-8",
                )
                changed = True
            return successful(targets)

        FakeRPC.handler = restart_service
        with self.assertRaisesRegex(
            RuntimeError, "App server changed during sandbox probe"
        ):
            self.run_doctor()
        self.assertFalse((self.state / "probe.json").exists())

    def test_failure_invalidates_receipt_and_cleans_only_created_artifacts(
        self,
    ) -> None:
        (self.state / "probe.json").write_text('{"ok": true}', encoding="utf-8")
        for cwd in (self.orchestrator, self.worker):
            (cwd / ".codex").mkdir()
            (cwd / ".codex/sentinel").write_text("keep", encoding="utf-8")
            (cwd / ".git").write_text("original", encoding="utf-8")
        FakeRPC.fail = True
        with self.assertRaisesRegex(RuntimeError, "synthetic RPC failure"):
            self.run_doctor()
        self.assertFalse((self.state / "probe.json").exists())
        for cwd in (self.orchestrator, self.worker):
            self.assertEqual((cwd / ".codex/sentinel").read_text(), "keep")
            self.assertEqual((cwd / ".git").read_text(), "original")
            self.assertEqual(
                [path.name for path in (cwd / ".codex").iterdir()], ["sentinel"]
            )
            self.assertFalse((cwd / ".agents").exists())
            self.assertFalse(
                any(path.name.startswith(".cpo-probe-") for path in cwd.iterdir())
            )
        self.assertFalse(
            any(path.name.startswith(".cpo-probe-") for path in self.state.iterdir())
        )

    def test_protected_symlink_is_inconclusive_and_target_untouched(self) -> None:
        external = Path(self.temporary.name) / "external"
        external.mkdir()
        sentinel = external / "sentinel"
        sentinel.write_text("keep", encoding="utf-8")
        (self.worker / ".agents").symlink_to(external, target_is_directory=True)
        result = self.run_doctor()
        self.assertFalse(result["ok"])
        worker = next(row for row in result["sandbox_probes"] if row["role"] == "alpha")
        check = next(
            item for item in worker["checks"] if item["name"] == "protected:.agents"
        )
        self.assertEqual(check["outcome"], "inconclusive")
        self.assertEqual(check["reason"], "symlink")
        self.assertEqual(sentinel.read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
