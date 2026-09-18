"""An operator-started app server; no daemon registration or global config edits."""
import json
import os
from pathlib import Path
import signal
import subprocess
import time

from filelock import FileLock

from .config import atomic, compiled, load
from .runtime import RPC


def environment(state):
    allowed = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "COLORTERM", "TZ"}
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env["CODEX_HOME"] = str(Path(state) / "codex-home")
    return env


def owned(state):
    record = Path(state) / "service.json"
    if not record.exists():
        return None
    data = json.loads(record.read_text())
    command = subprocess.run(["ps", "-p", str(data["pid"]), "-o", "command="], capture_output=True, text=True)
    marker = "unix://" + str(Path(state) / "app.sock")
    if command.returncode == 0 and "app-server" in command.stdout and marker in command.stdout:
        return data["pid"]
    return None


def status(state):
    pid = owned(state)
    ready = False
    if pid:
        try:
            with RPC(Path(state) / "app.sock"):
                ready = True
        except (OSError, RuntimeError, TimeoutError):
            pass
    return {"running": pid is not None, "ready": ready, "pid": pid}


def start(state):
    state = Path(state)
    with FileLock(str(state / "service.lock"), timeout=10):
        settings = load(state)
        if settings["mode"] != "isolated":
            raise ValueError("Worker service is disabled in local mode")
        if owned(state):
            result = status(state)
            if not result["ready"]:
                raise RuntimeError("Owned service is not ready; inspect app-server.log")
            return result
        target = state / "codex-home" / "config.toml"
        if target.read_text() != compiled(settings, state):
            raise ValueError("Configuration drift; run apply with service stopped")
        sock = state / "app.sock"
        if sock.exists():
            raise ValueError("Unowned app.sock exists; reconcile service ownership before removing it")
        with (state / "app-server.log").open("a") as log:
            os.chmod(log.name, 0o600)
            child = subprocess.Popen([settings["codex"], "app-server", "--listen", "unix://" + str(sock)],
                                     cwd=state, env=environment(state), stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        atomic(state / "service.json", json.dumps({"pid": child.pid}))
        for _ in range(80):
            if child.poll() is not None:
                raise RuntimeError("App server exited; inspect private app-server.log")
            if sock.exists():
                try:
                    with RPC(sock):
                        return {"running": True, "ready": True, "pid": child.pid}
                except (OSError, RuntimeError, TimeoutError):
                    pass
            time.sleep(0.1)
        raise RuntimeError("App server startup timed out; inspect private app-server.log")


def stop(state, force=False):
    state = Path(state)
    with FileLock(str(state / "service.lock"), timeout=10):
        pid = owned(state)
        if not pid:
            return {"stopped": False, "reason": "No owned process"}
        if not force:
            # Include workers removed from a newly edited registry. Their old
            # threads may still be running in this service.
            with RPC(state / "app.sock") as rpc:
                for record in state.glob("worker-*.json"):
                    data = json.loads(record.read_text())
                    if not data.get("thread_id"):
                        if data.get("status") == "reconciliation_required":
                            raise RuntimeError("Uncertain worker identity; reconcile or explicitly use stop --force")
                        continue
                    thread = rpc.call("thread/read", {"threadId": data["thread_id"], "includeTurns": False})["thread"]
                    if thread.get("status", {}).get("type") == "active":
                        raise RuntimeError("Worker active; wait, or explicitly use stop --force")
        os.kill(pid, signal.SIGTERM)
        for _ in range(80):
            if not owned(state):
                (state / "app.sock").unlink(missing_ok=True)
                (state / "service.json").unlink(missing_ok=True)
                return {"stopped": True}
            time.sleep(0.1)
        raise RuntimeError("Service did not stop; inspect it before changing mode")
