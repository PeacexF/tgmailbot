from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from mailbridge.imap import ImapError, RawMessage, fetch_unseen

BODY_KEY = b"BODY[]"


class FakeClient:
    def __init__(
        self,
        uids: Sequence[int] = (),
        bodies: Mapping[int, bytes] | None = None,
        fails_on: str | None = None,
    ) -> None:
        self._uids = list(uids)
        self._bodies = dict(bodies or {uid: b"raw-%d" % uid for uid in uids})
        self._fails_on = fails_on
        self.selected: tuple[str, bool] | None = None
        self.fetched: list[int] = []

    def _maybe_fail(self, operation: str) -> None:
        if self._fails_on == operation:
            raise OSError(f"{operation} exploded")

    def select_folder(self, folder: str, readonly: bool = False) -> Any:
        self._maybe_fail("select_folder")
        self.selected = (folder, readonly)
        return {b"EXISTS": len(self._uids)}

    def search(self, criteria: Sequence[str]) -> Sequence[int]:
        self._maybe_fail("search")
        assert list(criteria) == ["UNSEEN"]
        return list(self._uids)

    def fetch(
        self, messages: Sequence[int], data: Sequence[str]
    ) -> Mapping[int, Mapping[bytes, Any]]:
        self._maybe_fail("fetch")
        assert list(data) == ["BODY.PEEK[]"]
        self.fetched = list(messages)
        return {uid: {BODY_KEY: self._bodies[uid]} for uid in messages if uid in self._bodies}

    def logout(self) -> Any:
        return b"BYE"


class TestRawMessage:
    def test_size_is_the_byte_length(self) -> None:
        assert RawMessage(uid=1, raw=b"12345").size == 5


class TestFetchUnseen:
    def test_returns_a_message_per_uid(self) -> None:
        messages = fetch_unseen(FakeClient([4, 7]), "INBOX")

        assert [(m.uid, m.raw) for m in messages] == [(4, b"raw-4"), (7, b"raw-7")]

    def test_empty_mailbox_returns_nothing(self) -> None:
        client = FakeClient([])

        assert fetch_unseen(client, "INBOX") == []
        assert client.fetched == []

    def test_selects_the_folder_read_only(self) -> None:
        client = FakeClient([1])

        fetch_unseen(client, "Archive")

        assert client.selected == ("Archive", True)

    def test_uids_are_processed_oldest_first(self) -> None:
        messages = fetch_unseen(FakeClient([9, 2, 5]), "INBOX")

        assert [m.uid for m in messages] == [2, 5, 9]

    def test_limit_keeps_the_oldest(self) -> None:
        messages = fetch_unseen(FakeClient([9, 2, 5]), "INBOX", limit=2)

        assert [m.uid for m in messages] == [2, 5]

    def test_limit_above_the_count_changes_nothing(self) -> None:
        messages = fetch_unseen(FakeClient([1, 2]), "INBOX", limit=50)

        assert len(messages) == 2

    def test_skips_a_uid_that_returns_no_body(self) -> None:
        client = FakeClient([1, 2], bodies={1: b"only-one"})

        messages = fetch_unseen(client, "INBOX")

        assert [m.uid for m in messages] == [1]

    @pytest.mark.parametrize("operation", ["select_folder", "search", "fetch"])
    def test_server_failure_becomes_an_imap_error(self, operation: str) -> None:
        client = FakeClient([1], fails_on=operation)

        with pytest.raises(ImapError):
            fetch_unseen(client, "INBOX")
