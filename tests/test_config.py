"""Tests for local credential configuration."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from reporationale.config import Settings, load_settings

ENVIRONMENT_VARIABLES = (
    "GITHUB_TOKEN",
    "VOYAGE_API_KEY",
    "ANTHROPIC_API_KEY",
)


def clear_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove credentials inherited from the machine running the tests."""
    for variable in ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(variable, raising=False)


def test_settings_load_credentials_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Environment variables populate all required secret fields."""
    values = {
        "GITHUB_TOKEN": "github-test-value",
        "VOYAGE_API_KEY": "voyage-test-value",
        "ANTHROPIC_API_KEY": "anthropic-test-value",
    }
    for variable, value in values.items():
        monkeypatch.setenv(variable, value)

    settings = Settings(_env_file=None)

    assert settings.github_token.get_secret_value() == values["GITHUB_TOKEN"]
    assert settings.voyage_api_key.get_secret_value() == values["VOYAGE_API_KEY"]
    assert settings.anthropic_api_key.get_secret_value() == values["ANTHROPIC_API_KEY"]
    assert all(value not in repr(settings) for value in values.values())


def test_load_settings_reads_local_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The application can read a local uncommitted `.env` file."""
    clear_credentials(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "GITHUB_TOKEN=github-file-value\n"
        "VOYAGE_API_KEY=voyage-file-value\n"
        "ANTHROPIC_API_KEY=anthropic-file-value\n",
        encoding="utf-8",
    )

    settings = load_settings()

    assert settings.github_token.get_secret_value() == "github-file-value"
    assert settings.voyage_api_key.get_secret_value() == "voyage-file-value"
    assert settings.anthropic_api_key.get_secret_value() == "anthropic-file-value"


def test_settings_reject_missing_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clear validation error is raised before an API call can be attempted."""
    clear_credentials(monkeypatch)

    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None)

    missing_fields = {item["loc"][0] for item in error.value.errors()}
    assert missing_fields == set(ENVIRONMENT_VARIABLES)


def test_settings_reject_empty_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blank placeholders cannot pass validation as usable credentials."""
    monkeypatch.setenv("GITHUB_TOKEN", "")
    monkeypatch.setenv("VOYAGE_API_KEY", "voyage-test-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test-value")

    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None)

    assert error.value.errors()[0]["loc"] == ("GITHUB_TOKEN",)
