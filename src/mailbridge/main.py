from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence

from mailbridge import __version__
from mailbridge.config import ConfigError, load_config
from mailbridge.log import setup_logging

logger = logging.getLogger("mailbridge")

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2


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

    logger.info("nothing to run yet: the daemon loop arrives in Phase 1")
    return EXIT_OK
