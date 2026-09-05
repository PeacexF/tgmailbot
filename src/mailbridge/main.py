from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence

from mailbridge import __version__, database, imap
from mailbridge.config import Config, ConfigError, load_config
from mailbridge.database import Database, DatabaseError, MessageKey, Status
from mailbridge.imap import ImapError
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
        "--dry-run",
        action="store_true",
        help="fetch from the mailbox but send nothing to Telegram",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        metavar="N",
        help=f"forward at most N messages in this pass (default: {DEFAULT_LIMIT})",
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

    return run_once(config, limit=args.limit, dry_run=args.dry_run)


def run_once(config: Config, *, limit: int, dry_run: bool = False) -> int:
    """Fetch the unseen messages once and forward whatever has not been delivered yet."""
    try:
        with database.connect(config.database_path) as db:
            return _pass(config, db, limit=limit, dry_run=dry_run)
    except DatabaseError as error:
        logger.error("state unavailable: %s", error)
        return EXIT_FAILURE


def _pass(config: Config, db: Database, *, limit: int, dry_run: bool) -> int:
    interrupted = db.interrupted()
    if interrupted:
        logger.warning(
            "%d message(s) were interrupted mid-delivery and will be retried", len(interrupted)
        )

    try:
        with imap.connect(config) as client:
            uidvalidity = imap.open_folder(client, config.mail_folder)
            messages = imap.fetch_unseen(client, limit)
    except ImapError as error:
        logger.error("mailbox unavailable: %s", error)
        return EXIT_FAILURE

    if not messages:
        logger.info("nothing to forward")
        return EXIT_OK

    if dry_run:
        logger.info("dry run: %d message(s) fetched, none sent", len(messages))
        return EXIT_OK

    pending: list[tuple[MessageKey, Email]] = []
    skipped = 0
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
            skipped += 1
            continue

        # The same message can reappear under a new UID after a UIDVALIDITY reset.
        if db.delivered_message_id(key.mailbox, email.message_id):
            logger.info("uid %d matches an already delivered Message-ID, skipping", message.uid)
            db.mark_sent(key)
            skipped += 1
            continue

        pending.append((key, email))

    if not pending:
        logger.info("nothing new to forward (%d already delivered)", skipped)
        return EXIT_OK

    failures = 0
    with TelegramClient(config.telegram_bot_token, config.telegram_chat_id) as telegram:
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

    logger.info("forwarded %d, skipped %d, failed %d", len(pending) - failures, skipped, failures)
    return EXIT_FAILURE if failures else EXIT_OK


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
