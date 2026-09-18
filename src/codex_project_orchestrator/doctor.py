"""Probe enforcement with synthetic files and sockets; no model invocation."""

from __future__ import annotations

import errno
import hashlib
import json
import shutil
import socket
import subprocess
import uuid
from pathlib import Path
from typing import Any

from .config import TESTED_CODEX, atomic, compiled
from .runtime import RPC

_PROTECTED = (".codex", ".git", ".agents")
_DENIED_ERRNOS = {errno.EACCES, errno.EPERM}

_PROBE_CODE = r"""import errno,json,os,socket,sys
targets=json.loads(sys.argv[1])
denied={errno.EACCES,errno.EPERM}
for item in targets:
 status='inconclusive'
 number=None
 try:
  op=item['op']
  if op in ('read','write','open_write'):
   flags=os.O_RDONLY if op=='read' else os.O_WRONLY
   fd=os.open(item['path'],flags)
   if op=='read': os.read(fd,1)
   elif op=='write': os.write(fd,b's')
   os.close(fd)
   status='allowed'
  elif op=='tcp_connect':
   sock=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
   sock.settimeout(1)
   number=sock.connect_ex(('127.0.0.1',item['port']))
   sock.close()
   status='allowed' if number==0 else ('denied' if number in denied else 'inconclusive')
  elif op=='unix_connect':
   sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
   sock.settimeout(1)
   number=sock.connect_ex(item['path'])
   sock.close()
   status='allowed' if number==0 else ('denied' if number in denied else 'inconclusive')
 except OSError as exc:
  number=exc.errno
  status='denied' if number in denied else 'inconclusive'
 item['outcome']=status
 item['errno']=number
 item.pop('path',None)
 item.pop('port',None)
print(json.dumps(targets,separators=(',',':')))
"""


def diagnose(
    settings: dict[str, Any], state: Path, probe: bool = False
) -> dict[str, Any]:
    state = Path(state)
    if probe:
        (state / "probe.json").unlink(missing_ok=True)
    binary = shutil.which(settings["codex"])
    version = (
        subprocess.run(
            [binary, "--version"], capture_output=True, text=True, check=False
        ).stdout.strip()
        if binary
        else "missing"
    )
    checks = {
        "tested_codex_version": version == "codex-cli " + TESTED_CODEX,
        "compiled_config_matches": (state / "codex-home/config.toml").read_text()
        == compiled(settings, state),
        "state_private": (state.stat().st_mode & 0o077) == 0,
        "python_exists": Path(settings["python"]).is_file(),
    }
    result: dict[str, Any] = {
        "ok": all(checks.values()),
        "codex": version,
        "mode": settings["mode"],
        "checks": checks,
        "authentication": "Use cpo login before model tasks; probes do not require a model",
    }
    if not probe or not result["ok"]:
        return result
    if settings["mode"] != "isolated":
        raise ValueError("Enforcement probes require isolated mode")

    # Invalidate the previous proof before any operation that can fail.  A
    # successful receipt must describe this exact probe run and service.
    app_socket = state / "app.sock"
    if not app_socket.is_socket():
        raise RuntimeError("App server socket is not ready")

    marker = ".cpo-probe-" + uuid.uuid4().hex
    roles = {"orchestrator": settings["orchestrator"], **settings["workers"]}
    ordinary = {role: Path(spec["cwd"]) / marker for role, spec in roles.items()}
    private = state / marker
    created_files: list[Path] = []
    created_directories: list[Path] = []
    protected: dict[str, list[dict[str, Any]]] = {role: [] for role in roles}
    rows: list[dict[str, Any]] = []
    listener: socket.socket | None = None
    try:
        for path in [*ordinary.values(), private]:
            _create_probe_file(path, created_files)
        for role, spec in roles.items():
            protected[role] = _protected_targets(
                Path(spec["cwd"]), marker, created_files, created_directories
            )

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(max(8, len(roles) + 1))
        port = listener.getsockname()[1]
        # Prove the endpoint is reachable outside the sandbox, so a refused or
        # timed-out sandbox connection cannot masquerade as network denial.
        control = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        control.settimeout(1)
        if control.connect_ex(("127.0.0.1", port)) != 0:
            raise RuntimeError("Loopback control connection failed")
        accepted, _ = listener.accept()
        accepted.close()
        control.close()

        for role, spec in roles.items():
            targets = [
                {"name": owner, "path": str(path), "op": op, "expect": owner == role}
                for owner, path in ordinary.items()
                for op in ("read", "write")
            ]
            targets += [
                {"name": "state", "path": str(private), "op": op, "expect": False}
                for op in ("read", "write")
            ]
            targets += protected[role]
            targets += [
                {
                    "name": "network:loopback",
                    "port": port,
                    "op": "tcp_connect",
                    "expect": False,
                },
                {
                    "name": "state:app-socket",
                    "path": str(app_socket),
                    "op": "unix_connect",
                    "expect": False,
                },
            ]
            runnable = [target for target in targets if "outcome" not in target]
            fixed = [target for target in targets if "outcome" in target]
            with RPC(app_socket) as rpc:
                response = rpc.call(
                    "command/exec",
                    {
                        "command": [
                            str(Path(settings["python"]).resolve()),
                            "-c",
                            _PROBE_CODE,
                            json.dumps(runnable),
                        ],
                        "cwd": spec["cwd"],
                        "permissionProfile": spec["profile"],
                        "timeoutMs": 10_000,
                    },
                )
            if response["exitCode"]:
                raise RuntimeError(
                    "Sandbox probe command failed for "
                    + role
                    + ": "
                    + response.get("stderr", "")[:1000]
                )
            observed = json.loads(response["stdout"])
            if not isinstance(observed, list) or len(observed) != len(runnable):
                raise RuntimeError("Sandbox probe returned malformed evidence")
            role_checks = observed + fixed
            rows.append({"role": role, "checks": role_checks})

        result["sandbox_probes"] = rows
        result["ok"] = all(
            _check_passed(check) for row in rows for check in row["checks"]
        )
        receipt = {
            "ok": result["ok"],
            "config_sha256": hashlib.sha256(
                compiled(settings, state).encode()
            ).hexdigest(),
            "service_pid": json.loads((state / "service.json").read_text())["pid"],
        }
        atomic(state / "probe.json", json.dumps(receipt))
        return result
    finally:
        if listener is not None:
            listener.close()
        for path in reversed(created_files):
            path.unlink(missing_ok=True)
        for path in reversed(created_directories):
            try:
                path.rmdir()
            except OSError:
                # Never remove a directory if anything appeared in it during
                # the probe or if it wasn't created by this run.
                pass


def _create_probe_file(path: Path, created: list[Path]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        # Record ownership as soon as creation succeeds so a later write or
        # flush failure still removes only this run's file.
        created.append(path)
        handle.write("synthetic probe\n")


def _protected_targets(
    cwd: Path,
    marker: str,
    created_files: list[Path],
    created_directories: list[Path],
) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for name in _PROTECTED:
        protected = cwd / name
        logical = "protected:" + name
        if protected.is_symlink():
            targets.append(
                {
                    "name": logical,
                    "op": "write",
                    "expect": False,
                    "outcome": "inconclusive",
                    "errno": None,
                    "reason": "symlink",
                }
            )
            continue
        if protected.exists() and not protected.is_dir():
            # Opening O_WRONLY without writing tests the deny rule without
            # changing a gitfile or other existing protected content.
            targets.append(
                {
                    "name": logical,
                    "path": str(protected),
                    "op": "open_write",
                    "expect": False,
                }
            )
            continue
        if not protected.exists():
            protected.mkdir()
            created_directories.append(protected)
        probe_file = protected / marker
        _create_probe_file(probe_file, created_files)
        targets.append(
            {
                "name": logical,
                "path": str(probe_file),
                "op": "write",
                "expect": False,
            }
        )
    return targets


def _check_passed(check: dict[str, Any]) -> bool:
    expected = "allowed" if check["expect"] else "denied"
    if check.get("outcome") != expected:
        return False
    return expected != "denied" or check.get("errno") in _DENIED_ERRNOS
