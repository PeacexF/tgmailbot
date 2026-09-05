from __future__ import annotations

import logging
import sys

import pytest

from mailbridge.log import RedactingFormatter, setup_logging

FORMAT = "%(levelname)s %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def make_record(message: str, args: tuple[object, ...] = ()) -> logging.LogRecord:
    return logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=args,
        exc_info=None,
    )


class TestRedactingFormatter:
    def test_redacts_a_secret_in_the_message(self) -> None:
        formatter = RedactingFormatter(["s3cret-value"], FORMAT, DATE_FORMAT)

        rendered = formatter.format(make_record("connecting with s3cret-value"))

        assert "s3cret-value" not in rendered
        assert "***" in rendered

    def test_redacts_a_secret_passed_as_an_argument(self) -> None:
        formatter = RedactingFormatter(["s3cret-value"], FORMAT, DATE_FORMAT)

        rendered = formatter.format(make_record("token=%s", ("s3cret-value",)))

        assert "s3cret-value" not in rendered

    def test_redacts_a_secret_inside_a_traceback(self) -> None:
        formatter = RedactingFormatter(["s3cret-value"], FORMAT, DATE_FORMAT)
        try:
            raise ValueError("login failed for s3cret-value")
        except ValueError:
            record = make_record("login error")
            record.exc_info = sys.exc_info()

        rendered = formatter.format(record)

        assert "s3cret-value" not in rendered

    def test_redacts_every_occurrence(self) -> None:
        formatter = RedactingFormatter(["s3cret-value"], FORMAT, DATE_FORMAT)

        rendered = formatter.format(make_record("s3cret-value and s3cret-value again"))

        assert "s3cret-value" not in rendered

    def test_leaves_unrelated_text_alone(self) -> None:
        formatter = RedactingFormatter(["s3cret-value"], FORMAT, DATE_FORMAT)

        rendered = formatter.format(make_record("connected to imap.mail.ru"))

        assert "connected to imap.mail.ru" in rendered

    def test_an_overlapping_secret_leaves_no_fragment(self) -> None:
        formatter = RedactingFormatter(["abcdef", "abcdefghij"], FORMAT, DATE_FORMAT)

        rendered = formatter.format(make_record("value=abcdefghij"))

        assert "abcdef" not in rendered

    @pytest.mark.parametrize("short", ["", "abc", "abcde"])
    def test_ignores_values_too_short_to_redact_safely(self, short: str) -> None:
        formatter = RedactingFormatter([short], FORMAT, DATE_FORMAT)

        rendered = formatter.format(make_record("a plain message"))

        assert rendered == "INFO a plain message"


class TestSetupLogging:
    def test_installs_exactly_one_handler(self) -> None:
        setup_logging("INFO")
        setup_logging("INFO")

        assert len(logging.getLogger().handlers) == 1

    def test_applies_the_requested_level(self) -> None:
        setup_logging("DEBUG")

        assert logging.getLogger().level == logging.DEBUG

    def test_accepts_a_lowercase_level(self) -> None:
        setup_logging("warning")

        assert logging.getLogger().level == logging.WARNING

    def test_wires_redaction_into_the_handler(self, capsys: pytest.CaptureFixture[str]) -> None:
        setup_logging("INFO", ["s3cret-value"])

        logging.getLogger("test").info("token is s3cret-value")

        assert "s3cret-value" not in capsys.readouterr().err
