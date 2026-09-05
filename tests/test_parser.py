from __future__ import annotations

from pathlib import Path

import pytest

from mailbridge.parser import (
    INLINE_MIN_BYTES,
    Email,
    decode_mime_header,
    html_to_text,
    parse,
    safe_filename,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> Email:
    return parse((FIXTURES / name).read_bytes())


class TestPlainMessage:
    def test_reads_the_headers(self) -> None:
        email = load("plain.eml")

        assert email.subject == "Invoice #4821"
        assert email.sender == "John Doe <john@example.com>"
        assert email.to == ("user@mail.ru",)
        assert email.cc == ()

    def test_reads_the_date(self) -> None:
        email = load("plain.eml")

        assert email.date is not None
        assert email.date.strftime("%Y-%m-%d %H:%M") == "2026-09-05 12:41"

    def test_reads_the_body(self) -> None:
        email = load("plain.eml")

        assert email.body.startswith("Hey,")
        assert "Here's the invoice you requested." in email.body

    def test_has_no_attachments(self) -> None:
        assert load("plain.eml").attachments == ()


class TestMultipart:
    def test_prefers_the_plain_text_part(self) -> None:
        email = load("multipart_alternative.eml")

        assert email.body == "The plain text version."

    def test_reads_multiple_recipients(self) -> None:
        email = load("multipart_alternative.eml")

        assert email.cc == ("Boss <boss@example.com>", "other@example.com")

    def test_falls_back_to_html_when_there_is_no_text_part(self) -> None:
        email = load("html_only.eml")

        assert "First paragraph." in email.body
        assert "Second & last." in email.body

    def test_html_fallback_drops_script_and_style(self) -> None:
        email = load("html_only.eml")

        assert "color: red" not in email.body
        assert "alert(" not in email.body
        assert "ignored" not in email.body


class TestEncodedHeaders:
    def test_decodes_a_base64_subject(self) -> None:
        assert load("encoded_headers.eml").subject == "Счёт за июнь"

    def test_decodes_a_base64_display_name(self) -> None:
        assert load("encoded_headers.eml").sender == "Иван Петров <ivan@example.ru>"

    def test_decodes_a_quoted_printable_recipient(self) -> None:
        assert load("encoded_headers.eml").to == ("Пользователь <user@mail.ru>",)

    def test_decodes_a_non_utf8_body(self) -> None:
        assert load("encoded_headers.eml").body == "Счёт за июнь, текстом."


class TestDegradedInput:
    def test_empty_body_parses_to_an_empty_string(self) -> None:
        email = load("empty_body.eml")

        assert email.body == ""
        assert email.subject == "Subject only"

    def test_missing_date_is_none(self) -> None:
        assert load("no_date.eml").date is None

    def test_unparsable_date_is_none(self) -> None:
        email = load("bad_date.eml")

        assert email.date is None
        assert email.subject == "Broken date"

    def test_unknown_charset_falls_back_instead_of_raising(self) -> None:
        assert "nobody has heard of" in load("unknown_charset.eml").body

    def test_a_non_email_does_not_raise(self) -> None:
        email = load("no_headers.eml")

        assert email.subject == ""
        assert email.sender == ""

    @pytest.mark.parametrize("raw", [b"", b"\x00\x01\x02", b"Subject: only\n"])
    def test_arbitrary_bytes_never_raise(self, raw: bytes) -> None:
        assert isinstance(parse(raw), Email)


class TestAttachments:
    def test_finds_every_attachment(self) -> None:
        assert len(load("with_attachment.eml").attachments) == 2

    def test_keeps_the_body_separate_from_the_attachments(self) -> None:
        assert load("with_attachment.eml").body == "Please find both files attached."

    def test_reads_name_type_and_payload(self) -> None:
        attachment = load("with_attachment.eml").attachments[0]

        assert attachment.filename == "invoice.pdf"
        assert attachment.content_type == "application/pdf"
        assert attachment.payload == b"Hello world!"
        assert attachment.size == 12

    def test_a_traversal_filename_is_reduced_to_a_basename(self) -> None:
        assert load("with_attachment.eml").attachments[1].filename == "passwd"


class TestSafeFilename:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("invoice.pdf", "invoice.pdf"),
            ("../../etc/passwd", "passwd"),
            ("C:\\Windows\\system32\\evil.exe", "evil.exe"),
            ("with/slash.txt", "slash.txt"),
            ("tab\there.txt", "tab here.txt"),
            ("bad\x00name.txt", "bad_name.txt"),
            ("pipe|colon:.txt", "pipe_colon_.txt"),
            ("", "attachment"),
            (None, "attachment"),
            ("...", "attachment"),
        ],
    )
    def test_reduces_to_a_bare_basename(self, raw: str | None, expected: str) -> None:
        assert safe_filename(raw) == expected

    def test_decodes_an_encoded_filename(self) -> None:
        assert safe_filename("=?UTF-8?B?0YTQsNC50LsucGRm?=") == "файл.pdf"

    def test_caps_a_very_long_name(self) -> None:
        assert len(safe_filename("a" * 500)) == 120


class TestDecodeMimeHeader:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("plain subject", "plain subject"),
            ("=?UTF-8?B?0KHRh9GR0YI=?=", "Счёт"),
            ("=?iso-8859-1?q?caf=E9?=", "café"),
            ("", ""),
            (None, ""),
        ],
    )
    def test_decodes_encoded_words(self, raw: str | None, expected: str) -> None:
        assert decode_mime_header(raw) == expected

    def test_unfolds_a_wrapped_header(self) -> None:
        assert decode_mime_header("first\r\n  second") == "first second"

    def test_a_broken_encoded_word_does_not_raise(self) -> None:
        assert decode_mime_header("=?UTF-8?B?!!!not-base64!!!?=") != ""


class TestHtmlToText:
    def test_strips_tags(self) -> None:
        assert html_to_text("<p>Hello <b>world</b></p>") == "Hello world"

    def test_unescapes_entities(self) -> None:
        assert html_to_text("<p>a &amp; b &lt; c</p>") == "a & b < c"

    def test_breaks_lines_on_block_tags(self) -> None:
        assert html_to_text("<div>one</div><div>two</div>") == "one\n\ntwo"

    def test_a_line_break_makes_one_newline(self) -> None:
        assert html_to_text("one<br>two") == "one\ntwo"

    def test_runs_of_blank_lines_are_capped(self) -> None:
        assert "\n\n\n" not in html_to_text("<p>a</p><div><p></p></div><p>b</p>")

    def test_empty_input_gives_empty_output(self) -> None:
        assert html_to_text("") == ""

    def test_unclosed_tags_do_not_raise(self) -> None:
        assert "text" in html_to_text("<div><p>text")


class TestInlineParts:
    def test_a_small_inline_logo_is_dropped(self) -> None:
        names = [a.filename for a in load("inline_images.eml").attachments]

        assert "signature-logo.png" not in names

    def test_a_large_inline_image_is_kept(self) -> None:
        names = [a.filename for a in load("inline_images.eml").attachments]

        assert names == ["holiday-photo.png"]

    def test_inline_parts_never_become_the_body(self) -> None:
        assert load("inline_images.eml").body == "See the photo below."

    def test_a_small_explicit_attachment_survives(self) -> None:
        attachments = load("small_attachment.eml").attachments

        assert [a.filename for a in attachments] == ["tiny.csv"]
        assert attachments[0].size < INLINE_MIN_BYTES

    def test_the_threshold_is_the_boundary(self) -> None:
        assert INLINE_MIN_BYTES == 16 * 1024
