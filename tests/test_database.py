from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from mailbridge.database import (
    SCHEMA_VERSION,
    Database,
    DatabaseError,
    MessageKey,
    Record,
    Status,
    connect,
    open_database,
)


def _get(db: Database, key: MessageKey) -> Record:
    record = db.get(key)
    assert record is not None
    return record


MAILBOX = "user@mail.ru"
KEY = MessageKey(MAILBOX, 42, 1)
OTHER = MessageKey(MAILBOX, 42, 2)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    with connect(tmp_path / "state.db") as database:
        yield database


class TestOpenDatabase:
    def test_creates_the_file_and_parent_directory(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "deeper" / "state.db"

        with connect(path):
            pass

        assert path.exists()

    def test_stamps_the_schema_version(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"
        with connect(path):
            pass

        connection = sqlite3.connect(path)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        connection.close()

    def test_enables_write_ahead_logging(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"
        with connect(path):
            pass

        connection = sqlite3.connect(path)
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        connection.close()

    def test_reopening_preserves_rows(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"
        with connect(path) as first:
            first.record(KEY)
            first.mark_sent(KEY, 7)

        with connect(path) as second:
            assert second.status_of(KEY) is Status.SENT

    def test_a_newer_schema_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"
        connection = sqlite3.connect(path)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        connection.close()

        with pytest.raises(DatabaseError, match="schema"):
            open_database(path)


class TestRecord:
    def test_an_unknown_message_becomes_pending(self, db: Database) -> None:
        assert db.record(KEY) is Status.PENDING
        assert db.status_of(KEY) is Status.PENDING

    def test_recording_twice_keeps_the_first_status(self, db: Database) -> None:
        db.record(KEY)
        db.mark_sent(KEY, 7)

        assert db.record(KEY) is Status.SENT

    def test_an_unknown_message_has_no_status(self, db: Database) -> None:
        assert db.status_of(KEY) is None

    def test_the_same_uid_under_a_new_uidvalidity_is_a_new_message(self, db: Database) -> None:
        db.record(KEY)
        db.mark_sent(KEY, 7)

        renumbered = MessageKey(MAILBOX, 99, KEY.uid)
        assert db.record(renumbered) is Status.PENDING

    def test_the_same_uid_in_another_mailbox_is_a_new_message(self, db: Database) -> None:
        db.record(KEY)
        db.mark_sent(KEY, 7)

        assert db.record(MessageKey("other@mail.ru", 42, KEY.uid)) is Status.PENDING


class TestDeliveryStates:
    def test_claim_moves_pending_to_sending(self, db: Database) -> None:
        db.record(KEY)
        db.claim(KEY)

        assert db.status_of(KEY) is Status.SENDING

    def test_mark_sent_records_the_telegram_id(self, db: Database) -> None:
        db.record(KEY)
        db.claim(KEY)
        db.mark_sent(KEY, 555)

        assert db.status_of(KEY) is Status.SENT
        assert _get(db, KEY).telegram_message_id == 555

    def test_mark_failed_records_the_reason(self, db: Database) -> None:
        db.record(KEY)
        db.claim(KEY)
        db.mark_failed(KEY, "chat not found")

        assert db.status_of(KEY) is Status.FAILED
        assert _get(db, KEY).error == "chat not found"

    def test_a_long_error_is_truncated(self, db: Database) -> None:
        db.record(KEY)
        db.mark_failed(KEY, "x" * 5000)

        assert len(_get(db, KEY).error) == 500

    def test_each_claim_counts_an_attempt(self, db: Database) -> None:
        db.record(KEY)
        db.claim(KEY)
        db.claim(KEY)

        assert _get(db, KEY).attempts == 2

    def test_a_retry_clears_the_previous_error(self, db: Database) -> None:
        db.record(KEY)
        db.mark_failed(KEY, "temporary")
        db.claim(KEY)
        db.mark_sent(KEY, 1)

        assert _get(db, KEY).error == ""


class TestMessageIdDeduplication:
    def test_a_delivered_message_id_is_recognised(self, db: Database) -> None:
        db.record(KEY, "<abc@example.com>")
        db.mark_sent(KEY, 1)

        assert db.delivered_message_id(MAILBOX, "<abc@example.com>")

    def test_a_pending_message_id_is_not_delivered(self, db: Database) -> None:
        db.record(KEY, "<abc@example.com>")

        assert not db.delivered_message_id(MAILBOX, "<abc@example.com>")

    def test_an_unknown_message_id_is_not_delivered(self, db: Database) -> None:
        assert not db.delivered_message_id(MAILBOX, "<never-seen@example.com>")

    def test_an_empty_message_id_never_matches(self, db: Database) -> None:
        db.record(KEY, "")
        db.mark_sent(KEY, 1)

        assert not db.delivered_message_id(MAILBOX, "")

    def test_another_mailbox_does_not_match(self, db: Database) -> None:
        db.record(KEY, "<abc@example.com>")
        db.mark_sent(KEY, 1)

        assert not db.delivered_message_id("other@mail.ru", "<abc@example.com>")


class TestInterrupted:
    def test_a_clean_database_has_nothing_interrupted(self, db: Database) -> None:
        assert db.interrupted() == []

    def test_a_claimed_but_unresolved_message_is_interrupted(self, db: Database) -> None:
        db.record(KEY)
        db.claim(KEY)

        assert db.interrupted() == [KEY]

    def test_resolved_messages_are_not_interrupted(self, db: Database) -> None:
        db.record(KEY)
        db.claim(KEY)
        db.mark_sent(KEY, 1)
        db.record(OTHER)
        db.claim(OTHER)
        db.mark_failed(OTHER, "nope")

        assert db.interrupted() == []


class TestCrashSafety:
    """The roadmap's two crash windows, replayed across a real reopen."""

    def test_a_crash_between_claim_and_send_leaves_a_retryable_row(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"
        with connect(path) as first:
            first.record(KEY, "<abc@example.com>")
            first.claim(KEY)  # process dies here, before the API call

        with connect(path) as second:
            assert second.interrupted() == [KEY]
            assert second.status_of(KEY) is Status.SENDING
            assert not second.delivered_message_id(MAILBOX, "<abc@example.com>")

    def test_a_crash_between_send_and_commit_retries_rather_than_loses(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "state.db"
        with connect(path) as first:
            first.record(KEY, "<abc@example.com>")
            first.claim(KEY)  # Telegram accepted it, but the process died before mark_sent

        with connect(path) as second:
            # At-least-once: the message is offered again rather than dropped.
            assert second.status_of(KEY) is not Status.SENT
            second.claim(KEY)
            second.mark_sent(KEY, 2)

        with connect(path) as third:
            assert third.status_of(KEY) is Status.SENT
            assert _get(third, KEY).attempts == 2

    def test_a_delivered_message_is_never_offered_again(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"
        with connect(path) as first:
            first.record(KEY)
            first.claim(KEY)
            first.mark_sent(KEY, 1)

        with connect(path) as second:
            assert second.record(KEY) is Status.SENT
            assert second.interrupted() == []
