from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from fastmcp import Client

from codex_project_orchestrator.config import initialize
from codex_project_orchestrator.mcp import build


class McpRoleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.alpha = self.root / "alpha"
        self.beta = self.root / "beta"
        self.alpha.mkdir()
        self.beta.mkdir()
        self.state = self.root / "state"
        initialize(
            self.state,
            self.root / "orchestrator",
            [f"alpha={self.alpha}", f"beta={self.beta}"],
        )

    async def test_role_exposes_only_its_fixed_tool_set(self) -> None:
        async with Client(build(self.state, "orchestrator")) as client:
            orchestrator_tools = {tool.name for tool in await client.list_tools()}
        async with Client(build(self.state, "alpha")) as client:
            worker_tools = {tool.name for tool in await client.list_tools()}
        async with Client(build(self.state, "operator")) as client:
            operator_tools = {tool.name for tool in await client.list_tools()}

        common = {
            "list_workers",
            "send_message",
            "fetch_inbox",
            "acknowledge_message",
        }
        self.assertEqual(
            common
            | {
                "check_worker_inbox",
                "worker_status",
                "create_worktree_worker",
                "list_worktree_workers",
            },
            orchestrator_tools,
        )
        self.assertEqual(common, worker_tools)
        self.assertEqual(
            {
                "send_orchestrator_prompt",
                "orchestrator_status",
                "wait_orchestrator",
                "read_orchestrator_result",
                "steer_orchestrator",
                "interrupt_orchestrator",
                "acknowledge_orchestrator_reconciliation",
            },
            operator_tools,
        )

    async def test_inbox_and_acknowledgement_are_bound_to_server_role(self) -> None:
        async with Client(build(self.state, "orchestrator")) as client:
            sent = await client.call_tool(
                "send_message",
                {
                    "to": "alpha",
                    "subject": "Bounded task",
                    "body": "Do the authorized work",
                    "task_id": "task-alpha-1",
                },
            )
        message_id = sent.data["message_id"]

        async with Client(build(self.state, "beta")) as client:
            beta_inbox = await client.call_tool("fetch_inbox")
            previous_log_threshold = logging.root.manager.disable
            logging.disable(logging.CRITICAL)
            try:
                foreign_ack = await client.call_tool(
                    "acknowledge_message",
                    {"message_id": message_id},
                    raise_on_error=False,
                )
            finally:
                logging.disable(previous_log_threshold)
        self.assertEqual([], beta_inbox.structured_content["result"])
        self.assertTrue(foreign_ack.is_error)

        async with Client(build(self.state, "alpha")) as client:
            alpha_inbox = await client.call_tool("fetch_inbox")
            acknowledged = await client.call_tool(
                "acknowledge_message", {"message_id": message_id}
            )
            after_ack = await client.call_tool("fetch_inbox")

        alpha_messages = alpha_inbox.structured_content["result"]
        self.assertEqual(["task-alpha-1"], [item["task_id"] for item in alpha_messages])
        self.assertEqual(message_id, acknowledged.data["message_id"])
        self.assertIsNotNone(acknowledged.data["acknowledged_at"])
        self.assertEqual([], after_ack.structured_content["result"])


if __name__ == "__main__":
    unittest.main()
