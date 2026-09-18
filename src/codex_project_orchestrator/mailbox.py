"""Durable, role-scoped message passing backed by SQLite.

The mailbox stores delivery and acknowledgement state.  It deliberately makes
no claim about exactly-once processing by a recipient.
"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import os
import sqlite3
from typing import Iterator


ORCHESTRATOR = "orchestrator"
MAX_TASK_ID_LENGTH = 128
MAX_SUBJECT_BYTES = 4 * 1024
MAX_BODY_BYTES = 1024 * 1024
_BUSY_TIMEOUT_MS = 5_000


class MailboxError(Exception):
    """Base class for mailbox-specific failures."""


class InvalidMessageError(MailboxError, ValueError):
    """Raised when a message field is invalid."""


class UnauthorizedError(MailboxError, PermissionError):
    """Raised when a role attempts an operation it is not allowed to do."""


class MessageConflictError(MailboxError, ValueError):
    """Raised when a task key is reused with different message content."""


class MessageNotFoundError(MailboxError, LookupError):
    """Raised when an acknowledgement refers to an unknown message."""


class Mailbox:
    """A durable mailbox shared by one orchestrator and a fixed worker set."""

    def __init__(self, path: Path, workers: Iterable[str]) -> None:
        self.path = Path(path)
        if isinstance(workers, (str, bytes)):
            raise ValueError("workers must be an iterable of worker IDs")

        worker_set = frozenset(workers)
        if not worker_set:
            raise ValueError("at least one worker is required")
        for worker in worker_set:
            if not isinstance(worker, str) or not worker.strip():
                raise ValueError("worker IDs must be nonempty strings")
            if worker != worker.strip():
                raise ValueError("worker IDs may not have surrounding whitespace")
            if worker == ORCHESTRATOR:
                raise ValueError("'orchestrator' is a reserved role")

        self.workers = worker_set
        self._roles = worker_set | {ORCHESTRATOR}
        self._prepare_parent()
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    acknowledged_at TEXT,
                    UNIQUE (sender, recipient, task_id)
                );
                CREATE INDEX IF NOT EXISTS messages_inbox_idx
                    ON messages (recipient, acknowledged_at, id);
                """
            )
        self._protect_database_file()

    def send(
        self,
        sender: str,
        to: str,
        subject: str,
        body: str,
        task_id: str,
    ) -> dict[str, object]:
        """Send a message, returning the stored message.

        Repeating the same sender/recipient/task ID and payload returns the
        original message.  Reusing that key with changed content is rejected.
        """

        self._validate_route(sender, to)
        subject = self._validate_text("subject", subject, MAX_SUBJECT_BYTES)
        body = self._validate_text("body", body, MAX_BODY_BYTES)
        task_id = self._validate_task_id(task_id)
        created_at = self._now()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if sender != ORCHESTRATOR:
                parent = connection.execute(
                    """
                    SELECT 1 FROM messages
                    WHERE sender = ? AND recipient = ? AND task_id = ?
                    """,
                    (ORCHESTRATOR, sender, task_id),
                ).fetchone()
                if parent is None:
                    raise InvalidMessageError(
                        "a worker reply must reference an existing task sent "
                        "to that worker by the orchestrator"
                    )

            existing = connection.execute(
                """
                SELECT id, sender, recipient, subject, body, task_id,
                       created_at, acknowledged_at
                FROM messages
                WHERE sender = ? AND recipient = ? AND task_id = ?
                """,
                (sender, to, task_id),
            ).fetchone()
            if existing is not None:
                if existing[3] != subject or existing[4] != body:
                    raise MessageConflictError(
                        "sender, recipient, and task_id already identify a "
                        "message with different content"
                    )
                return self._as_dict(existing)

            cursor = connection.execute(
                """
                INSERT INTO messages
                    (sender, recipient, subject, body, task_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (sender, to, subject, body, task_id, created_at),
            )
            row = connection.execute(
                """
                SELECT id, sender, recipient, subject, body, task_id,
                       created_at, acknowledged_at
                FROM messages WHERE id = ?
                """,
                (cursor.lastrowid,),
            ).fetchone()
            assert row is not None
            return self._as_dict(row)

    def inbox(self, role: str) -> list[dict[str, object]]:
        """Return a role's unacknowledged messages in delivery order."""

        self._validate_role(role)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, sender, recipient, subject, body, task_id,
                       created_at, acknowledged_at
                FROM messages
                WHERE recipient = ? AND acknowledged_at IS NULL
                ORDER BY id ASC
                """,
                (role,),
            ).fetchall()
        return [self._as_dict(row) for row in rows]

    def acknowledge(self, role: str, message_id: int) -> dict[str, object]:
        """Acknowledge a message as its recipient and return its new state."""

        self._validate_role(role)
        if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
            raise ValueError("message_id must be a positive integer")

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT id, sender, recipient, subject, body, task_id,
                       created_at, acknowledged_at
                FROM messages WHERE id = ?
                """,
                (message_id,),
            ).fetchone()
            if row is None:
                raise MessageNotFoundError(f"message {message_id} does not exist")
            if row[2] != role:
                raise UnauthorizedError("only the message recipient may acknowledge it")

            if row[7] is None:
                acknowledged_at = self._now()
                connection.execute(
                    "UPDATE messages SET acknowledged_at = ? WHERE id = ?",
                    (acknowledged_at, message_id),
                )
                row = (*row[:7], acknowledged_at)
            return self._as_dict(row)

    def _prepare_parent(self) -> None:
        parent = self.path.parent
        if parent.exists():
            if not parent.is_dir():
                raise ValueError(f"mailbox parent is not a directory: {parent}")
            return
        parent.mkdir(parents=True, mode=0o700)

    def _protect_database_file(self) -> None:
        # The DB itself belongs to the mailbox.  Existing parent directories may
        # be shared or user-managed, so their modes are intentionally untouched.
        os.chmod(self.path, 0o600)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.path,
            timeout=_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        try:
            connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.row_factory = sqlite3.Row
            with connection:
                yield connection
        finally:
            connection.close()

    def _validate_role(self, role: str) -> None:
        if not isinstance(role, str) or role not in self._roles:
            raise UnauthorizedError(f"unknown mailbox role: {role!r}")

    def _validate_route(self, sender: str, recipient: str) -> None:
        self._validate_role(sender)
        self._validate_role(recipient)
        if sender == ORCHESTRATOR:
            if recipient not in self.workers:
                raise UnauthorizedError("the orchestrator may send only to workers")
        elif recipient != ORCHESTRATOR:
            raise UnauthorizedError("workers may send only to the orchestrator")

    @staticmethod
    def _validate_text(name: str, value: str, byte_limit: int) -> str:
        if not isinstance(value, str) or not value.strip():
            raise InvalidMessageError(f"{name} must be a nonempty string")
        if len(value.encode("utf-8")) > byte_limit:
            raise InvalidMessageError(f"{name} exceeds the {byte_limit}-byte limit")
        return value

    @staticmethod
    def _validate_task_id(task_id: str) -> str:
        if not isinstance(task_id, str) or not task_id.strip():
            raise InvalidMessageError("task_id must be a nonempty string")
        if task_id != task_id.strip():
            raise InvalidMessageError("task_id may not have surrounding whitespace")
        if len(task_id) > MAX_TASK_ID_LENGTH:
            raise InvalidMessageError(
                f"task_id exceeds the {MAX_TASK_ID_LENGTH}-character limit"
            )
        return task_id

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")

    @staticmethod
    def _as_dict(row: sqlite3.Row | tuple[object, ...]) -> dict[str, object]:
        return {
            "message_id": row[0],
            "sender": row[1],
            "recipient": row[2],
            "to": row[2],
            "subject": row[3],
            "body": row[4],
            "task_id": row[5],
            "created_at": row[6],
            "acknowledged_at": row[7],
        }
