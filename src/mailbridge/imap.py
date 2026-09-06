from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Final, Protocol

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError

from mailbridge.config import Config

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT: Final = 30.0

# RFC 2177 requires re-issuing IDLE at least every 29 minutes; servers drop it sooner.
IDLE_TIMEOUT: Final = 29 * 60.0
# Short inner polls keep shutdown responsive while IDLE is outstanding.
IDLE_POLL: Final = 30.0

# PEEK plus a read-only folder selection: a pass never mutates the mailbox,
# so \Seen flags stay meaningful to whoever also reads this account.
_BODY_PEEK: Final = "BODY.PEEK[]"
_BODY_KEY: Final = b"BODY[]"


# A dropped socket surfaces as OSError, not as an imapclient error.
_SERVER_ERRORS: Final = (IMAPClientError, OSError)


class ImapError(Exception):
    pass


class MailboxClient(Protocol):
    """The slice of IMAPClient this package uses. Signatures mirror the library."""

    def select_folder(self, folder: Any, readonly: Any = False) -> Any: ...

    def search(self, criteria: Any = "ALL", charset: Any = None) -> Sequence[int]: ...

    def fetch(
        self, messages: Any, data: Any, modifiers: Any = None
    ) -> Mapping[int, Mapping[bytes, Any]]: ...

    def has_capability(self, capability: Any) -> Any: ...

    def idle(self) -> Any: ...

    def idle_check(self, timeout: Any = None) -> Sequence[Any]: ...

    def idle_done(self) -> Any: ...

    def noop(self) -> Any: ...

    def logout(self) -> Any: ...


@dataclass(frozen=True, slots=True)
class RawMessage:
    uid: int
    raw: bytes


@contextmanager
def connect(config: Config) -> Iterator[MailboxClient]:
    logger.info(
        "connecting to %s:%d as %s", config.mail_host, config.mail_port, config.mail_username
    )
    try:
        client = IMAPClient(
            config.mail_host, port=config.mail_port, ssl=True, timeout=CONNECT_TIMEOUT
        )
    except OSError as error:
        raise ImapError(f"cannot reach {config.mail_host}:{config.mail_port}: {error}") from error

    try:
        try:
            client.login(config.mail_username, config.mail_password.reveal())
        except _SERVER_ERRORS as error:
            raise ImapError(f"login failed for {config.mail_username}: {error}") from error
        logger.info("logged in")
        yield client
    finally:
        _logout(client)


def open_folder(client: MailboxClient, folder: str) -> int:
    """Select the folder read-only and return its UIDVALIDITY.

    UIDs are only meaningful within one UIDVALIDITY: when the server changes it,
    every UID in the folder is renumbered and prior state no longer applies.
    """
    try:
        response = client.select_folder(folder, readonly=True)
    except _SERVER_ERRORS as error:
        raise ImapError(f"cannot open folder {folder}: {error}") from error

    uidvalidity = response.get(b"UIDVALIDITY") if isinstance(response, Mapping) else None
    if not isinstance(uidvalidity, int):
        raise ImapError(f"folder {folder} reported no UIDVALIDITY")
    logger.info("opened %s (uidvalidity %d)", folder, uidvalidity)
    return uidvalidity


def search_new(client: MailboxClient, since_uid: int | None = None) -> list[int]:
    """UIDs worth fetching: everything above since_uid, or the unseen ones on a cold start.

    Searching by UID rather than by \\Seen is what covers downtime — a message that
    someone else read in the webmail while the daemon was down is still forwarded.
    """
    criteria = ["UNSEEN"] if since_uid is None else ["UID", f"{since_uid + 1}:*"]
    try:
        uids = sorted(client.search(criteria))
    except _SERVER_ERRORS as error:
        raise ImapError(f"cannot search the mailbox: {error}") from error

    if since_uid is not None:
        # "n:*" always returns the highest UID in the folder, even when it is below n.
        uids = [uid for uid in uids if uid > since_uid]
    return uids


def fetch_new(
    client: MailboxClient, since_uid: int | None = None, limit: int | None = None
) -> list[RawMessage]:
    """Fetch messages from the folder already opened by open_folder."""
    uids = search_new(client, since_uid)
    logger.info("%d message(s) to consider", len(uids))
    if limit is not None and len(uids) > limit:
        logger.warning("limiting this pass to the %d oldest of %d unseen", limit, len(uids))
        uids = uids[:limit]
    if not uids:
        return []

    try:
        response = client.fetch(uids, [_BODY_PEEK])
    except _SERVER_ERRORS as error:
        raise ImapError(f"cannot fetch {len(uids)} message(s): {error}") from error

    messages: list[RawMessage] = []
    for uid in uids:
        raw = response.get(uid, {}).get(_BODY_KEY)
        if not isinstance(raw, bytes):
            logger.warning("uid %d returned no body, skipping", uid)
            continue
        logger.info("fetched uid %d (%d bytes)", uid, len(raw))
        messages.append(RawMessage(uid=uid, raw=raw))
    return messages


def _logout(client: MailboxClient) -> None:
    try:
        client.logout()
    except Exception as error:
        logger.debug("logout failed, dropping the connection anyway: %s", error)


def idle(
    client: MailboxClient,
    *,
    timeout: float = IDLE_TIMEOUT,
    poll: float = IDLE_POLL,
    should_stop: Callable[[], bool] = lambda: False,
    monotonic: Callable[[], float] = time.monotonic,
) -> bool:
    """Wait until there is a reason to re-check the mailbox.

    True means the server reported activity, False that the wait ended on its own.
    Both leave the caller in the same place: fetch again. The polling fallback has
    no way to tell the difference and always reports True.
    """
    if not _supports_idle(client):
        return _poll_instead(
            client, timeout=timeout, poll=poll, should_stop=should_stop, monotonic=monotonic
        )

    try:
        client.idle()
    except _SERVER_ERRORS as error:
        raise ImapError(f"cannot enter IDLE: {error}") from error

    deadline = monotonic() + timeout
    try:
        while not should_stop():
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            responses = client.idle_check(timeout=min(poll, remaining))
            if responses:
                logger.debug("idle reported %d response(s)", len(responses))
                return True
        return False
    except _SERVER_ERRORS as error:
        raise ImapError(f"connection lost while idling: {error}") from error
    finally:
        _end_idle(client)


def _supports_idle(client: MailboxClient) -> bool:
    try:
        return bool(client.has_capability("IDLE"))
    except _SERVER_ERRORS as error:
        raise ImapError(f"cannot read server capabilities: {error}") from error


def _poll_instead(
    client: MailboxClient,
    *,
    timeout: float,
    poll: float,
    should_stop: Callable[[], bool],
    monotonic: Callable[[], float],
) -> bool:
    """Fallback for a server without IDLE: keep the connection warm and come back."""
    logger.debug("server does not advertise IDLE, polling instead")
    deadline = monotonic() + min(timeout, poll)
    while not should_stop() and monotonic() < deadline:
        time.sleep(min(poll, max(0.0, deadline - monotonic())))
    try:
        client.noop()
    except _SERVER_ERRORS as error:
        raise ImapError(f"connection lost while polling: {error}") from error
    return True


def _end_idle(client: MailboxClient) -> None:
    try:
        client.idle_done()
    except Exception as error:
        logger.debug("could not end IDLE cleanly: %s", error)
