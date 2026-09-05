from __future__ import annotations

from pathlib import Path

import pytest

from mailbridge.main import EXIT_CONFIG_ERROR, EXIT_OK, main

from .test_config import VALID_ENV


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.chdir(tmp_path)
    for key in (*VALID_ENV, "MAIL_HOST", "MAIL_PORT", "MAIL_FOLDER", "DATABASE_PATH", "LOG_LEVEL"):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_check_succeeds_with_a_valid_configuration(
    isolated_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key, value in VALID_ENV.items():
        isolated_env.setenv(key, value)

    assert main(["--check"]) == EXIT_OK
    assert "configuration OK" in capsys.readouterr().err


def test_check_reads_a_dotenv_file(
    tmp_path: Path, isolated_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".env").write_text(
        "\n".join(f"{key}={value}" for key, value in VALID_ENV.items()), encoding="utf-8"
    )

    assert main(["--check"]) == EXIT_OK
    assert "user@mail.ru" in capsys.readouterr().err


def test_startup_logging_never_prints_secrets(
    isolated_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key, value in VALID_ENV.items():
        isolated_env.setenv(key, value)

    main(["--check"])

    stderr = capsys.readouterr().err
    assert VALID_ENV["MAIL_PASSWORD"] not in stderr
    assert VALID_ENV["TELEGRAM_BOT_TOKEN"] not in stderr


def test_missing_configuration_exits_with_an_error_code(
    isolated_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--check"]) == EXIT_CONFIG_ERROR

    stderr = capsys.readouterr().err
    assert "configuration is invalid" in stderr
    assert "MAIL_USERNAME" in stderr
    assert ".env.example" in stderr


def test_run_without_check_exits_cleanly(
    isolated_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key, value in VALID_ENV.items():
        isolated_env.setenv(key, value)

    assert main([]) == EXIT_OK
    assert "Phase 1" in capsys.readouterr().err


def test_version_flag_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--version"])

    assert raised.value.code == 0
    assert "mailbridge" in capsys.readouterr().out
