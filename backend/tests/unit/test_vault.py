"""Credential vault, key ring, provider catalog and key validation (ADR 0019, 0022)."""

from __future__ import annotations

import base64
import json
import os
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from documind.application.credentials.vault import hint_for
from documind.core.config import Settings, credential_key_ring
from documind.domain.ai.providers import PROVIDERS, clean_settings
from documind.infrastructure.ai.factory import ProviderAdapterFactory
from documind.infrastructure.security.vault import EnvelopeCipher, LocalKeyRing, VaultError
from documind.main import create_app
from tests.conftest import PRODUCTION, SettingsFactory


def _key() -> bytes:
    return os.urandom(32)


def test_seal_and_open_round_trip_and_bind_to_context() -> None:
    cipher = EnvelopeCipher(LocalKeyRing([("k1", _key())]))
    sealed = cipher.seal("sk-very-secret-value-1234", "cred-1|ws-1|ai_provider_key")
    assert "secret" not in sealed.ciphertext
    assert cipher.open(sealed, "cred-1|ws-1|ai_provider_key") == "sk-very-secret-value-1234"
    # A row copied to another credential or workspace doesn't decrypt.
    with pytest.raises(VaultError):
        cipher.open(sealed, "cred-1|ws-2|ai_provider_key")


def test_each_secret_gets_its_own_data_key() -> None:
    cipher = EnvelopeCipher(LocalKeyRing([("k1", _key())]))
    a, b = cipher.seal("same", "ctx"), cipher.seal("same", "ctx")
    assert a.ciphertext != b.ciphertext
    assert a.wrapped_dek != b.wrapped_dek


def test_tampering_is_detected() -> None:
    cipher = EnvelopeCipher(LocalKeyRing([("k1", _key())]))
    sealed = cipher.seal("secret", "ctx")
    raw = bytearray(base64.urlsafe_b64decode(sealed.ciphertext))
    raw[-1] ^= 1
    tampered = type(sealed)(base64.urlsafe_b64encode(bytes(raw)).decode(), sealed.wrapped_dek, "k1")
    with pytest.raises(VaultError):
        cipher.open(tampered, "ctx")


def test_rotation_rewraps_data_keys_and_old_key_can_then_be_removed() -> None:
    old, new = _key(), _key()
    sealed = EnvelopeCipher(LocalKeyRing([("k1", old)])).seal("secret", "ctx")
    both = EnvelopeCipher(LocalKeyRing([("k2", new), ("k1", old)]))
    assert both.open(sealed, "ctx") == "secret"  # old key still unwraps
    rewrapped = both.rewrap(sealed, "ctx")
    assert rewrapped.kek_id == "k2"
    assert rewrapped.ciphertext == sealed.ciphertext  # the secret itself isn't re-encrypted
    only_new = EnvelopeCipher(LocalKeyRing([("k2", new)]))
    assert only_new.open(rewrapped, "ctx") == "secret"
    with pytest.raises(VaultError, match="not in the key ring"):
        only_new.open(sealed, "ctx")


def test_hint_reveals_at_most_four_characters() -> None:
    assert hint_for("sk-proj-abcdefghijklmnop1234") == "…1234"
    assert hint_for("short") == "…"


def _ring(*items: tuple[str, bytes]) -> SecretStr:
    return SecretStr(",".join(f"{i}:{base64.urlsafe_b64encode(k).decode()}" for i, k in items))


def test_key_ring_parsing() -> None:
    a, b = _key(), _key()
    assert credential_key_ring(_ring(("k2", b), ("k1", a))) == [("k2", b), ("k1", a)]
    with pytest.raises(ValueError, match="32 bytes"):
        credential_key_ring(_ring(("k1", b"short")))
    with pytest.raises(ValueError, match="duplicate"):
        credential_key_ring(_ring(("k1", a), ("k1", b)))
    with pytest.raises(ValueError, match="look like"):
        credential_key_ring(SecretStr("no-separator"))


def test_production_requires_a_real_credential_key(make_settings: SettingsFactory) -> None:
    with pytest.raises(ValidationError, match="CREDENTIAL_ENCRYPTION_KEYS is required"):
        make_settings(**{**PRODUCTION, "credential_encryption_keys": None})
    dev = "dev1:" + base64.urlsafe_b64encode(b"change-me-dev-only-credential-k1").decode()
    with pytest.raises(ValidationError, match=r"CREDENTIAL_ENCRYPTION_KEYS \(dev1\)"):
        make_settings(**{**PRODUCTION, "credential_encryption_keys": dev})


def test_provider_catalog_and_settings() -> None:
    assert PROVIDERS["openai"].status == "available"
    assert all(p.status == "planned" for k, p in PROVIDERS.items() if k != "openai")
    assert clean_settings(PROVIDERS["openai"], {"chat_model": "gpt-x", "stt_model": ""}) == {
        "chat_model": "gpt-x"
    }
    with pytest.raises(ValueError, match="isn't a setting"):
        clean_settings(PROVIDERS["openai"], {"base_url": "http://169.254.169.254"})
    with pytest.raises(ValueError, match="model name"):
        clean_settings(PROVIDERS["openai"], {"chat_model": "a b"})


# ---------------------------------------------------------------- validation


KEY = "sk-proj-SECRETSECRETSECRET-9876"


def _factory(settings: Settings, handler: Any) -> ProviderAdapterFactory:
    return ProviderAdapterFactory(
        settings,
        http_client_factory=lambda: httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )


def _error(status: int, code: str | None = None, message: str = "nope") -> httpx2.Response:
    return httpx2.Response(status, json={"error": {"message": message, "type": "x", "code": code}})


async def test_validation_accepts_a_working_key(settings: Settings) -> None:
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.path)
        assert request.headers["authorization"] == f"Bearer {KEY}"
        return httpx2.Response(
            200, json={"id": "m", "object": "model", "created": 0, "owned_by": "x"}
        )

    result = await _factory(settings, handler).validate("openai", KEY, {})
    assert result.ok
    assert seen == [f"/v1/models/{settings.embedding_model}", f"/v1/models/{settings.llm_model}"]


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (
            _error(401, "invalid_api_key", f"Incorrect API key provided: {KEY}"),
            "provider_key_rejected",
        ),
        (_error(404, "model_not_found"), "model_not_available"),
        (_error(429, "insufficient_quota"), "provider_quota_exhausted"),
    ],
)
async def test_validation_errors_are_sanitized(
    settings: Settings, response: httpx2.Response, code: str
) -> None:
    result = await _factory(settings, lambda r: response).validate("openai", KEY, {})
    assert not result.ok
    assert result.code == code
    assert KEY not in (result.message or "")
    assert "9876" not in (result.message or "")


async def test_built_adapters_use_the_workspace_key(settings: Settings) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["authorization"] == f"Bearer {KEY}"
        body = json.loads(request.content)
        return httpx2.Response(
            200,
            json={
                "object": "list",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.0] * 1536}],
                "model": body["model"],
                "usage": {"prompt_tokens": 2, "total_tokens": 2},
            },
        )

    adapters = _factory(settings, handler).build("openai", KEY, {"embedding_model": "emb-x"})
    from documind.domain.billing.plans import Capability

    emb = adapters[Capability.EMBEDDING]
    assert emb.model == "emb-x"
    batch = await emb.adapter.embed(["hello"])
    assert batch.input_tokens == 2


def test_validation_errors_do_not_echo_input(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/auth/login", json={"email": "not-an-email", "password": "hunter2-secret"}
        )
    assert response.status_code == 422
    assert "hunter2" not in response.text
    assert response.json()["detail"][0]["loc"]
