import json
from typing import Any

import httpx2
import pytest

from documind.domain.processing.errors import PermanentProcessingError, TransientProcessingError
from documind.infrastructure.speech.openai_provider import OpenAISpeechProvider


def provider(handler: Any) -> OpenAISpeechProvider:
    return OpenAISpeechProvider(
        api_key="sk-test",
        stt_model="gpt-4o-mini-transcribe",
        tts_model="gpt-4o-mini-tts",
        default_voice="alloy",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )


async def test_transcribe_sends_audio_and_model() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"text": "  How much notice?  "})

    transcript = await provider(handler).transcribe(
        b"RIFFaudio", media_type="audio/wav", language="en"
    )
    assert transcript.text == "How much notice?"
    request = seen[0]
    assert request.url.path.endswith("/audio/transcriptions")
    body = request.content
    assert b"gpt-4o-mini-transcribe" in body
    assert b'filename="speech.wav"' in body
    assert b"RIFFaudio" in body
    assert b'name="language"' in body


async def test_synthesize_streams_audio() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(request.content))
        return httpx2.Response(
            200, content=b"ID3" + b"\x00" * 20000, headers={"content-type": "audio/mpeg"}
        )

    chunks = [c async for c in provider(handler).synthesize("Hello", voice="nova", speed=1.5)]
    assert b"".join(chunks).startswith(b"ID3")
    assert len(b"".join(chunks)) == 20003
    assert seen[0] == {
        "model": "gpt-4o-mini-tts",
        "voice": "nova",
        "input": "Hello",
        "response_format": "mp3",
        "speed": 1.5,
    }


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (429, TransientProcessingError),
        (500, TransientProcessingError),
        (400, PermanentProcessingError),
    ],
)
async def test_errors_are_classified(status: int, error: type[Exception]) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, json={"error": {"message": "x"}})

    with pytest.raises(error):
        await provider(handler).transcribe(b"x", media_type="audio/webm")
    with pytest.raises(error):
        async for _ in provider(handler).synthesize("x"):
            pass
