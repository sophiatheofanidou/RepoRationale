"""Validated local configuration for external-service credentials."""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Credentials supplied locally through environment variables or `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    github_token: SecretStr = Field(
        validation_alias="GITHUB_TOKEN",
        min_length=1,
    )
    voyage_api_key: SecretStr = Field(
        validation_alias="VOYAGE_API_KEY",
        min_length=1,
    )
    anthropic_api_key: SecretStr = Field(
        validation_alias="ANTHROPIC_API_KEY",
        min_length=1,
    )


def load_settings() -> Settings:
    """Load and validate credentials without exposing their values."""
    return Settings()
