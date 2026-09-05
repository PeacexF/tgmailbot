from __future__ import annotations

import base64
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, ClassVar

import pytest

from mailbridge.config import Config, load_config
from mailbridge.database import MessageKey, Status, connect
from mailbridge.imap import ImapError, MailboxClient
from mailbridge.main import EXIT_CONFIG_ERROR, EXIT_FAILURE, EXIT_OK, main, run_once
from mailbridge.telegram import TelegramError

from .test_config import VALID_ENV
from .test_imap import FakeClient


def eml(
    subject: str = "Invoice 4821",
    body: str = "Here is the invoice.",
    message_id: str | None = None,
) -> bytes:
    """A minimal but realistic message. Pass message_id="" to omit the header."""
    if message_id is None:
        message_id = f"<{subject.replace(' ', '-')}@example.com>"
    header = f"Message-ID: {message_id}\r\n" if message_id else ""
    return (
        "From: John Doe <john@example.com>\r\n"
        "To: user@mail.ru\r\n"
        f"Subject: {subject}\r\n"
        "Date: Fri, 5 Sep 2026 12:41:00 +0300\r\n"
        f"{header}"
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
def config(tmp_path: Path) -> Config:
    return load_config(VALID_ENV | {"DATABASE_PATH": str(tmp_path / "state.db")})


class FakeTelegram:
    instances: ClassVar[list[FakeTelegram]] = []

    def __init__(
        self,
        *args: Any,
        fail_on: set[str] | None = None,
        reject_uploads: set[str] | None = None,
        **kwargs: Any,
    ) -> None:
        self.sent: list[str] = []
        self.uploaded: list[tuple[str, bytes]] = []
        self.closed = False
        self._fail_on = fail_on or set()
        self._reject_uploads = reject_uploads or set()
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

    def send_document(
        self, filename: str, content: bytes, content_type: str, caption: str = ""
    ) -> int:
        if filename in self._reject_uploads:
            raise TelegramError("upload rejected by the API")
        self.uploaded.append((filename, content))
        return 2000 + len(self.uploaded)


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


class TestDeduplication:
    def test_a_second_pass_sends_nothing_new(
        self, config: Config, mailbox: Any, telegram: type[FakeTelegram]
    ) -> None:
        mailbox(FakeClient([1, 2], bodies={1: eml("first"), 2: eml("second")}))

        assert run_once(config, limit=10) == EXIT_OK
        assert run_once(config, limit=10) == EXIT_OK

        assert len(telegram.instances[0].sent) == 2
        assert len(telegram.instances) == 1

    def test_only_the_new_message_is_sent_on_a_later_pass(
        self, config: Config, mailbox: Any, telegram: type[FakeTelegram]
    ) -> None:
        mailbox(FakeClient([1], bodies={1: eml("first")}))
        run_once(config, limit=10)

        mailbox(FakeClient([1, 2], bodies={1: eml("first"), 2: eml("second")}))
        run_once(config, limit=10)

        assert len(telegram.instances[1].sent) == 1
        assert "second" in telegram.instances[1].sent[0]

    def test_delivery_state_is_persisted(self, config: Config, mailbox: Any, telegram: Any) -> None:
        mailbox(FakeClient([7], bodies={7: eml()}))

        run_once(config, limit=10)

        with connect(config.database_path) as db:
            record = db.get(MessageKey(config.mail_username, 42, 7))
            assert record is not None
            assert record.status is Status.SENT
            assert record.telegram_message_id == 1001

    def test_a_uidvalidity_reset_does_not_resend(
        self, config: Config, mailbox: Any, telegram: type[FakeTelegram]
    ) -> None:
        message = eml("renumbered")
        mailbox(FakeClient([5], bodies={5: message}, uidvalidity=42))
        run_once(config, limit=10)

        # The server renumbered the folder: same message, new UIDVALIDITY and UID.
        mailbox(FakeClient([1], bodies={1: message}, uidvalidity=77))
        run_once(config, limit=10)

        # Nothing to send, so the second pass never opens a Telegram client at all.
        assert len(telegram.instances) == 1
        assert len(telegram.instances[0].sent) == 1

    def test_a_reset_resends_a_message_that_carries_no_message_id(
        self, config: Config, mailbox: Any, telegram: type[FakeTelegram]
    ) -> None:
        # Without a Message-ID there is nothing left to recognise the message by,
        # so a renumbered folder produces a duplicate rather than a loss.
        message = eml("anonymous", message_id="")
        mailbox(FakeClient([5], bodies={5: message}, uidvalidity=42))
        run_once(config, limit=10)

        mailbox(FakeClient([1], bodies={1: message}, uidvalidity=77))
        run_once(config, limit=10)

        assert len(telegram.instances[1].sent) == 1

    def test_a_failed_message_is_retried_on_the_next_pass(
        self, config: Config, mailbox: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        FakeTelegram.instances = []
        monkeypatch.setattr(
            "mailbridge.main.TelegramClient",
            lambda *args, **kwargs: FakeTelegram(fail_on={"flaky"}),
        )
        mailbox(FakeClient([1], bodies={1: eml("flaky")}))
        assert run_once(config, limit=10) == EXIT_FAILURE

        monkeypatch.setattr("mailbridge.main.TelegramClient", FakeTelegram)
        assert run_once(config, limit=10) == EXIT_OK
        assert len(FakeTelegram.instances[1].sent) == 1

    def test_a_dry_run_records_no_state(
        self, config: Config, mailbox: Any, telegram: type[FakeTelegram]
    ) -> None:
        mailbox(FakeClient([1], bodies={1: eml()}))

        run_once(config, limit=10, dry_run=True)

        with connect(config.database_path) as db:
            assert db.status_of(MessageKey(config.mail_username, 42, 1)) is None


class TestCrashRecovery:
    def test_a_crash_mid_delivery_resends_rather_than_loses(
        self, config: Config, mailbox: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class CrashError(Exception):
            pass

        class Crashing(FakeTelegram):
            def send_text(self, text: str) -> list[int]:
                raise CrashError("process died mid-send")

        FakeTelegram.instances = []
        monkeypatch.setattr("mailbridge.main.TelegramClient", Crashing)
        mailbox(FakeClient([1], bodies={1: eml()}))

        with pytest.raises(CrashError):
            run_once(config, limit=10)

        key = MessageKey(config.mail_username, 42, 1)
        with connect(config.database_path) as db:
            assert db.interrupted() == [key]

        monkeypatch.setattr("mailbridge.main.TelegramClient", FakeTelegram)
        assert run_once(config, limit=10) == EXIT_OK

        assert len(FakeTelegram.instances[-1].sent) == 1
        with connect(config.database_path) as db:
            record = db.get(key)
            assert record is not None
            assert record.status is Status.SENT
            assert record.attempts == 2

    def test_the_duplicate_is_bounded_to_one(
        self, config: Config, mailbox: Any, telegram: type[FakeTelegram]
    ) -> None:
        mailbox(FakeClient([1], bodies={1: eml()}))
        run_once(config, limit=10)
        run_once(config, limit=10)
        run_once(config, limit=10)

        total = sum(len(instance.sent) for instance in telegram.instances)
        assert total == 1


def eml_with_attachment(filename: str = "invoice.pdf", payload: bytes = b"Hello world!") -> bytes:
    encoded = base64.b64encode(payload).decode()
    return (
        "From: John Doe <john@example.com>\r\n"
        "To: user@mail.ru\r\n"
        "Subject: With attachment\r\n"
        "Date: Fri, 5 Sep 2026 12:41:00 +0300\r\n"
        "Message-ID: <att@example.com>\r\n"
        'Content-Type: multipart/mixed; boundary="MIX"\r\n'
        "\r\n"
        "--MIX\r\n"
        'Content-Type: text/plain; charset="utf-8"\r\n'
        "\r\n"
        "See attached.\r\n"
        "\r\n"
        "--MIX\r\n"
        f'Content-Type: application/pdf; name="{filename}"\r\n'
        f'Content-Disposition: attachment; filename="{filename}"\r\n'
        "Content-Transfer-Encoding: base64\r\n"
        "\r\n"
        f"{encoded}\r\n"
        "\r\n"
        "--MIX--\r\n"
    ).encode()


class TestAttachments:
    def test_an_attachment_is_uploaded_after_the_body(
        self, config: Config, mailbox: Any, telegram: type[FakeTelegram]
    ) -> None:
        mailbox(FakeClient([1], bodies={1: eml_with_attachment()}))

        assert run_once(config, limit=10) == EXIT_OK

        client = telegram.instances[0]
        assert len(client.sent) == 1
        assert client.uploaded == [("invoice.pdf", b"Hello world!")]

    def test_every_attachment_is_uploaded(
        self, config: Config, mailbox: Any, telegram: type[FakeTelegram]
    ) -> None:
        raw = eml_with_attachment().replace(
            b"--MIX--",
            b'--MIX\r\nContent-Type: text/plain; name="notes.txt"\r\n'
            b'Content-Disposition: attachment; filename="notes.txt"\r\n\r\n'
            b"some notes\r\n\r\n--MIX--",
        )
        mailbox(FakeClient([1], bodies={1: raw}))

        run_once(config, limit=10)

        assert [name for name, _ in telegram.instances[0].uploaded] == [
            "invoice.pdf",
            "notes.txt",
        ]

    def test_an_oversized_attachment_is_not_uploaded(
        self,
        config: Config,
        mailbox: Any,
        telegram: type[FakeTelegram],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("mailbridge.main.MAX_UPLOAD_BYTES", 4)
        mailbox(FakeClient([1], bodies={1: eml_with_attachment(payload=b"much too large")}))

        assert run_once(config, limit=10) == EXIT_OK
        assert telegram.instances[0].uploaded == []

    def test_the_email_is_still_delivered_when_an_upload_fails(
        self, config: Config, mailbox: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        FakeTelegram.instances = []
        monkeypatch.setattr(
            "mailbridge.main.TelegramClient",
            lambda *args, **kwargs: FakeTelegram(reject_uploads={"invoice.pdf"}),
        )
        mailbox(FakeClient([1], bodies={1: eml_with_attachment()}))

        assert run_once(config, limit=10) == EXIT_OK

        client = FakeTelegram.instances[0]
        assert "See attached." in client.sent[0]
        with connect(config.database_path) as db:
            assert db.status_of(MessageKey(config.mail_username, 42, 1)) is Status.SENT

    def test_a_failed_upload_is_reported_in_the_thread(
        self, config: Config, mailbox: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        FakeTelegram.instances = []
        monkeypatch.setattr(
            "mailbridge.main.TelegramClient",
            lambda *args, **kwargs: FakeTelegram(reject_uploads={"invoice.pdf"}),
        )
        mailbox(FakeClient([1], bodies={1: eml_with_attachment()}))

        run_once(config, limit=10)

        assert any(
            "Could not upload invoice.pdf" in message for message in FakeTelegram.instances[0].sent
        )

    def test_one_failed_upload_does_not_block_the_next(
        self, config: Config, mailbox: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        raw = eml_with_attachment().replace(
            b"--MIX--",
            b'--MIX\r\nContent-Type: text/plain; name="notes.txt"\r\n'
            b'Content-Disposition: attachment; filename="notes.txt"\r\n\r\n'
            b"some notes\r\n\r\n--MIX--",
        )
        FakeTelegram.instances = []
        monkeypatch.setattr(
            "mailbridge.main.TelegramClient",
            lambda *args, **kwargs: FakeTelegram(reject_uploads={"invoice.pdf"}),
        )
        mailbox(FakeClient([1], bodies={1: raw}))

        run_once(config, limit=10)

        assert [name for name, _ in FakeTelegram.instances[0].uploaded] == ["notes.txt"]

    def test_attachments_are_not_re_uploaded_on_a_second_pass(
        self, config: Config, mailbox: Any, telegram: type[FakeTelegram]
    ) -> None:
        mailbox(FakeClient([1], bodies={1: eml_with_attachment()}))
        run_once(config, limit=10)
        run_once(config, limit=10)

        assert len(telegram.instances) == 1
        assert len(telegram.instances[0].uploaded) == 1
