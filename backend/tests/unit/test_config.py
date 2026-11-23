import pytest
from pydantic import ValidationError

from tests.conftest import SettingsFactory


def test_valid_settings(make_settings: SettingsFactory) -> None:
    s = make_settings()
    assert s.s3_bucket == "documind-test"
    assert s.docs_enabled is True
    assert s.is_production is False


def test_secrets_are_not_rendered(make_settings: SettingsFactory) -> None:
    s = make_settings()
    assert "pass" not in repr(s)
    assert "test-secret-key" not in repr(s)


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
    for key in ("database_url", "redis_url", "s3_access_key_id", "s3_secret_access_key"):
        monkeypatch.setenv(key.upper(), base[key].get_secret_value())
    monkeypatch.setenv("S3_BUCKET", "documind-test")
    s = Settings(_env_file=None)
    assert s.cors_allowed_origins == ["http://a.test", "http://b.test"]


def test_production_rejects_placeholder_credentials(make_settings: SettingsFactory) -> None:
    with pytest.raises(ValidationError, match="S3_SECRET_ACCESS_KEY"):
        make_settings(environment="production", s3_secret_access_key="change-me-dev-only")


def test_production_rejects_wildcard_cors(make_settings: SettingsFactory) -> None:
    with pytest.raises(ValidationError, match="Wildcard CORS"):
        make_settings(environment="production", cors_allowed_origins="*")


def test_docs_disabled_by_default_in_production(make_settings: SettingsFactory) -> None:
    assert make_settings(environment="production").docs_enabled is False
    assert make_settings(environment="production", api_docs_enabled=True).docs_enabled is True
    assert make_settings(api_docs_enabled=False).docs_enabled is False
