"""Bring your own API key against the real stack (ADR 0019, 0022).

A fake provider factory stands in for OpenAI (no network); everything else is
real: the vault, the database, the resolver, the API, the worker orchestrator.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest

from documind.application.ai.ports import ValidationResult
from documind.application.ai.resolver import CapabilityAdapter
from documind.application.ports import (
    ChatMessage,
    LLMResult,
    LLMStreamEvent,
)
from documind.domain.billing.plans import Capability
from documind.domain.processing.errors import TransientProcessingError
from documind.infrastructure.embeddings.local_provider import LocalHashingEmbeddingProvider
from documind.infrastructure.llm.local_provider import LocalExtractiveLLM
from tests.integration.conftest import Api
from tests.integration.test_documents_api import upload
from tests.integration.test_processing_pipeline import sql, ws
from tests.integration.test_usage_and_quotas import POLICY, assign, run_metered_job

pytestmark = pytest.mark.integration

KEY = "sk-proj-WORKSPACEKEY-abcdefghijklmnop-7f3e"
BAD = "sk-bad-REJECTEDKEY-abcdefghijklmnop-0000"


class ByokEmbeddings(LocalHashingEmbeddingProvider):
    model = "byok-embed"


class ByokLLM:
    """Answers like the local model; can be switched to "the provider rejects the key"."""

    def __init__(self, state: dict[str, Any]) -> None:
        self._state = state
        self._inner = LocalExtractiveLLM()

    @property
    def model(self) -> str:
        return "byok-chat"

    def _check(self) -> None:
        self._state["calls"] += 1
        if self._state["revoked"]:
            raise TransientProcessingError(
                "The AI service is misconfigured.", code="llm_misconfigured"
            )

    async def generate(
        self, messages: list[ChatMessage], *, max_output_tokens: int, temperature: float | None
    ) -> LLMResult:
        self._check()
        result = await self._inner.generate(
            messages, max_output_tokens=max_output_tokens, temperature=temperature
        )
        return LLMResult(result.text, result.usage, self.model)

    async def stream(
        self, messages: list[ChatMessage], *, max_output_tokens: int, temperature: float | None
    ) -> AsyncIterator[LLMStreamEvent]:
        self._check()
        async for event in self._inner.stream(
            messages, max_output_tokens=max_output_tokens, temperature=temperature
        ):
            yield event


class FakeFactory:
    def __init__(self) -> None:
        self.state: dict[str, Any] = {"revoked": False, "calls": 0, "secrets": []}

    def models(self, provider: str, options: Mapping[str, str]) -> dict[Capability, str]:
        return {Capability.EMBEDDING: "byok-embed", Capability.CHAT: "byok-chat"}

    def embedding_model(self, provider: str, options: Mapping[str, str]) -> str:
        return "byok-embed"

    def build(
        self, provider: str, secret: str, options: Mapping[str, str]
    ) -> dict[Capability, CapabilityAdapter]:
        self.state["secrets"].append(secret)
        return {
            Capability.EMBEDDING: CapabilityAdapter("openai", ByokEmbeddings(1536), "byok-embed"),
            Capability.CHAT: CapabilityAdapter("openai", ByokLLM(self.state), "byok-chat"),
        }

    async def validate(
        self, provider: str, secret: str, options: Mapping[str, str]
    ) -> ValidationResult:
        if secret.startswith("sk-bad"):
            return ValidationResult(False, "provider_key_rejected", "OpenAI rejected this key.")
        return ValidationResult(True)


class PlatformSpy(LocalExtractiveLLM):
    calls = 0

    async def generate(self, *args: Any, **kwargs: Any) -> LLMResult:
        PlatformSpy.calls += 1
        return await super().generate(*args, **kwargs)


@pytest.fixture
def factory() -> FakeFactory:
    return FakeFactory()


class _Worker:
    """Container overrides for jobs run in tests (the worker uses the same fake provider)."""

    overrides: dict[str, Any] | None = None

    def get(self) -> dict[str, Any] | None:
        return self.overrides


WORKER = _Worker()


@pytest.fixture
def bapi(make_api: Any, factory: FakeFactory) -> Api:
    PlatformSpy.calls = 0
    WORKER.overrides = {"provider_factory": factory, "llm_provider": PlatformSpy()}
    api: Api = make_api({"provider_factory": factory, "llm_provider": PlatformSpy()})
    return api


def url(w: str, tail: str = "") -> str:
    return f"/api/v1/workspaces/{w}/ai-providers{tail}"


def ask(api: Api, w: str, h: dict[str, str]) -> Any:
    return api.client.post(
        f"/api/v1/workspaces/{w}/ask", json={"question": "How much notice is needed?"}, headers=h
    )


def owner_with_document(api: Api) -> tuple[str, dict[str, str], dict[str, str]]:
    h = api.signup("owner@example.com")
    w = ws(api, h)
    upload(api, w, h, POLICY, "policy.md")
    assert run_metered_job(api, api.dispatcher.dispatched[-1], WORKER.get()).status == "succeeded"
    secret, _ = api.enable_mfa(h)
    return w, h, api.step_up(h, secret)


def add_key(api: Api, w: str, elevated: dict[str, str]) -> None:
    saved = api.client.put(
        url(w, "/openai"), json={"api_key": KEY, "reindex": True}, headers=elevated
    )
    assert saved.status_code == 200, saved.text
    assert run_metered_job(api, api.dispatcher.dispatched[-1], WORKER.get()).status == "succeeded"


def test_adding_a_key_needs_2fa_validation_and_reindex_confirmation(
    bapi: Api, factory: FakeFactory
) -> None:
    api = bapi
    h = api.signup("owner@example.com")
    w = ws(api, h)
    upload(api, w, h, POLICY, "policy.md")
    assert run_metered_job(api, api.dispatcher.dispatched[-1], WORKER.get()).status == "succeeded"
    body = {"api_key": KEY}

    no_mfa = api.client.put(url(w, "/openai"), json=body, headers=h)
    assert no_mfa.status_code == 403
    assert no_mfa.json()["code"] == "mfa_enrollment_required"
    secret, _ = api.enable_mfa(h)
    stale = api.client.put(url(w, "/openai"), json=body, headers=h)
    assert stale.json()["code"] == "mfa_step_up_required"
    elevated = api.step_up(h, secret)

    rejected = api.client.put(url(w, "/openai"), json={"api_key": BAD}, headers=elevated)
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "provider_key_rejected"
    assert "REJECTEDKEY" not in rejected.text

    needs_reindex = api.client.put(url(w, "/openai"), json=body, headers=elevated)
    assert needs_reindex.status_code == 409
    assert needs_reindex.json()["code"] == "reindex_required"

    before = len(api.dispatcher.dispatched)
    saved = api.client.put(url(w, "/openai"), json={**body, "reindex": True}, headers=elevated)
    assert saved.status_code == 200, saved.text
    view = saved.json()
    assert view["hint"] == "…7f3e"
    assert view["status"] == "active"
    assert KEY not in saved.text
    assert len(api.dispatcher.dispatched) == before + 1  # the document is re-indexed

    # Stored encrypted; nothing readable in the database or the audit log.
    rows = sql(api, "SELECT ciphertext, wrapped_dek, hint, status FROM credentials")
    assert len(rows) == 1
    assert "WORKSPACEKEY" not in rows[0].ciphertext
    audit = " ".join(
        str(r.details)
        for r in sql(api, "SELECT details FROM audit_logs WHERE action LIKE 'ai_provider.%'")
    )
    assert "WORKSPACEKEY" not in audit
    actions = {r.action for r in sql(api, "SELECT action FROM audit_logs")}
    assert {"ai_provider.validation_failed", "ai_provider.key_added"} <= actions

    overview = api.client.get(url(w), headers=h).json()
    assert overview["effective"]["chat"] == "workspace_key"
    assert overview["effective"]["embedding"] == "workspace_key"
    assert overview["embedding_model"] == "byok-embed"
    assert [c["key"] for c in overview["catalog"] if c["status"] == "available"] == ["openai"]

    # The re-index runs in the worker with the workspace key, then search works again.
    assert run_metered_job(api, api.dispatcher.dispatched[-1], WORKER.get()).status == "succeeded"
    assert factory.state["secrets"][-1] == KEY  # decrypted in memory for the call
    models = {r.embedding_model for r in sql(api, "SELECT embedding_model FROM document_chunks")}
    assert models == {"byok-embed"}
    hits = api.client.post(f"/api/v1/workspaces/{w}/search", json={"query": "notice"}, headers=h)
    assert hits.json()["results"]


def test_workspace_key_usage_is_attributed_and_outside_platform_quotas(bapi: Api) -> None:
    api = bapi
    w, h, elevated = owner_with_document(api)
    add_key(api, w, elevated)
    assign(api, w, "free", chat_tokens=1)
    for _ in range(2):  # the platform quota doesn't apply to the workspace's own key
        answer = ask(api, w, h)
        assert answer.status_code == 200
        assert answer.json()["model"] == "byok-chat", answer.text
    chat = sql(
        api,
        "SELECT billed_to, credential_id, provider FROM usage_records WHERE capability = 'chat'",
    )
    assert {r.billed_to for r in chat} == {"workspace_key"}
    assert all(r.credential_id is not None for r in chat)
    assert PlatformSpy.calls == 0


def test_rejected_key_fails_clearly_and_never_falls_back(bapi: Api, factory: FakeFactory) -> None:
    api = bapi
    w, h, elevated = owner_with_document(api)
    add_key(api, w, elevated)
    factory.state["revoked"] = True

    first = ask(api, w, h)
    assert first.status_code == 503
    assert first.json()["code"] == "provider_key_invalid"
    calls = factory.state["calls"]
    second = ask(api, w, h)
    assert second.json()["code"] == "provider_key_invalid"
    assert factory.state["calls"] == calls  # the provider isn't called again
    assert PlatformSpy.calls == 0  # and the platform key is never used instead

    overview = api.client.get(url(w), headers=h).json()
    assert overview["providers"][0]["status"] == "invalid"
    assert overview["providers"][0]["last_error_code"] == "llm_misconfigured"
    assert overview["effective"]["chat"] == "provider_key_invalid"
    assert any("stopped working" in m.subject for m in api.mail.to("owner@example.com"))
    assert "ai_provider.key_rejected" in {
        r.action for r in sql(api, "SELECT action FROM audit_logs")
    }

    # Background jobs fail permanently instead of retrying forever.
    second_upload = upload(api, w, h, POLICY + b"\nAppendix.", "second.md")
    assert second_upload.status_code == 201, second_upload.text
    outcome = run_metered_job(api, api.dispatcher.dispatched[-1], WORKER.get())
    assert outcome.status == "failed"
    job = sql(api, "SELECT error_code FROM processing_jobs ORDER BY created_at DESC LIMIT 1")[0]
    assert job.error_code == "provider_key_invalid"

    # Fixed at the provider: revalidate brings the key back.
    factory.state["revoked"] = False
    assert api.client.post(url(w, "/openai/validate"), headers=h).json()["status"] == "active"
    assert ask(api, w, h).status_code == 200


def test_key_only_blocks_the_platform(bapi: Api) -> None:
    api = bapi
    w, h, elevated = owner_with_document(api)
    on = api.client.put(f"/api/v1/workspaces/{w}/ai-settings", json={"key_only": True}, headers=h)
    assert on.json() == {"key_only": True}
    blocked = ask(api, w, h)
    assert blocked.status_code == 503
    assert blocked.json()["code"] == "provider_key_required"
    assert PlatformSpy.calls == 0
    # Loosening it again requires a fresh 2FA check.
    h2 = {"Authorization": h["Authorization"]}
    assert (
        api.client.put(
            f"/api/v1/workspaces/{w}/ai-settings", json={"key_only": False}, headers=h2
        ).json()["code"]
        == "mfa_step_up_required"
    )
    assert api.client.put(
        f"/api/v1/workspaces/{w}/ai-settings", json={"key_only": False}, headers=elevated
    ).json() == {"key_only": False}
    assert ask(api, w, h).status_code == 200


def test_removing_the_key_returns_to_the_platform_after_reindex(bapi: Api) -> None:
    api = bapi
    w, h, elevated = owner_with_document(api)
    api.client.put(url(w, "/openai"), json={"api_key": KEY, "reindex": True}, headers=elevated)
    assert run_metered_job(api, api.dispatcher.dispatched[-1], WORKER.get()).status == "succeeded"
    assert (
        api.client.delete(url(w, "/openai"), headers=elevated).json()["code"] == "reindex_required"
    )
    before = len(api.dispatcher.dispatched)
    assert api.client.delete(url(w, "/openai?reindex=true"), headers=elevated).status_code == 204
    assert len(api.dispatcher.dispatched) == before + 1
    cred = sql(api, "SELECT status, ciphertext FROM credentials")[0]
    assert cred.status == "revoked"
    assert cred.ciphertext == ""  # destroyed, not just flagged
    assert api.client.get(url(w), headers=h).json()["effective"]["chat"] == "platform"
    assert run_metered_job(api, api.dispatcher.dispatched[-1], WORKER.get()).status == "succeeded"
    estimate = api.client.get(f"/api/v1/workspaces/{w}/reindex", headers=h).json()
    assert estimate["required"] is False
    assert estimate["target_model"] == "local-hashing-v1"


def test_only_owners_and_admins_manage_keys(bapi: Api) -> None:
    api = bapi
    owner = api.signup("owner@example.com")
    org = str(api.create_org(owner, "Acme")["id"])
    member = api.add_member(org, owner, "member@example.com", "member")
    assert api.client.get(url(org), headers=member).status_code == 403
    outsider = api.signup("eve@example.com")
    assert api.client.get(url(org), headers=outsider).status_code == 404
    admin = api.add_member(org, owner, "admin@example.com", "admin")
    assert api.client.get(url(org), headers=admin).status_code == 200


def test_planned_providers_are_refused(bapi: Api) -> None:
    api = bapi
    w, _, elevated = owner_with_document(api)
    response = api.client.put(url(w, "/anthropic"), json={"api_key": KEY}, headers=elevated)
    assert response.status_code == 422
    assert response.json()["code"] == "provider_not_available"
    assert (
        api.client.put(url(w, "/nope"), json={"api_key": KEY}, headers=elevated).status_code == 404
    )


def test_vault_key_rotation_rewrap(bapi: Api) -> None:
    """Rewrap with a new primary key; the old key can then be removed."""
    import base64
    import os

    from pydantic import SecretStr

    from documind.application.credentials.vault import CredentialVault
    from documind.container import build_container

    api = bapi
    w, _h, elevated = owner_with_document(api)
    api.client.put(url(w, "/openai"), json={"api_key": KEY, "reindex": True}, headers=elevated)
    old = api.settings.credential_encryption_keys
    assert old is not None
    new = "k9:" + base64.urlsafe_b64encode(os.urandom(32)).decode()

    async def rewrap(ring: str) -> tuple[int, int]:
        c = build_container(
            api.settings.model_copy(update={"credential_encryption_keys": SecretStr(ring)})
        )
        try:
            async with c.session_factory() as session:
                return await CredentialVault(session, c.cipher).rewrap_all()
        finally:
            await c.aclose()

    assert asyncio.run(rewrap(f"{new},{old.get_secret_value()}")) == (1, 0)
    assert sql(api, "SELECT kek_id FROM credentials")[0].kek_id == "k9"

    async def reveal_with_new_only() -> str:
        c = build_container(
            api.settings.model_copy(update={"credential_encryption_keys": SecretStr(new)})
        )
        try:
            async with c.session_factory() as session:
                vault = CredentialVault(session, c.cipher)
                row = (
                    await session.execute(
                        __import__("sqlalchemy").text("SELECT id FROM credentials")
                    )
                ).one()
                cred = await vault.get(uuid.UUID(w), row.id)
                assert cred is not None
                return vault.reveal(cred)
        finally:
            await c.aclose()

    assert asyncio.run(reveal_with_new_only()) == KEY
