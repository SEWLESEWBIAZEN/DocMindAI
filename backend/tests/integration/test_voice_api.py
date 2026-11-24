"""Voice policy, speech endpoints and reading-mode summaries."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from documind.application.ports import Transcript
from documind.domain.processing.errors import TransientProcessingError
from tests.integration.conftest import Api
from tests.integration.test_documents_api import upload
from tests.integration.test_processing_pipeline import contract_pdf, sql, ws
from tests.integration.test_rag_api import POLICY, ingest

pytestmark = pytest.mark.integration

WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 2000  # EBML header + padding


class FakeSpeech:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.transcribed: list[tuple[int, str, str | None]] = []
        self.spoken: list[tuple[str, str | None, float]] = []

    async def transcribe(
        self, audio: bytes, *, media_type: str, language: str | None = None
    ) -> Transcript:
        if self.fail:
            raise TransientProcessingError("down", code="llm_unavailable")
        self.transcribed.append((len(audio), media_type, language))
        return Transcript("Who reviews large renewals?", language)

    def synthesize(
        self, text: str, *, voice: str | None = None, speed: float = 1.0, audio_format: str = "mp3"
    ) -> AsyncIterator[bytes]:
        async def gen() -> AsyncIterator[bytes]:
            if self.fail:
                raise TransientProcessingError("down", code="llm_unavailable")
            self.spoken.append((text, voice, speed))
            yield b"ID3fake-mp3-"
            yield text.encode()[:20]

        return gen()


def voice_url(workspace: str, tail: str = "") -> str:
    return f"/api/v1/workspaces/{workspace}/voice{tail}"


@pytest.fixture
def speech() -> FakeSpeech:
    return FakeSpeech()


@pytest.fixture
def vapi(make_api: Any, speech: FakeSpeech) -> Api:
    api: Api = make_api({"speech_to_text": speech, "text_to_speech": speech})
    return api


def transcribe(
    api: Api,
    workspace: str,
    headers: dict[str, str],
    data: bytes = WEBM,
    ctype: str = "audio/webm;codecs=opus",
    **form: str,
) -> Any:
    return api.client.post(
        voice_url(workspace, "/transcribe"),
        files={"audio": ("q.webm", data, ctype)},
        data=form,
        headers=headers,
    )


def test_settings_reflect_policy_and_provider(vapi: Api, api: Api) -> None:
    h = vapi.signup("ada@example.com")
    w = ws(vapi, h)
    body = vapi.client.get(voice_url(w), headers=h).json()
    assert body["policy"] == "enabled"
    assert body["server_speech"] is True
    assert "alloy" in body["voices"]
    # Without a configured provider, browser speech is all that's left.
    h2 = api.signup("bob@example.com")
    plain = api.client.get(voice_url(ws(api, h2)), headers=h2).json()
    assert (plain["provider_configured"], plain["server_speech"], plain["voices"]) == (
        False,
        False,
        [],
    )
    r = transcribe(api, ws(api, h2), h2)
    assert r.status_code == 503
    assert r.json()["code"] == "speech_not_configured"


def test_transcribe_and_speak_store_nothing(vapi: Api, speech: FakeSpeech) -> None:
    h = vapi.signup("ada@example.com")
    w = ws(vapi, h)
    before = sql(vapi, "SELECT (SELECT count(*) FROM documents), (SELECT count(*) FROM messages)")[
        0
    ]
    r = transcribe(vapi, w, h, language="en")
    assert r.status_code == 200, r.text
    assert r.json() == {"text": "Who reviews large renewals?", "language": "en"}
    assert speech.transcribed == [(len(WEBM), "audio/webm", "en")]

    r = vapi.client.post(
        voice_url(w, "/speech"),
        json={"text": "Notice is ninety days.", "voice": "nova", "speed": 1.25},
        headers=h,
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/mpeg"
    assert r.headers["cache-control"] == "no-store"
    assert r.content.startswith(b"ID3fake-mp3-")
    assert speech.spoken == [("Notice is ninety days.", "nova", 1.25)]
    after = sql(vapi, "SELECT (SELECT count(*) FROM documents), (SELECT count(*) FROM messages)")[0]
    assert tuple(after) == tuple(before)


@pytest.mark.parametrize(
    ("data", "ctype", "status", "code"),
    [
        (WEBM, "video/mp4", 415, "unsupported_file"),
        (b"", "audio/webm", 422, "empty_audio"),
        (b"x" * (10 * 1024 * 1024 + 10), "audio/webm", 413, "payload_too_large"),
    ],
)
def test_transcribe_validation(vapi: Api, data: bytes, ctype: str, status: int, code: str) -> None:
    h = vapi.signup("ada@example.com")
    r = transcribe(vapi, ws(vapi, h), h, data, ctype)
    assert r.status_code == status, r.text
    assert r.json()["code"] == code


@pytest.mark.parametrize(
    "body",
    [
        {"text": ""},
        {"text": "x" * 4097},
        {"text": "ok", "speed": 3},
        {"text": "ok", "voice": "robot"},
    ],
)
def test_speech_validation(vapi: Api, body: dict[str, Any]) -> None:
    h = vapi.signup("ada@example.com")
    assert (
        vapi.client.post(voice_url(ws(vapi, h), "/speech"), json=body, headers=h).status_code == 422
    )


def test_provider_failure_is_503_before_audio(make_api: Any) -> None:
    failing = FakeSpeech(fail=True)
    api: Api = make_api({"speech_to_text": failing, "text_to_speech": failing})
    h = api.signup("ada@example.com")
    w = ws(api, h)
    assert transcribe(api, w, h).json()["code"] == "speech_unavailable"
    r = api.client.post(voice_url(w, "/speech"), json={"text": "hello"}, headers=h)
    assert r.status_code == 503
    assert r.json()["code"] == "speech_unavailable"


def test_policy_is_admin_only_audited_and_enforced(vapi: Api, speech: FakeSpeech) -> None:
    owner = vapi.signup("owner@example.com")
    org = vapi.create_org(owner)["id"]
    member = vapi.add_member(org, owner, "member@example.com", "member")
    assert (
        vapi.client.put(voice_url(org), json={"policy": "browser"}, headers=member).status_code
        == 403
    )

    r = vapi.client.put(voice_url(org), json={"policy": "browser"}, headers=owner)
    assert r.json()["policy"] == "browser"
    assert r.json()["server_speech"] is False
    assert vapi.client.get(voice_url(org), headers=member).json()["policy"] == "browser"
    assert transcribe(vapi, org, member).json()["code"] == "server_voice_not_allowed"

    vapi.client.put(voice_url(org), json={"policy": "disabled"}, headers=owner)
    r = vapi.client.post(voice_url(org, "/speech"), json={"text": "hi"}, headers=member)
    assert r.status_code == 403
    assert r.json()["code"] == "voice_disabled"
    assert speech.transcribed == []
    assert speech.spoken == []

    events = sql(
        vapi,
        "SELECT details FROM audit_logs "
        "WHERE action = 'workspace.voice_policy_changed' ORDER BY id",
    )
    assert [e.details for e in events] == [
        {"from": "enabled", "to": "browser"},
        {"from": "browser", "to": "disabled"},
    ]
    assert (
        vapi.client.put(voice_url(org), json={"policy": "loud"}, headers=owner).status_code == 422
    )


def test_voice_is_workspace_scoped(vapi: Api) -> None:
    ada = vapi.signup("ada@example.com")
    eve = vapi.signup("eve@example.com")
    w_eve = ws(vapi, eve)
    assert vapi.client.get(voice_url(w_eve), headers=ada).status_code == 404
    assert transcribe(vapi, w_eve, ada).status_code == 404
    assert (
        vapi.client.post(voice_url(w_eve, "/speech"), json={"text": "x"}, headers=ada).status_code
        == 404
    )


# --- reading-mode summaries ---------------------------------------------------------


def summary(api: Api, workspace: str, doc: str, headers: dict[str, str], **body: Any) -> Any:
    return api.client.post(
        f"/api/v1/workspaces/{workspace}/documents/{doc}/summary", json=body, headers=headers
    )


def test_summary_of_page_range_cites_those_pages(api: Api) -> None:
    h = api.signup("ada@example.com")
    w = ws(api, h)
    msa = ingest(api, w, h, contract_pdf(), "msa.pdf")
    body = summary(api, w, msa, h, page_start=2, page_end=2).json()
    assert body["status"] == "grounded"
    assert body["citations"]
    assert {c["page_start"] for c in body["citations"]} == {2}
    assert "Fees are payable" in body["answer"]

    whole = summary(api, w, msa, h).json()
    assert {c["page_start"] for c in whole["citations"]} >= {1, 2}


def test_summary_validation_and_isolation(api: Api) -> None:
    h = api.signup("ada@example.com")
    eve = api.signup("eve@example.com")
    w = ws(api, h)
    pending = upload(api, w, h, POLICY, "policy.md").json()["id"]
    r = summary(api, w, pending, h)
    assert r.status_code == 409
    assert r.json()["code"] == "document_not_ready"
    theirs = ingest(api, ws(api, eve), eve, contract_pdf(), "msa.pdf")
    assert summary(api, w, theirs, h).status_code == 404
    mine = ingest(api, w, h, contract_pdf("x"), "mine.pdf")
    assert summary(api, w, mine, h, page_start=3, page_end=2).status_code == 422
    assert summary(api, w, mine, h, page_start=0).status_code == 422
    empty = summary(api, w, mine, h, page_start=40).json()
    assert empty["status"] == "insufficient_context"


def test_container_without_speech_by_default() -> None:
    from documind.container import _build_speech
    from documind.core.config import Settings
    from tests.conftest import _BASE

    assert _build_speech(Settings(_env_file=None, **_BASE)) == {
        "speech_to_text": None,
        "text_to_speech": None,
    }
