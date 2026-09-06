from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Final, Self

logger = logging.getLogger(__name__)

SCHEMA_VERSION: Final = 1

_SCHEMA: Final = """
CREATE TABLE messages (
    id                  INTEGER PRIMARY KEY,
    mailbox             TEXT    NOT NULL,
    uidvalidity         INTEGER NOT NULL,
    uid                 INTEGER NOT NULL,
    message_id          TEXT,
    telegram_message_id INTEGER,
    status              TEXT    NOT NULL,
    attempts            INTEGER NOT NULL DEFAULT 0,
    error               TEXT,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    UNIQUE (mailbox, uidvalidity, uid)
);
CREATE INDEX messages_by_message_id ON messages (mailbox, message_id);
CREATE INDEX messages_by_status ON messages (status);
"""


class DatabaseError(Exception):
    pass


class Status(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class MessageKey:
    """UIDs are unique only within one mailbox and one UIDVALIDITY."""

    mailbox: str
    uidvalidity: int
    uid: int


@dataclass(frozen=True, slots=True)
class Record:
    key: MessageKey
    message_id: str
    telegram_message_id: int | None
    status: Status
    attempts: int
    error: str
    created_at: str
    updated_at: str


class Database:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._db.close()

    def record(self, key: MessageKey, message_id: str = "") -> Status:
        """Register a fetched message as pending. Returns its current status."""
        existing = self.status_of(key)
        if existing is not None:
            return existing

        now = _now()
        self._write(
            "INSERT INTO messages"
            " (mailbox, uidvalidity, uid, message_id, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (key.mailbox, key.uidvalidity, key.uid, message_id or None, Status.PENDING, now, now),
        )
        return Status.PENDING

    def claim(self, key: MessageKey) -> None:
        """Mark a message as in flight. Must be committed before the send is attempted."""
        self._write(
            "UPDATE messages SET status = ?, attempts = attempts + 1, updated_at = ?"
            " WHERE mailbox = ? AND uidvalidity = ? AND uid = ?",
            (Status.SENDING, _now(), key.mailbox, key.uidvalidity, key.uid),
        )

    def mark_sent(self, key: MessageKey, telegram_message_id: int | None = None) -> None:
        self._write(
            "UPDATE messages SET status = ?, telegram_message_id = ?, error = NULL,"
            " updated_at = ? WHERE mailbox = ? AND uidvalidity = ? AND uid = ?",
            (Status.SENT, telegram_message_id, _now(), key.mailbox, key.uidvalidity, key.uid),
        )

    def mark_failed(self, key: MessageKey, error: str) -> None:
        self._write(
            "UPDATE messages SET status = ?, error = ?, updated_at = ?"
            " WHERE mailbox = ? AND uidvalidity = ? AND uid = ?",
            (Status.FAILED, error[:500], _now(), key.mailbox, key.uidvalidity, key.uid),
        )

    def get(self, key: MessageKey) -> Record | None:
        row = self._db.execute(
            "SELECT message_id, telegram_message_id, status, attempts, error,"
            " created_at, updated_at FROM messages"
            " WHERE mailbox = ? AND uidvalidity = ? AND uid = ?",
            (key.mailbox, key.uidvalidity, key.uid),
        ).fetchone()
        if row is None:
            return None
        message_id, telegram_message_id, status, attempts, error, created, updated = row
        return Record(
            key=key,
            message_id=message_id or "",
            telegram_message_id=telegram_message_id,
            status=Status(status),
            attempts=attempts,
            error=error or "",
            created_at=created,
            updated_at=updated,
        )

    def status_of(self, key: MessageKey) -> Status | None:
        record = self.get(key)
        return record.status if record else None

    def delivered_message_id(self, mailbox: str, message_id: str) -> bool:
        """Has this RFC Message-ID already been delivered under any UIDVALIDITY?"""
        if not message_id:
            return False
        row = self._db.execute(
            "SELECT 1 FROM messages WHERE mailbox = ? AND message_id = ? AND status = ? LIMIT 1",
            (mailbox, message_id, Status.SENT),
        ).fetchone()
        return row is not None

    def resume_uid(self, mailbox: str, uidvalidity: int) -> int | None:
        """Highest UID that can be skipped: everything at or below it is delivered.

        Anything still pending, sending or failed pulls the mark back below itself, so
        an unresolved message is fetched again rather than stranded under the mark.
        """
        unresolved = self._db.execute(
            "SELECT MIN(uid) FROM messages WHERE mailbox = ? AND uidvalidity = ? AND status != ?",
            (mailbox, uidvalidity, Status.SENT),
        ).fetchone()[0]
        if unresolved is not None:
            return int(unresolved) - 1

        highest = self._db.execute(
            "SELECT MAX(uid) FROM messages WHERE mailbox = ? AND uidvalidity = ?",
            (mailbox, uidvalidity),
        ).fetchone()[0]
        return int(highest) if highest is not None else None

    def interrupted(self) -> list[MessageKey]:
        """Messages left mid-flight by a crash: claimed, never resolved."""
        rows = self._db.execute(
            "SELECT mailbox, uidvalidity, uid FROM messages WHERE status = ? ORDER BY id",
            (Status.SENDING,),
        ).fetchall()
        return [MessageKey(mailbox, uidvalidity, uid) for mailbox, uidvalidity, uid in rows]

    def _write(self, statement: str, parameters: tuple[object, ...]) -> None:
        try:
            with self._db:
                self._db.execute(statement, parameters)
        except sqlite3.Error as error:
            raise DatabaseError(f"write failed: {error}") from error


@contextmanager
def connect(path: Path) -> Iterator[Database]:
    database = open_database(path)
    try:
        yield database
    finally:
        database.close()


def open_database(path: Path) -> Database:
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        connection = sqlite3.connect(path, isolation_level="DEFERRED")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        _migrate(connection)
    except sqlite3.Error as error:
        raise DatabaseError(f"cannot open {path}: {error}") from error
    return Database(connection)


def _migrate(connection: sqlite3.Connection) -> None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        raise DatabaseError(
            f"database schema is version {version}, this build understands {SCHEMA_VERSION}"
        )
    if version == SCHEMA_VERSION:
        return

    logger.info("creating schema version %d", SCHEMA_VERSION)
    with connection:
        connection.executescript(_SCHEMA)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
