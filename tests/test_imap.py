from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from mailbridge.imap import ImapError, fetch_new, idle, open_folder, search_new

BODY_KEY = b"BODY[]"


class FakeClient:
    def __init__(
        self,
        uids: Sequence[int] = (),
        bodies: Mapping[int, bytes] | None = None,
        fails_on: str | None = None,
        uidvalidity: int | None = 42,
        idle_supported: bool = True,
        idle_events: Sequence[object] | None = None,
    ) -> None:
        self._uids = list(uids)
        self._uidvalidity = uidvalidity
        self.idle_supported = idle_supported
        self.idle_events: list[object] = list(idle_events or [])
        self.idle_checks = 0
        self.noops = 0
        self.idling = False
        self.searched: list[Sequence[str]] = []
        self._bodies = dict(bodies or {uid: b"raw-%d" % uid for uid in uids})
        self._fails_on = fails_on
        self.selected: tuple[str, bool] | None = None
        self.fetched: list[int] = []

    def _maybe_fail(self, operation: str) -> None:
        if self._fails_on == operation:
            raise OSError(f"{operation} exploded")

    def select_folder(self, folder: Any, readonly: Any = False) -> Any:
        self._maybe_fail("select_folder")
        self.selected = (str(folder), bool(readonly))
        if self._uidvalidity is None:
            return {b"EXISTS": len(self._uids)}
        return {b"EXISTS": len(self._uids), b"UIDVALIDITY": self._uidvalidity}

    def search(self, criteria: Any = "ALL", charset: Any = None) -> Sequence[int]:
        self._maybe_fail("search")
        self.searched.append(list(criteria))
        if list(criteria) == ["UNSEEN"]:
            return list(self._uids)
        # Mimic the server quirk: "n:*" always yields the highest UID in the folder.
        since = int(str(criteria[1]).split(":")[0])
        matching = [uid for uid in self._uids if uid >= since]
        return matching or self._uids[-1:]

    def fetch(
        self, messages: Any, data: Any, modifiers: Any = None
    ) -> Mapping[int, Mapping[bytes, Any]]:
        self._maybe_fail("fetch")
        assert list(data) == ["BODY.PEEK[]"]
        self.fetched = list(messages)
        return {uid: {BODY_KEY: self._bodies[uid]} for uid in messages if uid in self._bodies}

    def has_capability(self, capability: Any) -> Any:
        return self.idle_supported and str(capability).upper() == "IDLE"

    def idle(self) -> Any:
        self._maybe_fail("idle")
        self.idling = True
        return b"OK"

    def idle_check(self, timeout: Any = None) -> Sequence[Any]:
        self._maybe_fail("idle_check")
        self.idle_checks += 1
        if self.idle_events:
            return [self.idle_events.pop(0)]
        return []

    def idle_done(self) -> Any:
        self.idling = False
        return b"OK"

    def noop(self) -> Any:
        self._maybe_fail("noop")
        self.noops += 1
        return b"OK"

    def logout(self) -> Any:
        return b"BYE"


class TestFetchUnseen:
    def test_returns_a_message_per_uid(self) -> None:
        messages = fetch_new(FakeClient([4, 7]))

        assert [(m.uid, m.raw) for m in messages] == [(4, b"raw-4"), (7, b"raw-7")]

    def test_empty_mailbox_returns_nothing(self) -> None:
        client = FakeClient([])

        assert fetch_new(client) == []
        assert client.fetched == []

    def test_the_folder_is_opened_read_only(self) -> None:
        client = FakeClient([1])

        open_folder(client, "Archive")

        assert client.selected == ("Archive", True)

    def test_uids_are_processed_oldest_first(self) -> None:
        messages = fetch_new(FakeClient([9, 2, 5]))

        assert [m.uid for m in messages] == [2, 5, 9]

    def test_limit_keeps_the_oldest(self) -> None:
        messages = fetch_new(FakeClient([9, 2, 5]), limit=2)

        assert [m.uid for m in messages] == [2, 5]

    def test_limit_above_the_count_changes_nothing(self) -> None:
        messages = fetch_new(FakeClient([1, 2]), limit=50)

        assert len(messages) == 2

    def test_skips_a_uid_that_returns_no_body(self) -> None:
        client = FakeClient([1, 2], bodies={1: b"only-one"})

        messages = fetch_new(client)

        assert [m.uid for m in messages] == [1]

    @pytest.mark.parametrize("operation", ["search", "fetch"])
    def test_server_failure_becomes_an_imap_error(self, operation: str) -> None:
        client = FakeClient([1], fails_on=operation)

        with pytest.raises(ImapError):
            fetch_new(client)


class TestOpenFolder:
    def test_returns_the_uidvalidity(self) -> None:
        assert open_folder(FakeClient([1], uidvalidity=99), "INBOX") == 99

    def test_opens_the_folder_read_only(self) -> None:
        client = FakeClient([1])

        open_folder(client, "Archive")

        assert client.selected == ("Archive", True)

    def test_a_folder_without_uidvalidity_is_an_error(self) -> None:
        with pytest.raises(ImapError, match="UIDVALIDITY"):
            open_folder(FakeClient([1], uidvalidity=None), "INBOX")

    def test_a_server_failure_becomes_an_imap_error(self) -> None:
        with pytest.raises(ImapError, match="cannot open"):
            open_folder(FakeClient([1], fails_on="select_folder"), "INBOX")


class TestSearchNew:
    def test_a_cold_start_looks_only_at_unseen_mail(self) -> None:
        client = FakeClient([3, 4])

        assert search_new(client, None) == [3, 4]
        assert client.searched == [["UNSEEN"]]

    def test_a_warm_start_searches_by_uid_range(self) -> None:
        client = FakeClient([1, 2, 3, 4])

        assert search_new(client, 2) == [3, 4]
        assert client.searched == [["UID", "3:*"]]

    def test_the_highest_uid_quirk_is_filtered_out(self) -> None:
        # "n:*" returns the highest UID even when every message is below n.
        client = FakeClient([1, 2, 3])

        assert search_new(client, 10) == []

    def test_nothing_new_returns_nothing(self) -> None:
        assert search_new(FakeClient([5]), 5) == []

    def test_a_search_failure_becomes_an_imap_error(self) -> None:
        with pytest.raises(ImapError, match="cannot search"):
            search_new(FakeClient([1], fails_on="search"), None)


class TestCatchUp:
    def test_messages_that_arrived_during_downtime_are_fetched(self) -> None:
        # UID 7 was already delivered; 8 and 9 arrived while the daemon was down,
        # and both may well have been read in the webmail already.
        messages = fetch_new(FakeClient([7, 8, 9]), 7)

        assert [m.uid for m in messages] == [8, 9]

    def test_the_limit_still_applies_to_a_backlog(self) -> None:
        messages = fetch_new(FakeClient(list(range(1, 51))), 0, limit=5)

        assert [m.uid for m in messages] == [1, 2, 3, 4, 5]


class TestIdle:
    def test_activity_returns_true(self) -> None:
        client = FakeClient([1], idle_events=["EXISTS"])

        assert idle(client, timeout=10, poll=1) is True

    def test_idle_is_always_ended(self) -> None:
        client = FakeClient([1], idle_events=["EXISTS"])

        idle(client, timeout=10, poll=1)

        assert client.idling is False

    def test_a_quiet_timeout_returns_false(self) -> None:
        clock = iter([0.0, 0.0, 5.0, 11.0])
        client = FakeClient([1])

        assert idle(client, timeout=10, poll=1, monotonic=lambda: next(clock)) is False

    def test_a_shutdown_request_stops_the_wait(self) -> None:
        client = FakeClient([1], idle_events=["EXISTS"])

        assert idle(client, timeout=10, poll=1, should_stop=lambda: True) is False
        assert client.idle_checks == 0

    def test_a_dropped_connection_becomes_an_imap_error(self) -> None:
        client = FakeClient([1], fails_on="idle_check")

        with pytest.raises(ImapError, match="connection lost"):
            idle(client, timeout=10, poll=1)

    def test_a_drop_still_ends_idle(self) -> None:
        client = FakeClient([1], fails_on="idle_check")

        with pytest.raises(ImapError):
            idle(client, timeout=10, poll=1)

        assert client.idling is False

    def test_failing_to_enter_idle_is_an_imap_error(self) -> None:
        with pytest.raises(ImapError, match="cannot enter IDLE"):
            idle(FakeClient([1], fails_on="idle"), timeout=10, poll=1)


class TestIdleFallback:
    def test_a_server_without_idle_polls_instead(self) -> None:
        client = FakeClient([1], idle_supported=False)

        assert idle(client, timeout=0, poll=0) is True
        assert client.noops == 1
        assert client.idle_checks == 0

    def test_a_drop_while_polling_becomes_an_imap_error(self) -> None:
        client = FakeClient([1], idle_supported=False, fails_on="noop")

        with pytest.raises(ImapError, match="connection lost while polling"):
            idle(client, timeout=0, poll=0)
