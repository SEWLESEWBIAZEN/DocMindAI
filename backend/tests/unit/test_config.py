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
    monkeypatch.setenv("EMBEDDING_PROVIDER", "local")
    monkeypatch.setenv("LLM_PROVIDER", "local")
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


def test_openai_provider_requires_key(make_settings: SettingsFactory) -> None:
    with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
        make_settings(embedding_provider="openai")
    configured = make_settings(embedding_provider="openai", openai_api_key="sk-x")
    assert configured.embedding_provider == "openai"


def test_production_rejects_local_embeddings(make_settings: SettingsFactory) -> None:
    with pytest.raises(ValidationError, match="development only"):
        make_settings(**{**PRODUCTION, "embedding_provider": "local"})


def test_llm_provider_requires_key_and_is_openai_in_production(
    make_settings: SettingsFactory,
) -> None:
    with pytest.raises(ValidationError, match="LLM_PROVIDER=openai"):
        make_settings(llm_provider="openai")
    assert make_settings(llm_provider="openai", openai_api_key="sk-x").llm_model == "gpt-5.4-mini"
    with pytest.raises(ValidationError, match="LLM_PROVIDER=local"):
        make_settings(**{**PRODUCTION, "llm_provider": "local"})


def test_llm_temperature_optional(make_settings: SettingsFactory) -> None:
    assert make_settings().llm_temperature is None
    assert make_settings(llm_temperature=0.2).llm_temperature == 0.2
    with pytest.raises(ValidationError):
        make_settings(llm_temperature=3)


def test_embedding_dimensions_fixed_by_schema(make_settings: SettingsFactory) -> None:
    with pytest.raises(ValidationError, match="1536"):
        make_settings(embedding_dimensions=3072)


def test_chunk_overlap_bound(make_settings: SettingsFactory) -> None:
    with pytest.raises(ValidationError, match="CHUNK_OVERLAP_TOKENS"):
        make_settings(chunk_max_tokens=100, chunk_overlap_tokens=60)


def test_broker_defaults_to_redis(make_settings: SettingsFactory) -> None:
    assert make_settings().broker_url == "redis://127.0.0.1:1/0"
    custom = make_settings(celery_broker_url="redis://broker:6379/2")
    assert custom.broker_url == "redis://broker:6379/2"


def test_claude_for_answers_needs_its_key(make_settings: SettingsFactory) -> None:
    from pydantic import SecretStr

    with pytest.raises(ValidationError, match="ANTHROPIC_API_KEY"):
        make_settings(llm_provider="anthropic", anthropic_api_key=None)
    s = make_settings(
        llm_provider="anthropic",
        llm_model="claude-opus-5-5",
        anthropic_api_key=SecretStr("sk-test"),
        anthropic_base_url="",  # Compose passes unset variables as empty strings
    )
    assert s.anthropic_base_url is None
    assert "sk-test" not in repr(s)


def test_claude_with_figures_needs_an_openai_vision_model(make_settings: SettingsFactory) -> None:
    from pydantic import SecretStr

    with pytest.raises(ValidationError, match="VISION_MODEL"):
        make_settings(
            llm_provider="anthropic",
            anthropic_api_key=SecretStr("sk-test"),
            figure_descriptions_enabled=True,
            openai_api_key=SecretStr("sk-openai"),
        )
