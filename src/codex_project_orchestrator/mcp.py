"""Fixed-identity adapter. Tool callers cannot choose sender or mailbox owner."""
import asyncio
from pathlib import Path

from fastmcp import FastMCP

from .config import load, require_probe
from .mailbox import Mailbox
from .runtime import Runtime


def build(state, role):
    state = Path(state)
    settings = load(state)
    if settings["mode"] != "isolated":
        raise ValueError("Role-bound MCP is disabled in local mode")
    if role not in ("orchestrator", *settings["workers"]):
        raise ValueError("Unknown fixed identity")
    mailbox = Mailbox(state / "mail.sqlite3", settings["workers"])
    server = FastMCP("project-agents-" + role)

    def current():
        latest = load(state)
        if latest != settings:
            raise RuntimeError("Settings changed; restart this session and MCP process")
        return latest

    @server.tool()
    def list_workers() -> dict:
        """Return this fixed identity and registered worker IDs."""
        current()
        return {"identity": role, "workers": list(settings["workers"])}

    @server.tool()
    def send_message(to: str, subject: str, body: str, task_id: str) -> dict:
        """Queue authorized task or final result. Sending does not start a worker.
        Include scope, allowed actions, evidence and completion criteria in tasks.
        Replies use the same task_id. Identical resends deduplicate; changes fail.
        """
        current()
        return mailbox.send(role, to, subject, body, task_id)

    @server.tool()
    def fetch_inbox() -> list[dict]:
        """Read this identity's unacknowledged messages only."""
        current()
        return mailbox.inbox(role)

    @server.tool()
    def acknowledge_message(message_id: int) -> dict:
        """Acknowledge own message after saving a result or verifying a reply."""
        current()
        return mailbox.acknowledge(role, message_id)

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

    return server
