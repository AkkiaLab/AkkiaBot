"""Typed application settings loaded from environment variables and ``.env``.

Each group maps to an environment-variable prefix: ``TELEGRAM_BOT_TOKEN`` sets
``settings.telegram.bot_token``, ``DB_URL`` sets ``settings.db.url`` and so on
(the first ``_`` separates the group from the field). Missing required values
raise :class:`pydantic.ValidationError` at load time, so the process fails at
startup instead of on first use.

Secrets are :class:`pydantic.SecretStr` (or URLs whose password is hidden) so
they never show up in ``repr``. The logging layer does not redact anything, so
callers must log :meth:`Settings.redacted` / ``safe_url`` instead of raw values.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import make_url

_SETTINGS_CONFIG = SettingsConfigDict(
    env_file=".env",
    env_file_encoding="utf-8",
    env_nested_delimiter="_",
    env_nested_max_split=1,
    extra="ignore",
    case_sensitive=False,
    # Validation errors must never echo secrets (DB URL passwords, tokens).
    hide_input_in_errors=True,
)


# Supported (backend, async driver) pairs; validated together.
_SUPPORTED_DB_DRIVERS = frozenset({("postgresql", "asyncpg"), ("sqlite", "aiosqlite")})


class _Group(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    @model_validator(mode="before")
    @classmethod
    def _blank_is_unset(cls, data: Any) -> Any:
        # `FOO=` in .env means "not configured", not "empty string".
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if v != ""}  # pyright: ignore[reportUnknownVariableType]
        return data


class TelegramSettings(_Group):
    bot_token: SecretStr
    """Bot API token from @BotFather. Required."""


class DatabaseSettings(_Group):
    url: str = Field(repr=False)
    """SQLAlchemy async URL, e.g. ``postgresql+asyncpg://...`` or
    ``sqlite+aiosqlite:///./data/akkiabot.db``. Required."""
    echo: bool = False

    @field_validator("url")
    @classmethod
    def _check_async_driver(cls, value: str) -> str:
        url = make_url(value)
        if (url.get_backend_name(), url.get_driver_name()) not in _SUPPORTED_DB_DRIVERS:
            raise ValueError("仅支持 postgresql+asyncpg:// 或 sqlite+aiosqlite://")
        return value

    @property
    def safe_url(self) -> str:
        """The URL with its password replaced by ``***``."""
        return make_url(self.url).render_as_string(hide_password=True)


class RedisSettings(_Group):
    url: SecretStr | None = None
    """Redis URL. When empty an in-process cache is used instead."""


class LlmSettings(_Group):
    base_url: AnyHttpUrl | None = None
    api_key: SecretStr | None = None
    model: str | None = None


class TurnstileSettings(_Group):
    secret_key: SecretStr | None = None


class WebAppSettings(_Group):
    url: AnyHttpUrl | None = None
    """Public URL of the Mini App, e.g. ``https://bot.akkia.moe``."""
    init_data_max_age: int = Field(default=3600, gt=0)
    """Seconds an initData payload stays valid after ``auth_date``."""


class CorsSettings(_Group):
    allow_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    """Comma-separated list of allowed origins."""

    @field_validator("allow_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value


class ObservabilitySettings(_Group):
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    sentry_dsn: SecretStr | None = None
    """Leave empty to disable Sentry entirely (no SDK init, no network)."""
    sentry_environment: str = "development"
    sentry_traces_sample_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    metrics_enabled: bool = True

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_level(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value


class Settings(BaseSettings):
    """Full application settings."""

    model_config = _SETTINGS_CONFIG

    telegram: TelegramSettings
    db: DatabaseSettings
    redis: RedisSettings = Field(default_factory=RedisSettings)
    llm: LlmSettings = Field(default_factory=LlmSettings)
    turnstile: TurnstileSettings = Field(default_factory=TurnstileSettings)
    webapp: WebAppSettings = Field(default_factory=WebAppSettings)
    cors: CorsSettings = Field(default_factory=CorsSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)

    def redacted(self) -> dict[str, Any]:
        """A JSON-safe dump with secrets masked, suitable for logging."""
        data = self.model_dump(mode="json")
        data["db"]["url"] = self.db.safe_url
        return data


class _DatabaseOnly(BaseSettings):
    model_config = _SETTINGS_CONFIG
    db: DatabaseSettings


class _ObservabilityOnly(BaseSettings):
    model_config = _SETTINGS_CONFIG
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)


def load_settings(**overrides: Any) -> Settings:
    """Load and validate the full settings; raises on missing required values."""
    return Settings(**overrides)


def load_database_settings() -> DatabaseSettings:
    """Load only ``DB_*`` (used by ``akkiabot migrate``, which needs no bot token)."""
    return _DatabaseOnly().db  # pyright: ignore[reportCallIssue]


def load_observability_settings() -> ObservabilitySettings:
    return _ObservabilityOnly().observability


__all__ = [
    "CorsSettings",
    "DatabaseSettings",
    "LlmSettings",
    "ObservabilitySettings",
    "RedisSettings",
    "Settings",
    "TelegramSettings",
    "TurnstileSettings",
    "WebAppSettings",
    "load_database_settings",
    "load_observability_settings",
    "load_settings",
]
