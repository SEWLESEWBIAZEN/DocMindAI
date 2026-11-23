import pytest
from pydantic import ValidationError

from tests.conftest import PRODUCTION, SettingsFactory


def test_valid_settings(make_settings: SettingsFactory) -> None:
    s = make_settings()
    assert s.s3_bucket == "documind-test"
    assert s.docs_enabled is True
    assert s.is_production is False


def test_secrets_are_not_rendered(make_settings: SettingsFactory) -> None:
    s = make_settings()
    rendered = repr(s) + str(s.model_dump())
    for secret in ("user:pass@", "test-secret-key", "test-jwt-secret-key"):
        assert secret not in rendered


def test_database_url_requires_asyncpg_driver(make_settings: SettingsFactory) -> None:
    with pytest.raises(ValidationError, match="postgresql\\+asyncpg"):
        make_settings(database_url="postgresql://u:p@localhost/db")


def test_redis_url_scheme_is_validated(make_settings: SettingsFactory) -> None:
    with pytest.raises(ValidationError, match="REDIS_URL"):
        make_settings(redis_url="http://localhost:6379")


@pytest.mark.parametrize("bucket", ["UPPER", "ab", "under_score", "-leading"])
def test_invalid_bucket_names_rejected(make_settings: SettingsFactory, bucket: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(s3_bucket=bucket)


def test_cors_origins_accept_comma_separated_string(make_settings: SettingsFactory) -> None:
    s = make_settings(cors_allowed_origins="http://a.test, http://b.test ,")
    assert s.cors_allowed_origins == ["http://a.test", "http://b.test"]


def test_cors_origins_from_environment(
    monkeypatch: pytest.MonkeyPatch, make_settings: SettingsFactory
) -> None:
    from documind.core.config import Settings

    base = make_settings().model_dump()
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "http://a.test,http://b.test")
    for key in (
        "database_url",
        "redis_url",
        "s3_access_key_id",
        "s3_secret_access_key",
        "jwt_secret_key",
        "mfa_encryption_key",
    ):
        monkeypatch.setenv(key.upper(), base[key].get_secret_value())
    monkeypatch.setenv("S3_BUCKET", "documind-test")
    s = Settings(_env_file=None)
    assert s.cors_allowed_origins == ["http://a.test", "http://b.test"]


def test_production_rejects_placeholder_credentials(make_settings: SettingsFactory) -> None:
    with pytest.raises(ValidationError, match="S3_SECRET_ACCESS_KEY"):
        make_settings(**PRODUCTION, s3_secret_access_key="change-me-dev-only")


def test_production_rejects_wildcard_cors(make_settings: SettingsFactory) -> None:
    with pytest.raises(ValidationError, match="Wildcard CORS"):
        make_settings(**PRODUCTION, cors_allowed_origins="*")


def test_docs_disabled_by_default_in_production(make_settings: SettingsFactory) -> None:
    assert make_settings(**PRODUCTION).docs_enabled is False
    assert make_settings(**PRODUCTION, api_docs_enabled=True).docs_enabled is True
    assert make_settings(api_docs_enabled=False).docs_enabled is False


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"jwt_secret_key": "change-me-dev-only-0123456789abcdef0123"}, "JWT_SECRET_KEY"),
        ({"auth_cookie_secure": False}, "AUTH_COOKIE_SECURE"),
        ({"public_app_url": "http://app.example.com"}, "PUBLIC_APP_URL"),
        ({"smtp_starttls": False}, "SMTP must use"),
    ],
)
def test_production_auth_safeguards(
    make_settings: SettingsFactory, override: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        make_settings(**{**PRODUCTION, **override})


def test_jwt_secret_minimum_length(make_settings: SettingsFactory) -> None:
    with pytest.raises(ValidationError):
        make_settings(jwt_secret_key="too-short")


def test_smtp_tls_modes_are_exclusive(make_settings: SettingsFactory) -> None:
    with pytest.raises(ValidationError, match="either"):
        make_settings(smtp_starttls=True, smtp_use_ssl=True)


def test_mfa_key_must_be_32_bytes(make_settings: SettingsFactory) -> None:
    with pytest.raises(ValidationError, match="32 bytes"):
        make_settings(mfa_encryption_key="dG9vLXNob3J0")  # "too-short"
    with pytest.raises(ValidationError, match="32 bytes"):
        make_settings(mfa_encryption_key="not base64 !!")


def test_production_rejects_placeholder_mfa_key(make_settings: SettingsFactory) -> None:
    with pytest.raises(ValidationError, match="MFA_ENCRYPTION_KEY"):
        make_settings(
            **PRODUCTION, mfa_encryption_key="Y2hhbmdlLW1lLWRldi1vbmx5LW1mYS1rZXktMDAwMDA="
        )
