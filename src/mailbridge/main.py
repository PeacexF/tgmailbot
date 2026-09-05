from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence

from mailbridge import __version__, imap
from mailbridge.config import Config, ConfigError, load_config
from mailbridge.imap import ImapError, RawMessage
from mailbridge.log import setup_logging
from mailbridge.parser import parse
from mailbridge.telegram import TelegramClient, TelegramError, format_email

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
    """Fetch the unseen messages once and forward them. No persistence yet."""
    try:
        with imap.connect(config) as client:
            messages = imap.fetch_unseen(client, config.mail_folder, limit)
    except ImapError as error:
        logger.error("mailbox unavailable: %s", error)
        return EXIT_FAILURE

    if not messages:
        logger.info("nothing to forward")
        return EXIT_OK

    if dry_run:
        logger.info("dry run: %d message(s) fetched, none sent", len(messages))
        return EXIT_OK

    failures = 0
    with TelegramClient(config.telegram_bot_token, config.telegram_chat_id) as telegram:
        for message in messages:
            try:
                sent_ids = telegram.send_text(_render(message))
            except TelegramError as error:
                failures += 1
                logger.error("uid %d not delivered: %s", message.uid, error)
            else:
                logger.info(
                    "uid %d delivered as %d telegram message(s): %s",
                    message.uid,
                    len(sent_ids),
                    ", ".join(str(i) for i in sent_ids),
                )

    logger.info("forwarded %d of %d message(s)", len(messages) - failures, len(messages))
    return EXIT_FAILURE if failures else EXIT_OK


def _render(message: RawMessage) -> str:
    email = parse(message.raw)
    logger.info(
        "uid %d parsed: %d body characters, %d attachment(s)",
        message.uid,
        len(email.body),
        len(email.attachments),
    )
    return format_email(email)
