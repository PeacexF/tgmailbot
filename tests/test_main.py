from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, ClassVar

import pytest

from mailbridge.config import Config, load_config
from mailbridge.imap import ImapError, MailboxClient
from mailbridge.main import EXIT_CONFIG_ERROR, EXIT_FAILURE, EXIT_OK, main, run_once
from mailbridge.telegram import TelegramError

from .test_config import VALID_ENV
from .test_imap import FakeClient


def eml(subject: str = "Invoice 4821", body: str = "Here is the invoice.") -> bytes:
    return (
        "From: John Doe <john@example.com>\r\n"
        "To: user@mail.ru\r\n"
        f"Subject: {subject}\r\n"
        "Date: Fri, 5 Sep 2026 12:41:00 +0300\r\n"
        "\r\n"
        f"{body}\r\n"
    ).encode()


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.chdir(tmp_path)
    for key in (*VALID_ENV, "MAIL_HOST", "MAIL_PORT", "MAIL_FOLDER", "DATABASE_PATH", "LOG_LEVEL"):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


@pytest.fixture
def config() -> Config:
    return load_config(VALID_ENV)


class FakeTelegram:
    instances: ClassVar[list[FakeTelegram]] = []

    def __init__(self, *args: Any, fail_on: set[str] | None = None, **kwargs: Any) -> None:
        self.sent: list[str] = []
        self.closed = False
        self._fail_on = fail_on or set()
        FakeTelegram.instances.append(self)

    def __enter__(self) -> FakeTelegram:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self.closed = True

    def send_message(self, text: str) -> int:
        if any(marker in text for marker in self._fail_on):
            raise TelegramError("rejected by the API")
        self.sent.append(text)
        return 1000 + len(self.sent)

    def send_text(self, text: str) -> list[int]:
        return [self.send_message(text)]


@pytest.fixture
def telegram(monkeypatch: pytest.MonkeyPatch) -> type[FakeTelegram]:
    FakeTelegram.instances = []
    monkeypatch.setattr("mailbridge.main.TelegramClient", FakeTelegram)
    return FakeTelegram


@pytest.fixture
def mailbox(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Replace the IMAP connection so no test ever reaches the network."""

    def install(client: MailboxClient | None = None, error: ImapError | None = None) -> None:
        @contextmanager
        def fake_connect(_: Config) -> Iterator[MailboxClient]:
            if error is not None:
                raise error
            assert client is not None
            yield client

        monkeypatch.setattr("mailbridge.main.imap.connect", fake_connect)

    return install


class TestCheckMode:
    def test_succeeds_with_a_valid_configuration(
        self, isolated_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        for key, value in VALID_ENV.items():
            isolated_env.setenv(key, value)

        assert main(["--check"]) == EXIT_OK
        assert "configuration OK" in capsys.readouterr().err

    def test_reads_a_dotenv_file(
        self, tmp_path: Path, isolated_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / ".env").write_text(
            "\n".join(f"{key}={value}" for key, value in VALID_ENV.items()), encoding="utf-8"
        )

        assert main(["--check"]) == EXIT_OK
        assert "user@mail.ru" in capsys.readouterr().err

    def test_startup_logging_never_prints_secrets(
        self, isolated_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        for key, value in VALID_ENV.items():
            isolated_env.setenv(key, value)

        main(["--check"])

        stderr = capsys.readouterr().err
        assert VALID_ENV["MAIL_PASSWORD"] not in stderr
        assert VALID_ENV["TELEGRAM_BOT_TOKEN"] not in stderr

    def test_missing_configuration_exits_with_an_error_code(
        self, isolated_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--check"]) == EXIT_CONFIG_ERROR

        stderr = capsys.readouterr().err
        assert "configuration is invalid" in stderr
        assert "MAIL_USERNAME" in stderr
        assert ".env.example" in stderr

    def test_check_never_opens_a_connection(
        self, isolated_env: pytest.MonkeyPatch, mailbox: Any, telegram: type[FakeTelegram]
    ) -> None:
        for key, value in VALID_ENV.items():
            isolated_env.setenv(key, value)
        mailbox(error=ImapError("must not be called"))

        assert main(["--check"]) == EXIT_OK

    def test_version_flag_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as raised:
            main(["--version"])

        assert raised.value.code == 0
        assert "mailbridge" in capsys.readouterr().out


class TestRunOnce:
    def test_forwards_every_fetched_message(
        self, config: Config, mailbox: Any, telegram: type[FakeTelegram]
    ) -> None:
        mailbox(FakeClient([3, 8]))

        assert run_once(config, limit=10) == EXIT_OK
        assert len(telegram.instances[0].sent) == 2

    def test_the_body_carries_the_parsed_email(
        self, config: Config, mailbox: Any, telegram: type[FakeTelegram]
    ) -> None:
        mailbox(FakeClient([3], bodies={3: eml()}))

        run_once(config, limit=10)

        body = telegram.instances[0].sent[0]
        assert "Invoice 4821" in body
        assert "john@example.com" in body
        assert "Here is the invoice." in body

    def test_an_unparsable_message_still_produces_a_message(
        self, config: Config, mailbox: Any, telegram: type[FakeTelegram]
    ) -> None:
        mailbox(FakeClient([3], bodies={3: b"\x00\x01 not really an email"}))

        assert run_once(config, limit=10) == EXIT_OK
        assert len(telegram.instances[0].sent) == 1

    def test_an_empty_mailbox_sends_nothing(
        self, config: Config, mailbox: Any, telegram: type[FakeTelegram]
    ) -> None:
        mailbox(FakeClient([]))

        assert run_once(config, limit=10) == EXIT_OK
        assert telegram.instances == []

    def test_honours_the_limit(
        self, config: Config, mailbox: Any, telegram: type[FakeTelegram]
    ) -> None:
        mailbox(FakeClient([1, 2, 3, 4]))

        run_once(config, limit=2)

        assert len(telegram.instances[0].sent) == 2

    def test_dry_run_fetches_but_sends_nothing(
        self, config: Config, mailbox: Any, telegram: type[FakeTelegram]
    ) -> None:
        mailbox(FakeClient([1, 2]))

        assert run_once(config, limit=10, dry_run=True) == EXIT_OK
        assert telegram.instances == []

    def test_a_mailbox_failure_exits_nonzero(
        self, config: Config, mailbox: Any, telegram: type[FakeTelegram]
    ) -> None:
        mailbox(error=ImapError("login failed"))

        assert run_once(config, limit=10) == EXIT_FAILURE
        assert telegram.instances == []

    def test_a_delivery_failure_exits_nonzero(
        self, config: Config, mailbox: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        FakeTelegram.instances = []
        monkeypatch.setattr(
            "mailbridge.main.TelegramClient",
            lambda *args, **kwargs: FakeTelegram(fail_on={"second"}),
        )
        mailbox(FakeClient([1, 2], bodies={1: eml("first"), 2: eml("second")}))

        assert run_once(config, limit=10) == EXIT_FAILURE

    def test_one_failure_does_not_stop_the_others(
        self, config: Config, mailbox: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        FakeTelegram.instances = []
        monkeypatch.setattr(
            "mailbridge.main.TelegramClient",
            lambda *args, **kwargs: FakeTelegram(fail_on={"first"}),
        )
        mailbox(FakeClient([1, 2, 3], bodies={1: eml("first"), 2: eml("second"), 3: eml("third")}))

        run_once(config, limit=10)

        assert len(FakeTelegram.instances[0].sent) == 2

    def test_the_telegram_client_is_always_closed(
        self, config: Config, mailbox: Any, telegram: type[FakeTelegram]
    ) -> None:
        mailbox(FakeClient([1]))

        run_once(config, limit=10)

        assert telegram.instances[0].closed
