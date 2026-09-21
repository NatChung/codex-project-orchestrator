"""Fixed-identity adapter. Tool callers cannot choose sender or mailbox owner."""
import asyncio
from pathlib import Path

from fastmcp import FastMCP

from .config import load, require_probe
from .mailbox import Mailbox
from .orchestrator import OrchestratorRuntime
from .runtime import Runtime
from .worktrees import WorktreeRegistry, effective_workers


def build(state, role):
    state = Path(state)
    settings = load(state)
    if settings["mode"] != "isolated":
        raise ValueError("Role-bound MCP is disabled in local mode")
    if role == "operator":
        return _build_operator(state, settings)
    if role not in ("orchestrator", *effective_workers(settings, state)):
        raise ValueError("Unknown fixed identity")
    server = FastMCP("project-agents-" + role)

    def current():
        latest = load(state)
        if latest != settings:
            raise RuntimeError("Settings changed; restart this session and MCP process")
        if role != "orchestrator" and role not in effective_workers(latest, state):
            raise RuntimeError("Worker registration changed; restart this worker MCP process")
        return latest

    def mailbox():
        return Mailbox(state / "mail.sqlite3", effective_workers(current(), state))

    @server.tool()
    def list_workers() -> dict:
        """Return this fixed identity and registered worker IDs."""
        return {"identity": role, "workers": list(effective_workers(current(), state))}

    @server.tool()
    def send_message(to: str, subject: str, body: str, task_id: str) -> dict:
        """Queue authorized task or final result. Sending does not start a worker.
        Include scope, allowed actions, evidence and completion criteria in tasks.
        Replies use the same task_id. Identical resends deduplicate; changes fail.
        """
        return mailbox().send(role, to, subject, body, task_id)

    @server.tool()
    def fetch_inbox() -> list[dict]:
        """Read this identity's unacknowledged messages only."""
        return mailbox().inbox(role)

    @server.tool()
    def acknowledge_message(message_id: int) -> dict:
        """Acknowledge own message after saving a result or verifying a reply."""
        return mailbox().acknowledge(role, message_id)

    if role == "orchestrator":
        @server.tool()
        async def check_worker_inbox(project: str) -> dict:
            """Wake a registered independent worker. Queued or active is not completed."""
            latest = current()
            require_probe(latest, state)
            return await asyncio.to_thread(Runtime(latest, state).wake, project)

        @server.tool()
        async def worker_status(project: str) -> dict:
            """Inspect lifecycle metadata, not project content or result verification."""
            return await asyncio.to_thread(Runtime(current(), state).worker_status, project)

        @server.tool()
        async def create_worktree_worker(project: str, task_id: str, ref: str = "HEAD") -> dict:
            """Create an idempotent detached worktree worker for one authorized task."""
            latest = current()
            require_probe(latest, state)
            return await asyncio.to_thread(
                WorktreeRegistry(latest, state).create, project, task_id, ref
            )

        @server.tool()
        async def list_worktree_workers(project: str | None = None) -> list[dict]:
            """List durable dynamic worktree worker leases."""
            return await asyncio.to_thread(WorktreeRegistry(current(), state).list, project)

    return server


def _build_operator(state, settings):
    """Expose only persistent orchestrator controls to a trusted operator session."""
    server = FastMCP("project-orchestrator-operator")

    def current():
        latest = load(state)
        if latest != settings:
            raise RuntimeError("Settings changed; restart this operator MCP process")
        require_probe(latest, state)
        return latest

    def runtime():
        return OrchestratorRuntime(current(), state)

    @server.tool()
    async def send_orchestrator_prompt(prompt: str) -> dict:
        """Start a new turn on the persistent isolated orchestrator."""
        return await asyncio.to_thread(runtime().prompt, prompt)

    @server.tool()
    async def orchestrator_status() -> dict:
        """Read and refresh the persistent orchestrator lifecycle state."""
        return await asyncio.to_thread(runtime().status)

    @server.tool()
    async def wait_orchestrator(timeout_seconds: float = 110) -> dict:
        """Wait up to 110 seconds for the orchestrator turn to stop being active."""
        if timeout_seconds < 0 or timeout_seconds > 110:
            raise ValueError("timeout_seconds must be between 0 and 110")
        return await asyncio.to_thread(runtime().wait, timeout_seconds)

    @server.tool()
    async def read_orchestrator_result() -> dict:
        """Return the latest completed orchestrator response and status."""
        return await asyncio.to_thread(runtime().result)

    @server.tool()
    async def steer_orchestrator(prompt: str) -> dict:
        """Append operator guidance to the currently active orchestrator turn."""
        return await asyncio.to_thread(runtime().steer, prompt)

    @server.tool()
    async def interrupt_orchestrator() -> dict:
        """Request interruption of the currently active orchestrator turn."""
        return await asyncio.to_thread(runtime().interrupt)

    @server.tool()
    async def acknowledge_orchestrator_reconciliation(note: str) -> dict:
        """Record manual reconciliation and permit a new orchestrator turn."""
        return await asyncio.to_thread(runtime().acknowledge_reconciliation, note)

    return server
