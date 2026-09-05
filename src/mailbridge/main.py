from __future__ import annotations

import argparse
import logging
import random
import signal
import threading
import time
from collections.abc import Callable, Sequence
from types import FrameType
from typing import Final

from mailbridge import __version__, database, imap
from mailbridge.config import Config, ConfigError, load_config
from mailbridge.database import Database, DatabaseError, MessageKey, Status
from mailbridge.imap import ImapError, MailboxClient
from mailbridge.log import setup_logging
from mailbridge.parser import Attachment, Email, parse
from mailbridge.telegram import (
    MAX_UPLOAD_BYTES,
    TelegramClient,
    TelegramError,
    escape,
    format_email,
    format_size,
)

logger = logging.getLogger("mailbridge")

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG_ERROR = 2

DEFAULT_LIMIT = 10

RECONNECT_BASE_DELAY: Final = 2.0
RECONNECT_MAX_DELAY: Final = 300.0
HEARTBEAT_INTERVAL: Final = 15 * 60.0


class Shutdown:
    """Turns SIGINT/SIGTERM into a flag the loops can check between messages."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def install(self) -> None:
        for received in (signal.SIGINT, signal.SIGTERM):
            signal.signal(received, self._handle)

    def _handle(self, signum: int, frame: FrameType | None) -> None:  # noqa: ARG002
        logger.info("received %s, finishing the current message", signal.Signals(signum).name)
        self._event.set()

    def requested(self) -> bool:
        """A live check: a signal can flip this between any two statements."""
        return self._event.is_set()

    def request(self) -> None:
        self._event.set()

    def wait(self, seconds: float) -> bool:
        """Sleep unless shutdown arrives first. True means shutdown was requested."""
        return self._event.wait(seconds)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mailbridge",
        description="Forward incoming Mail.ru email to a Telegram group.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the configuration and exit without connecting to anything",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="make a single pass and exit instead of running continuously",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch from the mailbox but send nothing to Telegram",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        metavar="N",
        help=f"forward at most N messages per pass (default: {DEFAULT_LIMIT})",
    )
    parser.add_argument("--version", action="version", version=f"mailbridge {__version__}")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)

    # Bootstrap logging so configuration failures use the same channel.
    setup_logging()

    try:
        config = load_config()
    except ConfigError as error:
        logger.error("configuration is invalid:")
        for problem in error.problems:
            logger.error("  - %s", problem)
        logger.error("see .env.example for the expected variables")
        return EXIT_CONFIG_ERROR

    setup_logging(config.log_level, config.secrets())
    logger.info("mailbridge %s starting", __version__)
    logger.info("configuration: %s", config.summary())

    if args.check:
        logger.info("configuration OK")
        return EXIT_OK

    if args.once or args.dry_run:
        return run_once(config, limit=args.limit, dry_run=args.dry_run)
    return run_forever(config, limit=args.limit)


def run_once(config: Config, *, limit: int, dry_run: bool = False) -> int:
    """One pass over the mailbox, then exit."""
    try:
        with database.connect(config.database_path) as db:
            _report_interrupted(db)
            try:
                with imap.connect(config) as client:
                    uidvalidity = imap.open_folder(client, config.mail_folder)
                    messages = _fetch(db, config, client, uidvalidity, limit)
            except ImapError as error:
                logger.error("mailbox unavailable: %s", error)
                return EXIT_FAILURE

            if dry_run:
                logger.info("dry run: %d message(s) fetched, none sent", len(messages))
                return EXIT_OK
            if not messages:
                logger.info("nothing to forward")
                return EXIT_OK

            pending = _select(db, config, uidvalidity, messages)
            if not pending:
                logger.info("nothing new to forward")
                return EXIT_OK

            with TelegramClient(config.telegram_bot_token, config.telegram_chat_id) as telegram:
                failures = _deliver(db, telegram, pending)
            return EXIT_FAILURE if failures else EXIT_OK
    except DatabaseError as error:
        logger.error("state unavailable: %s", error)
        return EXIT_FAILURE


def run_forever(config: Config, *, limit: int, shutdown: Shutdown | None = None) -> int:
    """Watch the mailbox until asked to stop, reconnecting through any failure."""
    stop = shutdown or Shutdown()
    if shutdown is None:
        stop.install()

    try:
        with (
            database.connect(config.database_path) as db,
            TelegramClient(config.telegram_bot_token, config.telegram_chat_id) as telegram,
        ):
            _report_interrupted(db)
            _supervise(config, db, telegram, limit=limit, stop=stop)
    except DatabaseError as error:
        logger.error("state unavailable: %s", error)
        return EXIT_FAILURE

    logger.info("stopped")
    return EXIT_OK


def _supervise(
    config: Config, db: Database, telegram: TelegramClient, *, limit: int, stop: Shutdown
) -> None:
    attempt = 0
    heartbeat = _Heartbeat()
    while not stop.requested():
        try:
            with imap.connect(config) as client:
                attempt = 0
                uidvalidity = imap.open_folder(client, config.mail_folder)
                _watch(config, db, telegram, client, uidvalidity, limit, stop, heartbeat)
        except ImapError as error:
            if stop.requested():
                break
            attempt += 1
            delay = _reconnect_delay(attempt)
            logger.warning("%s; reconnecting in %.0fs (attempt %d)", error, delay, attempt)
            stop.wait(delay)


def _watch(
    config: Config,
    db: Database,
    telegram: TelegramClient,
    client: MailboxClient,
    uidvalidity: int,
    limit: int,
    stop: Shutdown,
    heartbeat: _Heartbeat,
) -> None:
    while not stop.requested():
        messages = _fetch(db, config, client, uidvalidity, limit)
        if messages:
            pending = _select(db, config, uidvalidity, messages)
            heartbeat.record(len(pending), _deliver(db, telegram, pending))
        heartbeat.maybe_log()

        if stop.requested():
            return
        # More may have arrived while we were delivering; only idle once caught up.
        if len(messages) == limit:
            continue
        imap.idle(client, should_stop=stop.requested)


def _fetch(
    db: Database, config: Config, client: MailboxClient, uidvalidity: int, limit: int
) -> list[imap.RawMessage]:
    since = db.resume_uid(config.mail_username, uidvalidity)
    if since is None:
        logger.info("no state for uidvalidity %d, starting from the unseen mail", uidvalidity)
    return imap.fetch_new(client, since, limit)


def _select(
    db: Database, config: Config, uidvalidity: int, messages: Sequence[imap.RawMessage]
) -> list[tuple[MessageKey, Email]]:
    """Parse and drop anything already delivered."""
    pending: list[tuple[MessageKey, Email]] = []
    for message in messages:
        key = MessageKey(config.mail_username, uidvalidity, message.uid)
        email = parse(message.raw)
        logger.info(
            "uid %d parsed: %d body characters, %d attachment(s)",
            message.uid,
            len(email.body),
            len(email.attachments),
        )

        if db.record(key, email.message_id) is Status.SENT:
            logger.info("uid %d already delivered, skipping", message.uid)
            continue

        # The same message can reappear under a new UID after a UIDVALIDITY reset.
        if db.delivered_message_id(key.mailbox, email.message_id):
            logger.info("uid %d matches an already delivered Message-ID, skipping", message.uid)
            db.mark_sent(key)
            continue

        pending.append((key, email))
    return pending


def _deliver(
    db: Database, telegram: TelegramClient, pending: Sequence[tuple[MessageKey, Email]]
) -> int:
    failures = 0
    for key, email in pending:
        # Claimed and committed before the send, so a crash mid-delivery leaves a
        # record to retry: at-least-once, per PLAN.
        db.claim(key)
        try:
            sent_ids = telegram.send_text(format_email(email))
        except TelegramError as error:
            failures += 1
            db.mark_failed(key, str(error))
            logger.error("uid %d not delivered: %s", key.uid, error)
        else:
            db.mark_sent(key, sent_ids[0] if sent_ids else None)
            logger.info("uid %d delivered as %d telegram message(s)", key.uid, len(sent_ids))
            # The email is delivered; an attachment problem must not undo that.
            _send_attachments(telegram, email.attachments, key.uid)
    if pending:
        logger.info("forwarded %d of %d message(s)", len(pending) - failures, len(pending))
    return failures


def _send_attachments(
    telegram: TelegramClient, attachments: tuple[Attachment, ...], uid: int
) -> None:
    for attachment in attachments:
        if attachment.size > MAX_UPLOAD_BYTES:
            # Already flagged in the message body; nothing to upload.
            logger.warning(
                "uid %d: %s is %s, above the upload limit",
                uid,
                attachment.filename,
                format_size(attachment.size),
            )
            continue

        try:
            telegram.send_document(attachment.filename, attachment.payload, attachment.content_type)
        except TelegramError as error:
            logger.error("uid %d: could not upload %s: %s", uid, attachment.filename, error)
            _note_upload_failure(telegram, attachment.filename, str(error))
        else:
            logger.info("uid %d: uploaded %s", uid, attachment.filename)


def _note_upload_failure(telegram: TelegramClient, filename: str, reason: str) -> None:
    try:
        telegram.send_message(f"⚠️ Could not upload {escape(filename)}: {escape(reason)}")
    except TelegramError as error:
        logger.error("could not report the failed upload of %s: %s", filename, error)


def _report_interrupted(db: Database) -> None:
    interrupted = db.interrupted()
    if interrupted:
        logger.warning(
            "%d message(s) were interrupted mid-delivery and will be retried", len(interrupted)
        )


def _reconnect_delay(attempt: int) -> float:
    delay: float = min(RECONNECT_BASE_DELAY * 2.0 ** (attempt - 1), RECONNECT_MAX_DELAY)
    return delay + random.uniform(0.0, delay * 0.25)


class _Heartbeat:
    """Enough of a pulse to tell a healthy idle daemon from a wedged one."""

    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._last = self._monotonic()
        self.delivered = 0
        self.failed = 0

    def record(self, attempted: int, failures: int) -> None:
        self.delivered += attempted - failures
        self.failed += failures

    def maybe_log(self) -> None:
        now = self._monotonic()
        if now - self._last < HEARTBEAT_INTERVAL:
            return
        self._last = now
        logger.info("alive: %d delivered, %d failed since start", self.delivered, self.failed)
