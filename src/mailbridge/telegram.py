from __future__ import annotations

import logging
from types import TracebackType
from typing import Any, Final, Self

import httpx

from mailbridge.config import Secret

logger = logging.getLogger(__name__)

API_BASE: Final = "https://api.telegram.org"
MAX_MESSAGE_LENGTH: Final = 4096
DEFAULT_TIMEOUT: Final = 30.0


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
            {"chat_id": self._chat_id, "text": text, "disable_web_page_preview": True},
        )
        message_id = result.get("message_id")
        if not isinstance(message_id, int):
            raise TelegramError("sendMessage returned no message_id")
        return message_id

    def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._http.post(f"/{method}", json=payload)
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
