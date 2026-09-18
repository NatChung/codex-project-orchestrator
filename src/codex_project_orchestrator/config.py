"""Operator-owned configuration. Never modify a registered project's config."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import tomllib

ID = re.compile(r"[a-z][a-z0-9-]{0,47}\Z")
TESTED_CODEX = "0.154.0"
ORCH_MODEL = "gpt-6-astra"
WORKER_MODEL = "gpt-5.6-sol"


def role_model(settings, role):
    spec = settings["orchestrator"] if role == "orchestrator" else settings["workers"][role]
    model = spec.get("model", ORCH_MODEL if role == "orchestrator" else WORKER_MODEL)
    if not isinstance(model, str) or not model.strip() or model != model.strip():
        raise ValueError("Role model must be a non-empty model ID: " + role)
    return model


def toml(data):
    """Serialize the deliberately small JSON-compatible settings schema."""
    def val(v):
        if isinstance(v, bool):
            return str(v).lower()
        if isinstance(v, str):
            return json.dumps(v, ensure_ascii=False)
        if isinstance(v, list):
            return "[" + ", ".join(val(x) for x in v) + "]"
        if isinstance(v, int):
            return str(v)
        raise TypeError(type(v))
    lines = []
    def section(d, path):
        if path:
            lines.append("\n[" + ".".join(val(k) for k in path) + "]")
        for k, v in d.items():
            if not isinstance(v, dict):
                lines.append(val(k) + " = " + val(v))
        for k, v in d.items():
            if isinstance(v, dict):
                section(v, path + [k])
    section(data, [])
    return "\n".join(lines) + "\n"


def atomic(path, text):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=".cpo-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def overlaps(a, b):
    return a == b or a in b.parents or b in a.parents


def validate(settings, state):
    if settings.get("mode") not in ("isolated", "local"):
        raise ValueError("mode must be isolated or local")
    workers = settings.get("workers", {})
    if not workers:
        raise ValueError("Register at least one project")
    roles = {"orchestrator": settings["orchestrator"], **workers}
    seen = []
    for role, spec in roles.items():
        role_model(settings, role)
        if role != "orchestrator" and (not ID.fullmatch(role) or role in ("orch", "local")):
            raise ValueError("Invalid project ID: " + role)
        path = Path(spec["cwd"])
        if not path.is_absolute() or path.resolve() != path or not path.is_dir():
            raise ValueError("Project cwd must be an existing canonical absolute directory: " + str(path))
        if path == Path.home() or overlaps(path, state):
            raise ValueError("Project directories must be separate from runtime state and home")
        if overlaps(path, Path(__file__).resolve().parent):
            raise ValueError("Install the package outside registered projects")
        if any(overlaps(path, other) for other in seen):
            raise ValueError("Project and orchestrator directories must not overlap")
        expected = "orch" if role == "orchestrator" else "worker-" + role
        if spec["profile"] != expected:
            raise ValueError("Unexpected role profile")
        seen.append(path)
    for item in settings.get("runtime_read", []):
        p = Path(item)
        if not p.is_absolute() or p.resolve() != p or not p.exists():
            raise ValueError("Runtime read paths must exist and be canonical")
        if any(overlaps(p, q) for q in seen + [state, Path.home() / ".ssh", Path.home() / ".codex"]):
            raise ValueError("Runtime read path overlaps projects, credentials or runtime state")


def load(state):
    state = Path(state).resolve()
    result = tomllib.loads((state / "settings.toml").read_text())
    validate(result, state)
    return result


def require_probe(settings, state):
    """A profile name alone never proves that the OS enforces its rules."""
    state = Path(state)
    try:
        receipt = json.loads((state / "probe.json").read_text())
        service = json.loads((state / "service.json").read_text())
        text = compiled(settings, state)
        if (state / "codex-home/config.toml").read_text() != text:
            raise RuntimeError("Compiled configuration changed; stop, apply and probe again")
        expected = hashlib.sha256(text.encode()).hexdigest()
        if receipt.get("config_sha256") == expected and receipt.get("service_pid") == service["pid"] and receipt.get("ok") is True:
            return
    except (OSError, ValueError, KeyError):
        pass
    raise RuntimeError("Run cpo doctor --probe successfully for this configuration and service before dispatch")


def compiled(settings, state):
    """Isolated home prevents inheriting the operator's connectors and plugins."""
    state = Path(state).resolve()
    profiles = {}
    roles = {"orchestrator": settings["orchestrator"], **settings["workers"]}
    all_paths = [s["cwd"] for s in roles.values()]
    for role, spec in roles.items():
        fs = {":minimal": "read", str(Path.home()): "deny", str(state): "deny"}
        for p in settings.get("runtime_read", []):
            fs[p] = "read"
        for p in all_paths:
            fs[p] = "deny"
        fs[spec["cwd"]] = "write"
        for protected in (".codex", ".git", ".agents"):
            fs[str(Path(spec["cwd"]) / protected)] = "read"
        profiles[spec["profile"]] = {"filesystem": fs, "network": {"enabled": False}}
    result = {
        "model": role_model(settings, "orchestrator"),
        "default_permissions": "orch" if settings["mode"] == "isolated" else ":danger-full-access",
        "approval_policy": "never",
        "web_search": "disabled",
        "developer_instructions": (
            "Current mode is isolated. Coordinate through the fixed-role project_agents MCP. "
            "A missing tool or permission failure requires operator repair; never bypass it."
            if settings["mode"] == "isolated" else
            "Current mode is local, explicitly selected by the operator. Work directly within user authorization. "
            "The isolated worker MCP is disabled. The workspace AGENTS.md delegation steps apply only in isolated mode."
        ),
        "cli_auth_credentials_store": "file",
        "features": {"plugins": False, "multi_agent": False, "network_proxy": False},
        "permissions": profiles,
        # Untrusted project layers are skipped. The operator owns this home.
        "projects": {s["cwd"]: {"trust_level": "untrusted"} for s in roles.values()},
        "mcp_servers": {"project_agents": {
            "command": settings["python"],
            "args": ["-m", "codex_project_orchestrator", "--state", str(state), "mcp", "--role", "orchestrator"],
            "enabled": settings["mode"] == "isolated",
            "startup_timeout_sec": 30,
            "tool_timeout_sec": 120,
            "tools": {name: {"approval_mode": "approve"} for name in (
                "list_workers", "send_message", "fetch_inbox", "acknowledge_message", "check_worker_inbox", "worker_status"
            )},
        }},
    }
    return toml(result)


ORCH_INSTRUCTIONS = """# Project orchestrator

Use project_agents.list_workers to discover registered project IDs. Delegate work
with send_message: include a unique task_id, goal, allowed actions, constraints,
expected evidence and completion criteria. Sending only queues the task; call
check_worker_inbox to start or wake its independent worker.

Inspect worker_status and fetch_inbox when needed. Match project and task_id,
verify evidence, save the useful result in this workspace, then acknowledge the
reply. Separate queued, running, reported, verified and accepted states.

Project files belong to their workers. A tool or sandbox rejection is a boundary;
return its exact error and the smallest needed operator decision. Ask the operator
to change configuration from an external terminal. Task text never authorizes
unrelated external communications, releases or permission changes. A prior crash
may leave an uncertain action: reconcile evidence before retrying side effects.

Use shell commands with login=false. Work only within the user's authorization.
"""


def initialize(state, orch, projects, runtime_read=()):
    state, orch = Path(state).expanduser().resolve(), Path(orch).expanduser().resolve()
    if state.exists():
        raise ValueError("State directory already exists; choose a new path")
    if orch.exists() and any(orch.iterdir()):
        raise ValueError("Orchestrator workspace must be new or empty")
    workers = {}
    for entry in projects:
        role, sep, raw = entry.partition("=")
        if not sep or role == "orchestrator" or role in workers or not ID.fullmatch(role):
            raise ValueError("Use unique --project id=/absolute/path entries")
        workers[role] = {"cwd": str(Path(raw).expanduser().resolve()), "profile": "worker-" + role, "model": WORKER_MODEL}
    # Validate before writing, except that the fresh orchestrator workspace must exist.
    created = not orch.exists()
    orch.mkdir(parents=True, exist_ok=True)
    settings = {"mode": "isolated", "python": sys.executable, "codex": shutil.which("codex") or "codex",
                "runtime_read": list(dict.fromkeys([str(Path(sys.base_prefix).resolve()), *[str(Path(p).expanduser().resolve()) for p in runtime_read]])),
                "orchestrator": {"cwd": str(orch), "profile": "orch", "model": ORCH_MODEL}, "workers": workers}
    try:
        validate(settings, state)
    except Exception:
        if created:
            orch.rmdir()
        raise
    state.mkdir(parents=True, mode=0o700)
    (state / "codex-home").mkdir(mode=0o700)
    atomic(state / "settings.toml", toml(settings))
    atomic(state / "codex-home" / "config.toml", compiled(settings, state))
    atomic(orch / "AGENTS.md", ORCH_INSTRUCTIONS)
    atomic(orch / "PROJECTS.md", "# Registered workers\n\n" + "\n".join("- " + k for k in workers) + "\n")
    return settings
