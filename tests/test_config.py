"""Configuration: fail-fast behaviour, credential resolution order, secret hygiene."""

from __future__ import annotations

import json

import pytest

from app import config as config_module
from app.config import ConfigurationError, Settings

SERVICE_ACCOUNT = {
    "type": "service_account",
    "project_id": "test-project",
    "private_key": "-----BEGIN PRIVATE KEY-----\nnotarealkey\n-----END PRIVATE KEY-----\n",
    "client_email": "bot@test-project.iam.gserviceaccount.com",
    "token_uri": "https://oauth2.googleapis.com/token",
}

REQUIRED = {
    "telegram_bot_token": "123:ABC",
    "groq_api_key": "gsk_test",
    "groq_model": "llama-3.3-70b-versatile",
    "google_calendar_id": "clinic@example.com",
    "timezone": "Asia/Kolkata",
}


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's real environment out of these tests."""
    for key in (
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET", "RUN_MODE", "PUBLIC_BASE_URL",
        "GROQ_API_KEY", "GROQ_MODEL", "GOOGLE_CALENDAR_ID",
        "GOOGLE_SERVICE_ACCOUNT_FILE", "GOOGLE_SERVICE_ACCOUNT_JSON",
        "SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "FROM_EMAIL",
        "TIMEZONE", "LOG_LEVEL",
    ):
        monkeypatch.delenv(key, raising=False)


def make_settings(**overrides: object) -> Settings:
    """Build Settings from explicit values only, ignoring any .env on disk."""
    values = {
        **REQUIRED,
        "google_service_account_json": json.dumps(SERVICE_ACCOUNT),
        **overrides,
    }
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


# ----------------------------------------------------------------- fail fast

@pytest.mark.parametrize("missing", sorted(REQUIRED))
def test_missing_required_key_is_named_in_the_error(missing: str) -> None:
    """A stuck operator needs the env var name, not a pydantic field trace."""
    values = {key: value for key, value in REQUIRED.items() if key != missing}

    with pytest.raises(ConfigurationError) as exc_info:
        config_module.load_settings(
            _env_file=None,
            google_service_account_json=json.dumps(SERVICE_ACCOUNT),
            **values,
        )

    message = str(exc_info.value)
    assert "Missing required environment variable(s)" in message
    assert missing.upper() in message


def test_all_missing_keys_are_reported_at_once() -> None:
    """Reporting one at a time would mean five restarts to find five gaps."""
    with pytest.raises(ConfigurationError) as exc_info:
        config_module.load_settings(_env_file=None)

    message = str(exc_info.value)
    for key in REQUIRED:
        assert key.upper() in message


def test_unknown_timezone_fails_at_startup() -> None:
    with pytest.raises(ConfigurationError, match="not a known IANA time zone"):
        make_settings(timezone="Mars/Olympus_Mons")


def test_extra_env_keys_are_ignored() -> None:
    """.env carries GitHub_repo_Link, which this app does not consume."""
    settings = make_settings(github_repo_link="https://github.com/example/repo")
    assert settings.groq_model == REQUIRED["groq_model"]


# --------------------------------------------- service account resolution order

def test_inline_json_takes_precedence_over_the_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config_module, "PROJECT_ROOT", tmp_path)
    other = {**SERVICE_ACCOUNT, "client_email": "file@test.iam.gserviceaccount.com"}
    (tmp_path / "sa.json").write_text(json.dumps(other), encoding="utf-8")

    settings = make_settings(google_service_account_file="sa.json")

    assert settings.service_account_email == SERVICE_ACCOUNT["client_email"]


def test_file_is_used_when_inline_json_is_absent(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config_module, "PROJECT_ROOT", tmp_path)
    (tmp_path / "sa.json").write_text(json.dumps(SERVICE_ACCOUNT), encoding="utf-8")

    settings = make_settings(
        google_service_account_json="", google_service_account_file="sa.json"
    )

    assert settings.service_account_email == SERVICE_ACCOUNT["client_email"]


def test_relative_file_resolves_against_project_root_not_cwd(
    tmp_path, monkeypatch
) -> None:
    """The app must start identically from any working directory."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "sa.json").write_text(json.dumps(SERVICE_ACCOUNT), encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    monkeypatch.setattr(config_module, "PROJECT_ROOT", project)
    monkeypatch.chdir(elsewhere)

    settings = make_settings(
        google_service_account_json="", google_service_account_file="sa.json"
    )

    assert settings.service_account_email == SERVICE_ACCOUNT["client_email"]


def test_missing_credentials_raise_at_startup(tmp_path, monkeypatch) -> None:
    """Not deferred to the first calendar call."""
    monkeypatch.setattr(config_module, "PROJECT_ROOT", tmp_path)

    with pytest.raises(ConfigurationError, match="No Google service account"):
        make_settings(
            google_service_account_json="", google_service_account_file="absent.json"
        )


def test_malformed_inline_json_explains_the_quoting_trap() -> None:
    with pytest.raises(ConfigurationError, match="single quotes"):
        make_settings(google_service_account_json="{not json")


def test_service_account_missing_a_field_is_rejected() -> None:
    incomplete = {key: value for key, value in SERVICE_ACCOUNT.items() if key != "private_key"}

    with pytest.raises(ConfigurationError, match="private_key"):
        make_settings(google_service_account_json=json.dumps(incomplete))


# ------------------------------------------------------------- secret hygiene

def test_secrets_never_appear_in_repr_or_dump() -> None:
    settings = make_settings()

    rendered = repr(settings) + json.dumps(settings.model_dump(), default=str)

    assert "gsk_test" not in rendered
    assert "123:ABC" not in rendered
    assert "PRIVATE KEY" not in rendered


def test_safe_summary_carries_no_secrets() -> None:
    summary = json.dumps(make_settings().safe_summary())

    assert "gsk_test" not in summary
    assert "PRIVATE KEY" not in summary
    assert SERVICE_ACCOUNT["client_email"] in summary  # identity is safe and useful


# ------------------------------------------------------------- derived settings

def test_email_configured_is_false_without_smtp() -> None:
    assert make_settings().email_configured is False


def test_email_configured_is_true_once_smtp_is_complete() -> None:
    settings = make_settings(
        smtp_host="smtp.gmail.com",
        smtp_username="clinic@example.com",
        smtp_password="apppassword16chr",
        from_email="clinic@example.com",
    )

    assert settings.email_configured is True


def test_webhook_mode_requires_a_secret_and_public_url() -> None:
    with pytest.raises(ConfigurationError, match="TELEGRAM_WEBHOOK_SECRET"):
        make_settings(run_mode="webhook")


def test_webhook_mode_accepts_a_complete_configuration() -> None:
    settings = make_settings(
        run_mode="webhook",
        telegram_webhook_secret="s3cret",
        public_base_url="https://example.com",
    )

    assert settings.webhook_path == "/telegram/webhook/s3cret"


def test_polling_mode_needs_neither() -> None:
    assert make_settings(run_mode="polling").run_mode == "polling"
