from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import stat
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_project_orchestrator.mailbox import (  # noqa: E402
    InvalidMessageError,
    Mailbox,
    MessageConflictError,
    MessageNotFoundError,
    UnauthorizedError,
)


class MailboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "private" / "mailbox.sqlite3"
        self.mailbox = Mailbox(self.db_path, ["worker-a", "worker-b"])

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def send_task(self, worker: str = "worker-a", task_id: str = "task-1") -> dict[str, object]:
        return self.mailbox.send(
            "orchestrator", worker, "Do work", "Task details", task_id
        )

    def test_routes_messages_only_between_orchestrator_and_workers(self) -> None:
        task = self.send_task()
        reply = self.mailbox.send(
            "worker-a", "orchestrator", "Done", "Result", "task-1"
        )

        self.assertEqual([task], self.mailbox.inbox("worker-a"))
        self.assertEqual([reply], self.mailbox.inbox("orchestrator"))
        with self.assertRaises(UnauthorizedError):
            self.mailbox.send("worker-a", "worker-b", "x", "y", "task-2")
        with self.assertRaises(UnauthorizedError):
            self.mailbox.send("orchestrator", "orchestrator", "x", "y", "task-2")

    def test_unknown_roles_cannot_spoof_sender_or_read_inbox(self) -> None:
        with self.assertRaises(UnauthorizedError):
            self.mailbox.send("attacker", "worker-a", "x", "y", "task-x")
        with self.assertRaises(UnauthorizedError):
            self.mailbox.inbox("attacker")

    def test_role_sees_only_own_unacknowledged_inbox(self) -> None:
        first = self.send_task("worker-a", "task-a")
        second = self.send_task("worker-b", "task-b")

        self.assertEqual([first], self.mailbox.inbox("worker-a"))
        self.assertEqual([second], self.mailbox.inbox("worker-b"))
        self.mailbox.acknowledge("worker-a", first["message_id"])
        self.assertEqual([], self.mailbox.inbox("worker-a"))

    def test_only_recipient_can_acknowledge_and_unknown_message_fails(self) -> None:
        message = self.send_task()
        with self.assertRaises(UnauthorizedError):
            self.mailbox.acknowledge("worker-b", message["message_id"])
        with self.assertRaises(UnauthorizedError):
            self.mailbox.acknowledge("orchestrator", message["message_id"])
        with self.assertRaises(MessageNotFoundError):
            self.mailbox.acknowledge("worker-a", 999_999)

    def test_acknowledgement_is_durable_and_idempotent_after_reopen(self) -> None:
        message = self.send_task()
        acknowledged = self.mailbox.acknowledge("worker-a", message["message_id"])
        reopened = Mailbox(self.db_path, ["worker-a", "worker-b"])

        self.assertIsNotNone(acknowledged["acknowledged_at"])
        self.assertEqual([], reopened.inbox("worker-a"))
        self.assertEqual(
            acknowledged,
            reopened.acknowledge("worker-a", message["message_id"]),
        )

    def test_identical_send_is_idempotent_but_changed_payload_conflicts(self) -> None:
        first = self.send_task()
        duplicate = self.send_task()
        self.assertEqual(first, duplicate)
        self.assertEqual(1, len(self.mailbox.inbox("worker-a")))

        with self.assertRaises(MessageConflictError):
            self.mailbox.send(
                "orchestrator", "worker-a", "Changed", "Task details", "task-1"
            )
        with self.assertRaises(MessageConflictError):
            self.mailbox.send(
                "orchestrator", "worker-a", "Do work", "Changed", "task-1"
            )

    def test_worker_reply_requires_correlated_task_for_same_worker(self) -> None:
        self.send_task("worker-a", "task-a")
        with self.assertRaises(InvalidMessageError):
            self.mailbox.send(
                "worker-b", "orchestrator", "Done", "Result", "task-a"
            )
        with self.assertRaises(InvalidMessageError):
            self.mailbox.send(
                "worker-a", "orchestrator", "Done", "Result", "unknown"
            )

        reply = self.mailbox.send(
            "worker-a", "orchestrator", "Done", "Result", "task-a"
        )
        self.assertEqual("task-a", reply["task_id"])

    def test_validates_message_fields_and_limits(self) -> None:
        invalid = [
            ("", "body", "task"),
            ("subject", "  ", "task"),
            ("subject", "body", ""),
            ("subject", "body", "x" * 129),
        ]
        for subject, body, task_id in invalid:
            with self.subTest(subject=subject, body=body, task_id=task_id):
                with self.assertRaises(InvalidMessageError):
                    self.mailbox.send(
                        "orchestrator", "worker-a", subject, body, task_id
                    )

        with self.assertRaises(InvalidMessageError):
            self.mailbox.send(
                "orchestrator", "worker-a", "s" * 4097, "body", "task"
            )
        with self.assertRaises(InvalidMessageError):
            self.mailbox.send(
                "orchestrator", "worker-a", "subject", "b" * (1024 * 1024 + 1), "task"
            )

    def test_database_and_new_parent_have_private_permissions(self) -> None:
        self.assertEqual(0o700, stat.S_IMODE(self.db_path.parent.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(self.db_path.stat().st_mode))

    def test_existing_parent_permissions_are_not_changed(self) -> None:
        existing_parent = Path(self.temp_dir.name) / "shared"
        existing_parent.mkdir(mode=0o755)
        existing_parent.chmod(0o755)

        mailbox_path = existing_parent / "mailbox.sqlite3"
        Mailbox(mailbox_path, ["worker-a"])

        self.assertEqual(0o755, stat.S_IMODE(existing_parent.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(mailbox_path.stat().st_mode))

    def test_concurrent_identical_sends_create_one_message(self) -> None:
        def send(_: int) -> dict[str, object]:
            mailbox = Mailbox(self.db_path, ["worker-a", "worker-b"])
            return mailbox.send(
                "orchestrator", "worker-a", "Concurrent", "Same body", "shared-task"
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(send, range(24)))

        self.assertEqual(1, len({result["message_id"] for result in results}))
        shared = [
            message
            for message in self.mailbox.inbox("worker-a")
            if message["task_id"] == "shared-task"
        ]
        self.assertEqual(1, len(shared))

    def test_concurrent_distinct_sends_are_all_durable_and_ordered(self) -> None:
        def send(number: int) -> dict[str, object]:
            mailbox = Mailbox(self.db_path, ["worker-a", "worker-b"])
            return mailbox.send(
                "orchestrator",
                "worker-b",
                f"Task {number}",
                "body",
                f"concurrent-{number}",
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(send, range(20)))

        messages = self.mailbox.inbox("worker-b")
        self.assertEqual(20, len(messages))
        ids = [message["message_id"] for message in messages]
        self.assertEqual(sorted(ids), ids)


if __name__ == "__main__":
    unittest.main()
