from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Final, Protocol

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError

from mailbridge.config import Config

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT: Final = 30.0

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

    def logout(self) -> Any: ...


@dataclass(frozen=True, slots=True)
class RawMessage:
    uid: int
    raw: bytes

    @property
    def size(self) -> int:
        return len(self.raw)


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


def fetch_unseen(client: MailboxClient, limit: int | None = None) -> list[RawMessage]:
    """Fetch the unseen messages of the folder already opened by open_folder."""
    try:
        uids = sorted(client.search(["UNSEEN"]))
    except _SERVER_ERRORS as error:
        raise ImapError(f"cannot search for unseen messages: {error}") from error

    logger.info("%d unseen message(s)", len(uids))
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
