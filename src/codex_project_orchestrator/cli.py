"""Operator CLI. Runtime state stays separate from registered repositories."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from filelock import FileLock

from .config import atomic, compiled, initialize, load, require_probe, toml


def parser():
    p = argparse.ArgumentParser(prog="cpo", description=__doc__)
    p.add_argument("--state", type=Path, default=Path.home() / ".local/share/codex-project-orchestrator")
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create private state and a fresh orchestrator workspace")
    init.add_argument("--workspace", required=True, type=Path)
    init.add_argument("--project", action="append", required=True, metavar="ID=PATH")
    init.add_argument("--runtime-read", action="append", default=[], metavar="PATH", help="Explicit trusted toolchain read exception")
    sub.add_parser("start", help="Start the local worker app server")
    stop = sub.add_parser("stop", help="Stop the owned app server; refuse active workers")
    stop.add_argument("--force", action="store_true")
    sub.add_parser("status")
    apply = sub.add_parser("apply", help="Compile edited settings after stopping the service")
    apply.add_argument("--dry-run", action="store_true")
    mode = sub.add_parser("mode", help="Change mode for newly launched sessions")
    mode.add_argument("value", choices=["local", "isolated"])
    mode.add_argument("--allow-full-access", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    login = sub.add_parser("login", help="Authenticate the dedicated Codex home")
    login.add_argument("--reuse-current", action="store_true", help="Link the current Codex file credential; never print it")
    sub.add_parser("orch", help="Open an interactive orchestrator in the dedicated home")
    ask = sub.add_parser("ask", help="Run one non-interactive orchestrator prompt")
    ask.add_argument("prompt", nargs="?", help="Prompt text; omit to read it from stdin")
    doctor = sub.add_parser("doctor", help="Check config and optionally probe actual sandbox access")
    doctor.add_argument("--probe", action="store_true")
    mcp = sub.add_parser("mcp", help="Internal fixed-role MCP entrypoint")
    mcp.add_argument("--role", required=True)
    return p


def run(args):
    state = args.state.expanduser().resolve()
    if args.command == "init":
        initialize(state, args.workspace, args.project, args.runtime_read)
        return {"initialized": str(state), "mode": "isolated", "next": "cpo login; cpo start; cpo doctor --probe; cpo orch"}
    settings = load(state)
    from . import services
    if args.command == "start":
        return services.start(state)
    if args.command == "stop":
        return services.stop(state, args.force)
    if args.command == "status":
        return {"mode": settings["mode"], **services.status(state)}
    if args.command in ("apply", "mode"):
        if args.command == "mode":
            if args.value == "local" and not args.allow_full_access:
                raise ValueError("Local mode removes sandbox restrictions; pass --allow-full-access explicitly")
            settings["mode"] = args.value
        if args.dry_run:
            return {"mode": settings["mode"], "config_preview": compiled(settings, state), "written": False}
        with FileLock(str(state / "service.lock"), timeout=10):
            latest = load(state)
            if args.command == "mode":
                latest["mode"] = args.value
            settings = latest
            text = compiled(settings, state)
            if services.owned(state) or (state / "app.sock").exists():
                raise ValueError("Stop the worker service before applying settings; also close old interactive sessions")
            config_path = state / "codex-home/config.toml"
            old = config_path.read_text()
            atomic(state / "config.previous.toml", old)
            atomic(config_path, text)
            try:
                atomic(state / "settings.toml", toml(settings))
            except Exception:
                atomic(config_path, old)
                raise
            return {"mode": settings["mode"], "written": True, "next": "Close old interactive sessions. Start fresh sessions; existing permissions do not change."}
    if args.command == "login":
        destination = state / "codex-home/auth.json"
        if args.reuse_current:
            source = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
            source = source.resolve()
            if not source.is_file() or destination.exists() or destination.is_symlink():
                raise ValueError("Source file credential missing or destination already exists; use cpo login")
            destination.symlink_to(source)
            return {"linked_current_file_credential": True, "note": "Removing the link leaves your original credential intact"}
        returncode = subprocess.call([settings["codex"], "login"], env=services.environment(state))
        if returncode:
            raise RuntimeError("Codex login failed")
        return {"login_completed": True}
    if args.command == "orch":
        if settings["mode"] == "isolated" and not services.status(state)["ready"]:
            raise RuntimeError("Start the worker service first")
        if settings["mode"] == "isolated":
            require_probe(settings, state)
        os.execvpe(settings["codex"], [settings["codex"], "--cd", settings["orchestrator"]["cwd"]], services.environment(state))
    if args.command == "ask":
        if settings["mode"] != "isolated":
            raise RuntimeError("Non-interactive orchestrator prompts require isolated mode")
        if not services.status(state)["ready"]:
            raise RuntimeError("Start the worker service first")
        require_probe(settings, state)
        prompt = args.prompt if args.prompt is not None else sys.stdin.read()
        if not prompt.strip():
            raise ValueError("Provide an orchestrator prompt as an argument or on stdin")
        command = [settings["codex"], "exec", "--cd", settings["orchestrator"]["cwd"],
                   "--skip-git-repo-check", "-"]
        with FileLock(str(state / "orchestrator-exec.lock"), timeout=10):
            completed = subprocess.run(
                command, input=prompt, text=True, env=services.environment(state)
            )
        if completed.returncode:
            raise RuntimeError("Non-interactive orchestrator prompt failed")
        return None
    if args.command == "doctor":
        from .doctor import diagnose
        return diagnose(settings, state, args.probe)
    if args.command == "mcp":
        from .mcp import build
        build(state, args.role).run(transport="stdio", show_banner=False)
        return None
    raise ValueError("Unknown command")


def main():
    os.umask(0o077)
    try:
        result = run(parser().parse_args())
        if result is not None:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        if isinstance(result, dict) and result.get("ok") is False:
            raise SystemExit(1)
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        print("cpo: " + str(exc), file=sys.stderr)
        raise SystemExit(1) from None
