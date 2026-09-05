from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from email import message_from_bytes
from email.header import decode_header
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import PurePosixPath, PureWindowsPath
from typing import Final

logger = logging.getLogger(__name__)

FALLBACK_CHARSET: Final = "utf-8"
UNNAMED_ATTACHMENT: Final = "attachment"

# Inline parts below this are signature logos and tracking pixels, not documents
# anyone wants forwarded. Inline parts at or above it are real embedded files.
INLINE_MIN_BYTES: Final = 16 * 1024

_WHITESPACE = re.compile(r"\s+")
_BLANK_LINES = re.compile(r"\n{3,}")
_UNSAFE_FILENAME = re.compile(r"[\x00-\x1f\x7f<>:\"|?*\\/]")
_BLOCK_TAGS: Final = frozenset(
    {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote"}
)
_SKIP_TAGS: Final = frozenset({"script", "style", "head", "title"})


@dataclass(frozen=True, slots=True)
class Attachment:
    filename: str
    content_type: str
    payload: bytes

    @property
    def size(self) -> int:
        return len(self.payload)


@dataclass(frozen=True, slots=True)
class Email:
    subject: str
    sender: str
    to: tuple[str, ...]
    cc: tuple[str, ...]
    date: datetime | None
    body: str
    attachments: tuple[Attachment, ...]
    message_id: str = ""


def parse(raw: bytes) -> Email:
    """Turn raw message bytes into an Email. Never raises on malformed input."""
    message = message_from_bytes(raw)
    text, html = _extract_bodies(message)
    return Email(
        subject=_header(message, "Subject"),
        sender=_addresses(message, "From")[0] if _addresses(message, "From") else "",
        to=_addresses(message, "To"),
        cc=_addresses(message, "Cc"),
        date=_date(message),
        body=text or html_to_text(html),
        attachments=tuple(_attachments(message)),
        message_id=_header(message, "Message-ID"),
    )


def decode_mime_header(value: str | None) -> str:
    """Decode RFC 2047 encoded words and unfold the result."""
    if not value:
        return ""
    try:
        parts = decode_header(value)
    except Exception:
        return _WHITESPACE.sub(" ", value).strip()

    decoded: list[str] = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            decoded.append(chunk.decode(charset or FALLBACK_CHARSET, errors="replace"))
        else:
            decoded.append(chunk)
    return _WHITESPACE.sub(" ", "".join(decoded)).strip()


def html_to_text(html: str) -> str:
    if not html:
        return ""
    stripper = _HtmlStripper()
    try:
        stripper.feed(html)
        stripper.close()
    except Exception as error:
        logger.debug("html parsing failed, falling back to unescaped text: %s", error)
        return _collapse(unescape(re.sub(r"<[^>]+>", " ", html)))
    return _collapse(stripper.text())


def safe_filename(raw: str | None) -> str:
    """Attachment names come from untrusted mail: keep a bare, printable basename."""
    name = decode_mime_header(raw)
    name = PureWindowsPath(PurePosixPath(name).name).name
    name = _UNSAFE_FILENAME.sub("_", name).strip(" .")
    return name[:120] or UNNAMED_ATTACHMENT


def _header(message: Message, name: str) -> str:
    try:
        return decode_mime_header(message.get(name))
    except Exception as error:
        logger.debug("cannot read header %s: %s", name, error)
        return ""


def _addresses(message: Message, name: str) -> tuple[str, ...]:
    try:
        raw_values = message.get_all(name, [])
        pairs = getaddresses([decode_mime_header(value) for value in raw_values])
    except Exception as error:
        logger.debug("cannot read address header %s: %s", name, error)
        return ()
    return tuple(
        _format_address(display, address) for display, address in pairs if display or address
    )


def _format_address(display: str, address: str) -> str:
    if display and address:
        return f"{display} <{address}>"
    return display or address


def _date(message: Message) -> datetime | None:
    raw = message.get("Date")
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError) as error:
        logger.debug("unparsable Date header: %s", error)
        return None


def _extract_bodies(message: Message) -> tuple[str, str]:
    """Return the first text/plain and text/html bodies, ignoring attachments."""
    text = ""
    html = ""
    for part in message.walk():
        if part.get_content_maintype() == "multipart" or _is_attachment(part):
            continue
        content_type = part.get_content_type()
        if content_type == "text/plain" and not text:
            text = _decode_part(part)
        elif content_type == "text/html" and not html:
            html = _decode_part(part)
    return _collapse(text), html


def _decode_part(part: Message) -> str:
    try:
        payload = part.get_payload(decode=True)
    except Exception as error:
        logger.debug("cannot decode part: %s", error)
        return ""
    if not isinstance(payload, bytes):
        return ""
    charset = part.get_content_charset() or FALLBACK_CHARSET
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode(FALLBACK_CHARSET, errors="replace")


def _attachments(message: Message) -> list[Attachment]:
    found: list[Attachment] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart" or not _is_attachment(part):
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        if _is_inline(part) and len(payload) < INLINE_MIN_BYTES:
            logger.debug("skipping a %d byte inline part", len(payload))
            continue
        found.append(
            Attachment(
                filename=safe_filename(part.get_filename()),
                content_type=part.get_content_type(),
                payload=payload,
            )
        )
    return found


def _is_inline(part: Message) -> bool:
    disposition = (part.get_content_disposition() or "").lower()
    if disposition == "inline":
        return True
    return not disposition and bool(part.get("Content-ID"))


def _is_attachment(part: Message) -> bool:
    disposition = (part.get_content_disposition() or "").lower()
    if disposition == "attachment":
        return True
    if part.get_filename():
        return True
    return disposition == "inline" and part.get_content_maintype() != "text"


def _collapse(text: str) -> str:
    return _BLANK_LINES.sub("\n\n", text.replace("\r\n", "\n").replace("\r", "\n")).strip()


class _HtmlStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def text(self) -> str:
        return "".join(self._chunks)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:  # noqa: ARG002
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._chunks.append(data)
