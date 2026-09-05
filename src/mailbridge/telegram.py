from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from types import TracebackType
from typing import Any, Final, Self

import httpx

from mailbridge.config import Secret
from mailbridge.parser import Attachment, Email

logger = logging.getLogger(__name__)

API_BASE: Final = "https://api.telegram.org"
MAX_MESSAGE_LENGTH: Final = 4096
DEFAULT_TIMEOUT: Final = 30.0

# HTML needs three characters escaped; MarkdownV2 needs eighteen, and an email
# subject is free to contain all of them.
PARSE_MODE: Final = "HTML"

# The cloud Bot API accepts uploads up to 50 MB; only a self-hosted API server
# raises that. Anything larger is reported in the message instead of uploaded.
MAX_UPLOAD_BYTES: Final = 50 * 1024 * 1024
CAPTION_LIMIT: Final = 1024

TOO_MANY_REQUESTS: Final = 429
SERVER_ERROR: Final = 500

MAX_ATTEMPTS: Final = 5
RETRY_BASE_DELAY: Final = 1.0
RETRY_MAX_DELAY: Final = 60.0

# Telegram allows roughly 20 messages a minute to one group.
MIN_SEND_INTERVAL: Final = 3.0


class TelegramError(Exception):
    """A request that will not succeed by being repeated."""


class RateLimiter:
    """Spaces outgoing requests so a burst of mail cannot trip Telegram's limits."""

    def __init__(
        self,
        min_interval: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._min_interval = min_interval
        self._monotonic = monotonic
        self._sleep = sleep
        self._next_allowed = 0.0

    def wait(self) -> None:
        now = self._monotonic()
        delay = self._next_allowed - now
        if delay > 0:
            self._sleep(delay)
            now = self._monotonic()
        self._next_allowed = now + self._min_interval


class TelegramClient:
    def __init__(
        self,
        token: Secret,
        chat_id: str,
        *,
        base_url: str = API_BASE,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
        max_attempts: int = MAX_ATTEMPTS,
        min_interval: float = MIN_SEND_INTERVAL,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._chat_id = chat_id
        self._token = token.reveal()
        self._max_attempts = max(1, max_attempts)
        self._sleep = sleep
        self._limiter = RateLimiter(min_interval, monotonic=monotonic, sleep=sleep)
        self._http = httpx.Client(
            base_url=f"{base_url}/bot{self._token}",
            timeout=timeout,
            transport=transport,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def send_message(self, text: str) -> int:
        if not text.strip():
            raise TelegramError("refusing to send an empty message")
        if len(text) > MAX_MESSAGE_LENGTH:
            logger.warning(
                "truncating a %d character message to %d; splitting arrives in Phase 2",
                len(text),
                MAX_MESSAGE_LENGTH,
            )
            text = text[:MAX_MESSAGE_LENGTH]

        result = self._post(
            "sendMessage",
            {
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": PARSE_MODE,
                "disable_web_page_preview": True,
            },
        )
        message_id = result.get("message_id")
        if not isinstance(message_id, int):
            raise TelegramError("sendMessage returned no message_id")
        return message_id

    def send_text(self, text: str) -> list[int]:
        """Send text as however many messages Telegram's size limit requires."""
        return [self.send_message(chunk) for chunk in split_text(text)]

    def send_document(
        self, filename: str, content: bytes, content_type: str, caption: str = ""
    ) -> int:
        if len(content) > MAX_UPLOAD_BYTES:
            raise TelegramError(
                f"{filename} is {format_size(len(content))}, above the"
                f" {format_size(MAX_UPLOAD_BYTES)} upload limit"
            )

        data = {"chat_id": self._chat_id}
        if caption:
            data["caption"] = caption[:CAPTION_LIMIT]
            data["parse_mode"] = PARSE_MODE

        result = self._request(
            "sendDocument",
            data=data,
            files={"document": (filename, content, content_type)},
        )
        message_id = result.get("message_id")
        if not isinstance(message_id, int):
            raise TelegramError("sendDocument returned no message_id")
        return message_id

    def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request(method, json=payload)

    def _request(self, method: str, **kwargs: Any) -> dict[str, Any]:
        """Post with retries: 429 honours retry_after, 5xx backs off, 4xx is final."""
        failure = f"{method} failed"
        for attempt in range(1, self._max_attempts + 1):
            self._limiter.wait()
            try:
                response = self._http.post(f"/{method}", **kwargs)
            except httpx.HTTPError as error:
                failure = f"{method} request failed: {self._scrub(str(error))}"
                if not self._pause(attempt, self._backoff(attempt), failure):
                    break
                continue

            body = _body_of(response)
            if response.status_code == TOO_MANY_REQUESTS:
                delay = _retry_after(body) or self._backoff(attempt)
                failure = f"{method} was rate limited"
                if not self._pause(attempt, delay, failure):
                    break
                continue

            if response.status_code >= SERVER_ERROR:
                failure = f"{method} failed with HTTP {response.status_code}"
                if not self._pause(attempt, self._backoff(attempt), failure):
                    break
                continue

            return self._unwrap(method, response, body)

        raise TelegramError(f"{failure} after {self._max_attempts} attempt(s)")

    def _pause(self, attempt: int, delay: float, reason: str) -> bool:
        """Sleep before the next attempt. False means the attempts are exhausted."""
        if attempt >= self._max_attempts:
            return False
        logger.warning("%s; retrying in %.1fs (attempt %d)", reason, delay, attempt + 1)
        self._sleep(delay)
        return True

    def _backoff(self, attempt: int) -> float:
        delay: float = min(RETRY_BASE_DELAY * 2.0 ** (attempt - 1), RETRY_MAX_DELAY)
        return delay + random.uniform(0.0, delay * 0.25)

    def _unwrap(
        self, method: str, response: httpx.Response, body: dict[str, Any]
    ) -> dict[str, Any]:
        if response.status_code != httpx.codes.OK or body.get("ok") is not True:
            detail = body.get("description") or response.text[:200] or "no response body"
            raise TelegramError(
                f"{method} failed with HTTP {response.status_code}: {self._scrub(str(detail))}"
            )

        result = body.get("result")
        if not isinstance(result, dict):
            raise TelegramError(f"{method} returned an unexpected payload")
        return result

    def _scrub(self, text: str) -> str:
        """The bot token sits in the request URL, which surfaces in some httpx errors."""
        return text.replace(self._token, "***")


def _body_of(response: httpx.Response) -> dict[str, Any]:
    try:
        body: Any = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _retry_after(body: dict[str, Any]) -> float:
    parameters = body.get("parameters")
    if not isinstance(parameters, dict):
        return 0.0
    retry_after = parameters.get("retry_after")
    return float(retry_after) if isinstance(retry_after, int | float) else 0.0


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_email(email: Email) -> str:
    lines = [
        "📩 <b>New email</b>",
        "",
        f"<b>From:</b> {escape(email.sender or 'unknown sender')}",
    ]
    if email.to:
        lines.append(f"<b>To:</b> {escape(', '.join(email.to))}")
    if email.cc:
        lines.append(f"<b>Cc:</b> {escape(', '.join(email.cc))}")
    lines.append(f"<b>Subject:</b> {escape(email.subject or '(no subject)')}")
    if email.date is not None:
        lines.append(f"<b>Date:</b> {email.date.strftime('%Y-%m-%d %H:%M')}")

    body = email.body.strip()
    lines += ["", escape(body) if body else "<i>(empty body)</i>"]

    if email.attachments:
        lines.append("")
        lines += [_attachment_line(a) for a in email.attachments]
    return "\n".join(lines)


def _attachment_line(attachment: Attachment) -> str:
    line = f"📎 {escape(attachment.filename)} ({format_size(attachment.size)})"
    if attachment.size > MAX_UPLOAD_BYTES:
        line += " — too large to upload"
    return line


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def split_text(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Split on line boundaries where possible, never inside a tag or entity."""
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit + 1)
        if cut <= 0:
            cut = _markup_safe_cut(remaining, limit)
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    if remaining.strip():
        chunks.append(remaining)
    return chunks


def _markup_safe_cut(text: str, limit: int) -> int:
    """Back off a hard cut that would land inside `&amp;` or `<b>`."""
    cut = limit
    window = text[:limit]
    for opener, closer in (("&", ";"), ("<", ">")):
        start = window.rfind(opener)
        if start > 0 and closer not in window[start:]:
            cut = min(cut, start)
    return max(cut, 1)
