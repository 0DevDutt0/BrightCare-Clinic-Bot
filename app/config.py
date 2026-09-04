"""Application configuration.

Everything comes from the environment or the project-root ``.env``; nothing is
hardcoded. Secrets are held as ``SecretStr`` so they cannot leak through logs,
tracebacks or ``model_dump()``, and the service account payload lives in a private
attribute so it never appears in a model dump at all.

Configuration is resolved eagerly at startup: a missing key, an unknown timezone or
an unusable service account raises here rather than at the first call that needs it.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import PrivateAttr, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

# Anchored on this file, not the working directory, so relative paths in .env resolve
# the same way whether the app is started from the repo root, a service manager, or
# a test runner in a temp directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Fields a Google service account key must carry for Phase 3 to authenticate.
_SERVICE_ACCOUNT_REQUIRED = ("client_email", "private_key", "token_uri")


class ConfigurationError(RuntimeError):
    """Raised at startup when configuration is missing, malformed or unusable."""


def _validate_service_account(info: object, source: str) -> dict[str, Any]:
    """Check the shape of a service account payload without echoing its contents.

    Error messages name only the missing fields -- never a value, and in particular
    never any part of ``private_key``.
    """
    if not isinstance(info, dict):
        raise ConfigurationError(
            f"{source} must contain a JSON object, got {type(info).__name__}."
        )
    missing = [key for key in _SERVICE_ACCOUNT_REQUIRED if not info.get(key)]
    if missing:
        raise ConfigurationError(
            f"{source} is missing required field(s): {', '.join(missing)}."
        )
    return info


class Settings(BaseSettings):
    """Typed view of the environment.

    Required keys have no default, so pydantic reports them as missing and
    :func:`get_settings` turns that into an error naming the env var.
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # .env carries entries this app does not consume (e.g. GitHub_repo_Link).
        extra="ignore",
    )

    # --- Telegram ---
    telegram_bot_token: SecretStr
    telegram_webhook_secret: SecretStr = SecretStr("")
    run_mode: Literal["polling", "webhook"] = "polling"
    public_base_url: str = ""

    # --- LLM ---
    groq_api_key: SecretStr
    groq_model: str
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_timeout_seconds: float = 12.0

    # --- Google Calendar (used from Phase 3; resolved now so failures surface early) ---
    google_calendar_id: str
    google_service_account_file: str = "credentials/service-account.json"
    google_service_account_json: SecretStr = SecretStr("")

    # --- Email (used from Phase 4; optional until then) ---
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    from_email: str = ""
    from_name: str = "BrightCare Clinic"

    # --- App ---
    timezone: str
    log_level: str = "INFO"

    # Kept private so the key cannot reach model_dump(), repr() or a traceback.
    _tz: ZoneInfo = PrivateAttr()
    _service_account_info: dict[str, Any] = PrivateAttr()

    def model_post_init(self, context: Any, /) -> None:
        self._tz = self._resolve_timezone()
        self._service_account_info = self._resolve_service_account()
        self._check_webhook_requirements()

    # ------------------------------------------------------------------ resolution

    def _resolve_timezone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigurationError(
                f"TIMEZONE={self.timezone!r} is not a known IANA time zone. "
                "Windows ships no system time zone database, so this also fails when "
                "the 'tzdata' package is missing -- install it with: pip install tzdata"
            ) from exc

    def _resolve_service_account(self) -> dict[str, Any]:
        """Resolve credentials: inline JSON first, then file, else fail."""
        inline = self.google_service_account_json.get_secret_value().strip()
        if inline:
            try:
                payload = json.loads(inline)
            except json.JSONDecodeError as exc:
                raise ConfigurationError(
                    "GOOGLE_SERVICE_ACCOUNT_JSON is set but is not valid JSON "
                    f"({exc.msg} at position {exc.pos}). In a .env file the value must "
                    "be on one line wrapped in single quotes, so the \\n escapes inside "
                    "private_key are preserved rather than expanded into real newlines."
                ) from exc
            return _validate_service_account(payload, "GOOGLE_SERVICE_ACCOUNT_JSON")

        path = Path(self.google_service_account_file)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ConfigurationError(
                    f"{path} is not valid JSON ({exc.msg} at position {exc.pos})."
                ) from exc
            return _validate_service_account(payload, str(path))

        raise ConfigurationError(
            "No Google service account credentials found. Either set "
            "GOOGLE_SERVICE_ACCOUNT_JSON to the inline JSON, or place the key file at "
            f"{path} and point GOOGLE_SERVICE_ACCOUNT_FILE at it."
        )

    def _check_webhook_requirements(self) -> None:
        """Webhook mode needs a secret and a public URL; polling needs neither."""
        if self.run_mode != "webhook":
            return
        missing = [
            name
            for name, value in (
                ("TELEGRAM_WEBHOOK_SECRET", self.telegram_webhook_secret.get_secret_value()),
                ("PUBLIC_BASE_URL", self.public_base_url),
            )
            if not value.strip()
        ]
        if missing:
            raise ConfigurationError(
                f"RUN_MODE=webhook requires {', '.join(missing)}. "
                "Set RUN_MODE=polling for local development instead."
            )

    # ------------------------------------------------------------------ accessors

    @property
    def tz(self) -> ZoneInfo:
        """The single ZoneInfo every datetime in the app is anchored to."""
        return self._tz

    @property
    def service_account_info(self) -> dict[str, Any]:
        """Parsed service account key. Never log this, or any part of it."""
        return self._service_account_info

    @property
    def service_account_email(self) -> str:
        """The client_email -- safe to log, and the address the calendar is shared with."""
        return str(self._service_account_info["client_email"])

    @property
    def email_configured(self) -> bool:
        """True when SMTP is fully configured. Phase 4 gates on this; Phase 1 ignores it."""
        return all(
            (
                self.smtp_host.strip(),
                self.smtp_port,
                self.smtp_username.strip(),
                self.smtp_password.get_secret_value().strip(),
                self.from_email.strip(),
            )
        )

    @property
    def webhook_path(self) -> str:
        """Route path Telegram posts to, including the shared secret."""
        return f"/telegram/webhook/{self.telegram_webhook_secret.get_secret_value()}"

    def safe_summary(self) -> dict[str, Any]:
        """Startup-loggable view of the config. Contains no secrets by construction."""
        return {
            "run_mode": self.run_mode,
            "groq_model": self.groq_model,
            "timezone": self.timezone,
            "log_level": self.log_level,
            "calendar_id": self.google_calendar_id,
            "service_account": self.service_account_email,
            "email_configured": self.email_configured,
            "public_base_url": self.public_base_url or None,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load settings once per process, converting pydantic errors into named keys."""
    try:
        return Settings()  # type: ignore[call-arg]  # values come from env/.env
    except ValidationError as exc:
        missing = sorted(
            {
                str(error["loc"][0]).upper()
                for error in exc.errors()
                if error["type"] == "missing" and error["loc"]
            }
        )
        if missing:
            raise ConfigurationError(
                f"Missing required environment variable(s): {', '.join(missing)}. "
                f"Copy .env.example to {PROJECT_ROOT / '.env'} and fill them in."
            ) from exc
        # Build the message from field names and reasons only -- never from the
        # rejected input, which for a SecretStr field would be the secret itself.
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']).upper()}: {error['msg']}"
            for error in exc.errors()
        )
        raise ConfigurationError(f"Invalid configuration -- {details}") from exc
