from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from mailbridge.config import Secret
from mailbridge.parser import Attachment, Email
from mailbridge.telegram import (
    MAX_MESSAGE_LENGTH,
    MAX_UPLOAD_BYTES,
    MIN_SEND_INTERVAL,
    RateLimiter,
    TelegramClient,
    TelegramError,
    escape,
    format_email,
    format_size,
    split_text,
)

TOKEN = "123456:AAHfakeTokenValue"
CHAT_ID = "-1001234567890"

Handler = Callable[[httpx.Request], httpx.Response]

SAMPLE = Email(
    subject="Invoice #4821",
    sender="John Doe <john@example.com>",
    to=("user@mail.ru",),
    cc=(),
    date=datetime(2026, 9, 5, 12, 41, tzinfo=UTC),
    body="Here is the invoice.",
    attachments=(),
)


def make_client(
    handler: Handler,
    *,
    max_attempts: int = 1,
    slept: list[float] | None = None,
    min_interval: float = MIN_SEND_INTERVAL,
) -> tuple[TelegramClient, list[httpx.Request]]:
    """A client whose clock is fake, so retries and rate limiting cost no real time."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def sleep(seconds: float) -> None:
        if slept is not None:
            slept.append(seconds)

    client = TelegramClient(
        Secret(TOKEN),
        CHAT_ID,
        transport=httpx.MockTransport(record),
        max_attempts=max_attempts,
        min_interval=min_interval,
        sleep=sleep,
        monotonic=lambda: 0.0,
    )
    return client, seen


def ok(result: dict[str, Any]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": result})

    return handler


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


class TestEscape:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("plain", "plain"),
            ("a & b", "a &amp; b"),
            ("<b>bold</b>", "&lt;b&gt;bold&lt;/b&gt;"),
            ("a <script>x</script>", "a &lt;script&gt;x&lt;/script&gt;"),
        ],
    )
    def test_escapes_the_three_html_characters(self, raw: str, expected: str) -> None:
        assert escape(raw) == expected

    def test_ampersand_is_escaped_before_the_angle_brackets(self) -> None:
        assert escape("&lt;") == "&amp;lt;"


class TestFormatEmail:
    def test_renders_the_header_block(self) -> None:
        rendered = format_email(SAMPLE)

        assert "<b>From:</b> John Doe &lt;john@example.com&gt;" in rendered
        assert "<b>To:</b> user@mail.ru" in rendered
        assert "<b>Subject:</b> Invoice #4821" in rendered
        assert "<b>Date:</b> 2026-09-05 12:41" in rendered

    def test_includes_the_body(self) -> None:
        assert "Here is the invoice." in format_email(SAMPLE)

    def test_omits_cc_when_absent(self) -> None:
        assert "<b>Cc:</b>" not in format_email(SAMPLE)

    def test_includes_cc_when_present(self) -> None:
        rendered = format_email(replace(SAMPLE, cc=("Boss <boss@example.com>",)))

        assert "<b>Cc:</b> Boss &lt;boss@example.com&gt;" in rendered

    def test_omits_the_date_line_when_there_is_no_date(self) -> None:
        assert "<b>Date:</b>" not in format_email(replace(SAMPLE, date=None))

    def test_marks_an_empty_body(self) -> None:
        assert "<i>(empty body)</i>" in format_email(replace(SAMPLE, body=""))

    def test_falls_back_when_the_subject_is_missing(self) -> None:
        assert "(no subject)" in format_email(replace(SAMPLE, subject=""))

    def test_escapes_markup_in_the_subject(self) -> None:
        rendered = format_email(replace(SAMPLE, subject="<b>not bold</b>"))

        assert "&lt;b&gt;not bold&lt;/b&gt;" in rendered

    def test_escapes_markup_in_the_body(self) -> None:
        rendered = format_email(replace(SAMPLE, body="1 < 2 && 3 > 2"))

        assert "1 &lt; 2 &amp;&amp; 3 &gt; 2" in rendered

    def test_lists_attachments_with_their_size(self) -> None:
        rendered = format_email(
            replace(
                SAMPLE,
                attachments=(Attachment("invoice.pdf", "application/pdf", b"x" * 2048),),
            )
        )

        assert "📎 invoice.pdf (2.0 KB)" in rendered

    @pytest.mark.parametrize(
        ("size", "expected"),
        [
            (0, "0 B"),
            (512, "512 B"),
            (1024, "1.0 KB"),
            (1536, "1.5 KB"),
            (5 * 1024 * 1024, "5.0 MB"),
        ],
    )
    def test_formats_sizes(self, size: int, expected: str) -> None:
        assert format_size(size) == expected


class TestSplitText:
    def test_a_short_message_is_one_chunk(self) -> None:
        assert split_text("hello") == ["hello"]

    def test_empty_text_produces_nothing(self) -> None:
        assert split_text("") == []

    def test_every_chunk_respects_the_limit(self) -> None:
        text = "\n".join(f"line {i}" for i in range(500))

        assert all(len(chunk) <= 100 for chunk in split_text(text, limit=100))

    def test_splitting_loses_no_words(self) -> None:
        text = "\n".join(f"line {i}" for i in range(500))

        joined = " ".join(split_text(text, limit=100))
        assert all(f"line {i}" in joined for i in range(500))

    def test_prefers_line_boundaries(self) -> None:
        text = "\n".join(["a" * 40] * 6)

        assert all(
            "\n" not in chunk or chunk.count("a" * 40) > 1 for chunk in split_text(text, 100)
        )

    def test_hard_splits_a_single_overlong_line(self) -> None:
        chunks = split_text("x" * 250, limit=100)

        assert len(chunks) == 3
        assert "".join(chunks) == "x" * 250

    def test_never_cuts_an_entity_in_half(self) -> None:
        text = "x" * 97 + "&amp;" + "y" * 100

        for chunk in split_text(text, limit=100):
            assert "&" not in chunk or "&amp;" in chunk

    def test_never_cuts_a_tag_in_half(self) -> None:
        text = "x" * 97 + "<b>bold</b>" + "y" * 100

        for chunk in split_text(text, limit=100):
            assert chunk.count("<") == chunk.count(">")

    def test_a_long_email_splits_into_several_messages(self) -> None:
        long_body = "\n".join("word " * 20 for _ in range(200))
        chunks = split_text(format_email(replace(SAMPLE, body=long_body)))

        assert len(chunks) > 1
        assert all(len(chunk) <= MAX_MESSAGE_LENGTH for chunk in chunks)


class TestSendText:
    def test_sends_one_message_for_short_text(self) -> None:
        client, seen = make_client(ok({"message_id": 7}))

        with client:
            assert client.send_text("hello") == [7]
        assert len(seen) == 1

    def test_sends_one_message_per_chunk(self) -> None:
        client, seen = make_client(ok({"message_id": 7}))

        with client:
            client.send_text("y" * (MAX_MESSAGE_LENGTH * 2 + 10))

        assert len(seen) == 3

    def test_uses_html_parse_mode(self) -> None:
        client, seen = make_client(ok({"message_id": 7}))

        with client:
            client.send_text("hello")

        assert body_of(seen[0])["parse_mode"] == "HTML"


class TestSendDocument:
    def test_returns_the_message_id(self) -> None:
        client, _ = make_client(ok({"message_id": 88}))

        with client:
            assert client.send_document("a.pdf", b"data", "application/pdf") == 88

    def test_targets_the_send_document_endpoint(self) -> None:
        client, seen = make_client(ok({"message_id": 1}))

        with client:
            client.send_document("a.pdf", b"data", "application/pdf")

        assert seen[0].url.path.endswith("/sendDocument")

    def test_uploads_as_multipart_with_the_filename(self) -> None:
        client, seen = make_client(ok({"message_id": 1}))

        with client:
            client.send_document("invoice.pdf", b"pdf-bytes", "application/pdf")

        content_type = seen[0].headers["content-type"]
        assert content_type.startswith("multipart/form-data")
        assert b'filename="invoice.pdf"' in seen[0].content
        assert b"pdf-bytes" in seen[0].content

    def test_sends_the_chat_id(self) -> None:
        client, seen = make_client(ok({"message_id": 1}))

        with client:
            client.send_document("a.pdf", b"data", "application/pdf")

        assert CHAT_ID.encode() in seen[0].content

    def test_an_oversized_upload_is_refused_without_a_request(self) -> None:
        client, seen = make_client(ok({"message_id": 1}))

        with client, pytest.raises(TelegramError, match="upload limit"):
            client.send_document("huge.zip", b"x" * (MAX_UPLOAD_BYTES + 1), "application/zip")

        assert seen == []

    def test_a_file_at_the_limit_is_accepted(self) -> None:
        client, seen = make_client(ok({"message_id": 1}))

        with client:
            client.send_document("big.zip", b"x" * MAX_UPLOAD_BYTES, "application/zip")

        assert len(seen) == 1

    def test_an_api_error_surfaces(self) -> None:
        client, _ = make_client(
            lambda request: httpx.Response(413, json={"ok": False, "description": "too big"})
        )

        with client, pytest.raises(TelegramError, match="too big"):
            client.send_document("a.pdf", b"data", "application/pdf")

    def test_missing_message_id_is_an_error(self) -> None:
        client, _ = make_client(ok({}))

        with client, pytest.raises(TelegramError, match="message_id"):
            client.send_document("a.pdf", b"data", "application/pdf")


class TestOversizedAttachmentNote:
    def test_an_oversized_attachment_is_flagged_in_the_body(self) -> None:
        rendered = format_email(
            replace(
                SAMPLE,
                attachments=(
                    Attachment("huge.zip", "application/zip", b"x" * (MAX_UPLOAD_BYTES + 1)),
                ),
            )
        )

        assert "too large to upload" in rendered
        assert "huge.zip" in rendered

    def test_a_normal_attachment_is_not_flagged(self) -> None:
        rendered = format_email(
            replace(SAMPLE, attachments=(Attachment("ok.pdf", "application/pdf", b"x" * 100),))
        )

        assert "too large" not in rendered


def rate_limited(retry_after: float | None, then: Handler) -> Handler:
    """429 on the first call, then defer to the given handler."""
    calls = {"n": 0}
    parameters = {"retry_after": retry_after} if retry_after is not None else {}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                429,
                json={
                    "ok": False,
                    "description": "Too Many Requests",
                    **{"parameters": parameters},
                },
            )
        return then(request)

    return handler


class TestRetryPolicy:
    def test_a_429_is_retried(self) -> None:
        client, seen = make_client(rate_limited(7, ok({"message_id": 3})), max_attempts=3)

        with client:
            assert client.send_message("hello") == 3
        assert len(seen) == 2

    def test_the_retry_after_delay_is_honoured(self) -> None:
        slept: list[float] = []
        client, _ = make_client(rate_limited(7, ok({"message_id": 3})), max_attempts=3, slept=slept)

        with client:
            client.send_message("hello")

        assert 7 in slept

    def test_a_429_without_retry_after_falls_back_to_backoff(self) -> None:
        slept: list[float] = []
        client, _ = make_client(
            rate_limited(None, ok({"message_id": 3})), max_attempts=3, slept=slept, min_interval=0.0
        )

        with client:
            client.send_message("hello")

        assert any(delay > 0 for delay in slept)

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_server_errors_are_retried(self, status: int) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(status, text="upstream problem")
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 9}})

        client, seen = make_client(handler, max_attempts=3)

        with client:
            assert client.send_message("hello") == 9
        assert len(seen) == 2

    @pytest.mark.parametrize("status", [400, 401, 403, 404])
    def test_client_errors_are_not_retried(self, status: int) -> None:
        client, seen = make_client(
            lambda request: httpx.Response(status, json={"ok": False, "description": "no"}),
            max_attempts=5,
        )

        with client, pytest.raises(TelegramError):
            client.send_message("hello")

        assert len(seen) == 1

    def test_a_transport_failure_is_retried(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectError("connection refused", request=request)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 4}})

        client, seen = make_client(handler, max_attempts=3)

        with client:
            assert client.send_message("hello") == 4
        assert len(seen) == 2

    def test_a_persistent_failure_gives_up_after_max_attempts(self) -> None:
        client, seen = make_client(
            lambda request: httpx.Response(503, text="still down"), max_attempts=4
        )

        with client, pytest.raises(TelegramError, match="4 attempt"):
            client.send_message("hello")

        assert len(seen) == 4

    def test_backoff_grows_between_attempts(self) -> None:
        slept: list[float] = []
        client, _ = make_client(
            lambda request: httpx.Response(503, text="down"),
            max_attempts=4,
            slept=slept,
            min_interval=0.0,
        )

        with client, pytest.raises(TelegramError):
            client.send_message("hello")

        assert len(slept) == 3
        assert slept[0] < slept[-1]

    def test_a_429_storm_eventually_succeeds(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 4:
                return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 1}})
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 12}})

        client, seen = make_client(handler, max_attempts=5)

        with client:
            assert client.send_message("hello") == 12
        assert len(seen) == 4

    def test_an_upload_is_retried_too(self) -> None:
        client, seen = make_client(rate_limited(1, ok({"message_id": 5})), max_attempts=3)

        with client:
            assert client.send_document("a.pdf", b"data", "application/pdf") == 5
        assert len(seen) == 2


class TestRateLimiter:
    def test_the_first_call_does_not_wait(self) -> None:
        slept: list[float] = []
        limiter = RateLimiter(3.0, monotonic=lambda: 100.0, sleep=slept.append)

        limiter.wait()

        assert slept == []

    def test_a_following_call_waits_the_interval(self) -> None:
        slept: list[float] = []
        limiter = RateLimiter(3.0, monotonic=lambda: 100.0, sleep=slept.append)

        limiter.wait()
        limiter.wait()

        assert slept == [3.0]

    def test_no_wait_once_enough_time_has_passed(self) -> None:
        slept: list[float] = []
        clock = iter([100.0, 200.0])
        limiter = RateLimiter(3.0, monotonic=lambda: next(clock), sleep=slept.append)

        limiter.wait()
        limiter.wait()

        assert slept == []

    def test_sends_are_spaced(self) -> None:
        slept: list[float] = []
        client, _ = make_client(ok({"message_id": 1}), slept=slept)

        with client:
            client.send_message("one")
            client.send_message("two")
            client.send_message("three")

        assert slept == [MIN_SEND_INTERVAL, MIN_SEND_INTERVAL]
