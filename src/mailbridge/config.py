from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, final

DEFAULT_ENV_FILE: Final = Path(".env")

_VALID_LOG_LEVELS: Final = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})


class ConfigError(Exception):
    def __init__(self, problems: Iterable[str]) -> None:
        self.problems: Final = tuple(problems)
        super().__init__("; ".join(self.problems))


@final
class Secret:
    """A string that does not leak through logging, tracebacks, or f-strings."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(***)"

    def __str__(self) -> str:
        return "***"


@dataclass(frozen=True, slots=True)
class Config:
    mail_host: str
    mail_port: int
    mail_username: str
    mail_password: Secret
    mail_folder: str
    telegram_bot_token: Secret
    telegram_chat_id: str
    database_path: Path
    log_level: str

    def secrets(self) -> tuple[str, ...]:
        return (self.mail_password.reveal(), self.telegram_bot_token.reveal())

    def summary(self) -> str:
        return (
            f"mailbox={self.mail_username} "
            f"server={self.mail_host}:{self.mail_port} "
            f"folder={self.mail_folder} "
            f"chat={self.telegram_chat_id} "
            f"database={self.database_path} "
            f"log_level={self.log_level}"
        )


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse ``KEY=value`` lines. No interpolation, no multi-line values.

    Malformed lines are skipped rather than raising, so a stray line never blocks startup.
    """
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").lstrip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def load_environment(env_file: Path = DEFAULT_ENV_FILE) -> Mapping[str, str]:
    merged: dict[str, str] = {}
    if env_file.is_file():
        merged.update(parse_env_file(env_file))
    merged.update(os.environ)
    return merged


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Build a Config, reporting every problem at once rather than the first."""
    source = load_environment() if env is None else env
    problems: list[str] = []

    def required(name: str) -> str:
        value = source.get(name, "").strip()
        if not value:
            problems.append(f"{name} is required but missing or empty")
        return value

    mail_username = required("MAIL_USERNAME")
    mail_password = required("MAIL_PASSWORD")
    telegram_bot_token = required("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = required("TELEGRAM_CHAT_ID")

    mail_host = source.get("MAIL_HOST", "").strip() or "imap.mail.ru"
    mail_folder = source.get("MAIL_FOLDER", "").strip() or "INBOX"
    database_path = source.get("DATABASE_PATH", "").strip() or "./data/mailbridge.db"

    raw_port = source.get("MAIL_PORT", "").strip() or "993"
    mail_port = 993
    try:
        mail_port = int(raw_port)
    except ValueError:
        problems.append(f"MAIL_PORT must be an integer, got {raw_port!r}")
    else:
        if not 1 <= mail_port <= 65535:
            problems.append(f"MAIL_PORT must be between 1 and 65535, got {mail_port}")

    log_level = (source.get("LOG_LEVEL", "").strip() or "INFO").upper()
    if log_level not in _VALID_LOG_LEVELS:
        problems.append(
            f"LOG_LEVEL must be one of {', '.join(sorted(_VALID_LOG_LEVELS))}, got {log_level!r}"
        )

    if problems:
        raise ConfigError(problems)

    return Config(
        mail_host=mail_host,
        mail_port=mail_port,
        mail_username=mail_username,
        mail_password=Secret(mail_password),
        mail_folder=mail_folder,
        telegram_bot_token=Secret(telegram_bot_token),
        telegram_chat_id=telegram_chat_id,
        database_path=Path(database_path),
        log_level=log_level,
    )
