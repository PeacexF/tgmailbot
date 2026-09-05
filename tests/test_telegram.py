from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from mailbridge.config import Secret
from mailbridge.telegram import MAX_MESSAGE_LENGTH, TelegramClient, TelegramError

TOKEN = "123456:AAHfakeTokenValue"
CHAT_ID = "-1001234567890"


def make_client(
    handler: object, *, token: str = TOKEN
) -> tuple[TelegramClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert callable(handler)
        response: httpx.Response = handler(request)
        return response

    client = TelegramClient(Secret(token), CHAT_ID, transport=httpx.MockTransport(record))
    return client, seen


def ok(result: dict[str, Any]) -> object:
    return lambda request: httpx.Response(200, json={"ok": True, "result": result})


def body_of(request: httpx.Request) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(request.content)
    return payload


class TestSendMessage:
    def test_returns_the_telegram_message_id(self) -> None:
        client, _ = make_client(ok({"message_id": 42}))

        with client:
            assert client.send_message("hello") == 42

    def test_posts_the_text_and_chat_id(self) -> None:
        client, seen = make_client(ok({"message_id": 1}))

        with client:
            client.send_message("hello")

        assert body_of(seen[0])["chat_id"] == CHAT_ID
        assert body_of(seen[0])["text"] == "hello"

    def test_targets_the_send_message_endpoint(self) -> None:
        client, seen = make_client(ok({"message_id": 1}))

        with client:
            client.send_message("hello")

        assert seen[0].url.path == f"/bot{TOKEN}/sendMessage"
        assert seen[0].method == "POST"

    def test_truncates_an_oversized_body(self) -> None:
        client, seen = make_client(ok({"message_id": 1}))

        with client:
            client.send_message("x" * (MAX_MESSAGE_LENGTH + 500))

        assert len(body_of(seen[0])["text"]) == MAX_MESSAGE_LENGTH

    def test_a_message_at_the_limit_is_untouched(self) -> None:
        client, seen = make_client(ok({"message_id": 1}))

        with client:
            client.send_message("x" * MAX_MESSAGE_LENGTH)

        assert len(body_of(seen[0])["text"]) == MAX_MESSAGE_LENGTH

    @pytest.mark.parametrize("text", ["", "   \n  "])
    def test_refuses_an_empty_message_without_calling_the_api(self, text: str) -> None:
        client, seen = make_client(ok({"message_id": 1}))

        with client, pytest.raises(TelegramError, match="empty"):
            client.send_message(text)

        assert seen == []


class TestErrorHandling:
    def test_api_error_surfaces_the_description(self) -> None:
        client, _ = make_client(
            lambda request: httpx.Response(400, json={"ok": False, "description": "chat not found"})
        )

        with client, pytest.raises(TelegramError, match="chat not found"):
            client.send_message("hello")

    def test_ok_false_on_a_200_is_still_an_error(self) -> None:
        client, _ = make_client(
            lambda request: httpx.Response(200, json={"ok": False, "description": "nope"})
        )

        with client, pytest.raises(TelegramError, match="nope"):
            client.send_message("hello")

    def test_non_json_response_is_reported_with_its_status(self) -> None:
        client, _ = make_client(lambda request: httpx.Response(502, text="bad gateway"))

        with client, pytest.raises(TelegramError, match="502"):
            client.send_message("hello")

    def test_missing_message_id_is_an_error(self) -> None:
        client, _ = make_client(ok({}))

        with client, pytest.raises(TelegramError, match="message_id"):
            client.send_message("hello")

    def test_transport_failure_becomes_a_telegram_error(self) -> None:
        def explode(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        client, _ = make_client(explode)

        with client, pytest.raises(TelegramError, match="request failed"):
            client.send_message("hello")


class TestTokenSafety:
    def test_transport_error_does_not_leak_the_token(self) -> None:
        def explode(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"failed calling {request.url}", request=request)

        client, _ = make_client(explode)

        with client, pytest.raises(TelegramError) as raised:
            client.send_message("hello")

        assert TOKEN not in str(raised.value)

    def test_api_description_echoing_the_token_is_scrubbed(self) -> None:
        client, _ = make_client(
            lambda request: httpx.Response(
                401, json={"ok": False, "description": f"bad token {TOKEN}"}
            )
        )

        with client, pytest.raises(TelegramError) as raised:
            client.send_message("hello")

        assert TOKEN not in str(raised.value)
