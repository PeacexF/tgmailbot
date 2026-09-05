from __future__ import annotations

from pathlib import Path

import pytest

from mailbridge.config import Config, ConfigError, Secret, load_config, parse_env_file

VALID_ENV = {
    "MAIL_USERNAME": "user@mail.ru",
    "MAIL_PASSWORD": "app-password",
    "TELEGRAM_BOT_TOKEN": "123456:token",
    "TELEGRAM_CHAT_ID": "-1001234567890",
}


def test_loads_minimal_environment_with_defaults() -> None:
    config = load_config(VALID_ENV)

    assert config.mail_username == "user@mail.ru"
    assert config.mail_host == "imap.mail.ru"
    assert config.mail_port == 993
    assert config.mail_folder == "INBOX"
    assert config.database_path == Path("./data/mailbridge.db")
    assert config.log_level == "INFO"


def test_overrides_every_default() -> None:
    config = load_config(
        VALID_ENV
        | {
            "MAIL_HOST": "imap.example.com",
            "MAIL_PORT": "1993",
            "MAIL_FOLDER": "Archive",
            "DATABASE_PATH": "/var/lib/mailbridge.db",
            "LOG_LEVEL": "debug",
        }
    )

    assert config.mail_host == "imap.example.com"
    assert config.mail_port == 1993
    assert config.mail_folder == "Archive"
    assert config.database_path == Path("/var/lib/mailbridge.db")
    assert config.log_level == "DEBUG"


def test_reports_every_missing_variable_at_once() -> None:
    with pytest.raises(ConfigError) as raised:
        load_config({})

    assert len(raised.value.problems) == len(VALID_ENV)
    joined = "; ".join(raised.value.problems)
    for name in VALID_ENV:
        assert name in joined


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_required_value_is_missing(blank: str) -> None:
    with pytest.raises(ConfigError, match="MAIL_PASSWORD"):
        load_config(VALID_ENV | {"MAIL_PASSWORD": blank})


@pytest.mark.parametrize("port", ["not-a-number", "0", "70000", "-1"])
def test_rejects_invalid_port(port: str) -> None:
    with pytest.raises(ConfigError, match="MAIL_PORT"):
        load_config(VALID_ENV | {"MAIL_PORT": port})


def test_rejects_unknown_log_level() -> None:
    with pytest.raises(ConfigError, match="LOG_LEVEL"):
        load_config(VALID_ENV | {"LOG_LEVEL": "LOUD"})


def test_blank_optional_value_falls_back_to_default() -> None:
    config = load_config(VALID_ENV | {"MAIL_HOST": "", "MAIL_PORT": "  ", "LOG_LEVEL": ""})

    assert config.mail_host == "imap.mail.ru"
    assert config.mail_port == 993
    assert config.log_level == "INFO"


def test_config_error_lists_problems_in_its_message() -> None:
    with pytest.raises(ConfigError) as raised:
        load_config({"MAIL_USERNAME": "user@mail.ru", "MAIL_PORT": "abc"})

    assert "MAIL_PORT" in str(raised.value)
    assert "MAIL_PASSWORD" in str(raised.value)


class TestSecret:
    def test_reveals_only_on_request(self) -> None:
        secret = Secret("hunter2")

        assert secret.reveal() == "hunter2"

    @pytest.mark.parametrize("rendered", [str(Secret("hunter2")), repr(Secret("hunter2"))])
    def test_never_renders_its_value(self, rendered: str) -> None:
        assert "hunter2" not in rendered
        assert "***" in rendered

    def test_interpolation_does_not_leak(self) -> None:
        secret = Secret("hunter2")

        assert "hunter2" not in f"{secret}"
        assert "hunter2" not in f"{secret!r}"
        assert "hunter2" not in str({"password": secret})


class TestSummary:
    def test_omits_secrets(self) -> None:
        config = load_config(VALID_ENV)
        summary = config.summary()

        assert "app-password" not in summary
        assert "123456:token" not in summary

    def test_includes_operational_detail(self) -> None:
        summary = load_config(VALID_ENV).summary()

        assert "user@mail.ru" in summary
        assert "imap.mail.ru:993" in summary

    def test_secrets_are_reported_for_redaction(self) -> None:
        config = load_config(VALID_ENV)

        assert set(config.secrets()) == {"app-password", "123456:token"}

    def test_repr_of_the_whole_config_is_clean(self) -> None:
        rendered = repr(load_config(VALID_ENV))

        assert "app-password" not in rendered
        assert "123456:token" not in rendered


class TestParseEnvFile:
    def test_parses_comments_quotes_and_export_prefix(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text(
            "\n".join(
                [
                    "# a comment",
                    "",
                    "PLAIN=value",
                    "  SPACED  =  padded  ",
                    'DOUBLE="quoted value"',
                    "SINGLE='quoted value'",
                    "export EXPORTED=yes",
                    "EMPTY=",
                    "WITH_EQUALS=a=b=c",
                    "no-separator-line",
                    "=missing-key",
                ]
            ),
            encoding="utf-8",
        )

        values = parse_env_file(env_file)

        assert values == {
            "PLAIN": "value",
            "SPACED": "padded",
            "DOUBLE": "quoted value",
            "SINGLE": "quoted value",
            "EXPORTED": "yes",
            "EMPTY": "",
            "WITH_EQUALS": "a=b=c",
        }

    def test_ignores_a_hash_inside_a_value(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("MAIL_PASSWORD=pa#ss\n", encoding="utf-8")

        assert parse_env_file(env_file) == {"MAIL_PASSWORD": "pa#ss"}


class TestEnvironmentPrecedence:
    def test_real_environment_wins_over_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / ".env").write_text(
            "\n".join(f"{key}=from-file" for key in VALID_ENV), encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)
        for key in VALID_ENV:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("MAIL_USERNAME", "from-environment")

        config = load_config()

        assert config.mail_username == "from-environment"
        assert config.mail_password.reveal() == "from-file"

    def test_missing_env_file_is_not_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        for key, value in VALID_ENV.items():
            monkeypatch.setenv(key, value)

        assert isinstance(load_config(), Config)
