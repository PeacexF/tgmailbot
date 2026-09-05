from __future__ import annotations

import logging
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


class TelegramError(Exception):
    pass


class TelegramClient:
    def __init__(
        self,
        token: Secret,
        chat_id: str,
        *,
        base_url: str = API_BASE,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._chat_id = chat_id
        self._token = token.reveal()
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
        try:
            response = self._http.post(f"/{method}", **kwargs)
        except httpx.HTTPError as error:
            raise TelegramError(f"{method} request failed: {self._scrub(str(error))}") from error
        return self._unwrap(method, response)

    def _unwrap(self, method: str, response: httpx.Response) -> dict[str, Any]:
        body: Any = None
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            body = {}

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
