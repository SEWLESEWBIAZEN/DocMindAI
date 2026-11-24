"""OpenAI speech-to-text and text-to-speech (ADR 0011).

Audio is passed straight through: it is never written to disk or storage.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx2
import openai

from documind.application.ports import Transcript
from documind.infrastructure.llm.openai_provider import raise_classified

_EXTENSIONS = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "mp4",
    "audio/m4a": "m4a",
    "audio/x-m4a": "m4a",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
}
SUPPORTED_AUDIO_TYPES = frozenset(_EXTENSIONS)
AUDIO_FORMATS = {"mp3": "audio/mpeg", "opus": "audio/ogg", "aac": "audio/aac", "wav": "audio/wav"}


class OpenAISpeechProvider:
    def __init__(
        self,
        *,
        api_key: str,
        stt_model: str,
        tts_model: str,
        default_voice: str,
        base_url: str | None = None,
        timeout_seconds: float = 60.0,
        max_retries: int = 2,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        self._stt_model = stt_model
        self._tts_model = tts_model
        self._voice = default_voice
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=max_retries,
            http_client=http_client,
        )

    async def transcribe(
        self, audio: bytes, *, media_type: str, language: str | None = None
    ) -> Transcript:
        base_type = media_type.split(";")[0].strip().lower()
        filename = f"speech.{_EXTENSIONS.get(base_type, 'webm')}"
        try:
            result = await self._client.audio.transcriptions.create(
                model=self._stt_model,
                file=(filename, audio, base_type),
                language=language or openai.omit,
                response_format="json",
            )
        except openai.OpenAIError as exc:
            raise_classified(exc)
        return Transcript(text=(getattr(result, "text", "") or "").strip(), language=language)

    async def synthesize(
        self, text: str, *, voice: str | None = None, speed: float = 1.0, audio_format: str = "mp3"
    ) -> AsyncIterator[bytes]:
        try:
            async with self._client.audio.speech.with_streaming_response.create(
                model=self._tts_model,
                voice=voice or self._voice,
                input=text,
                response_format=audio_format,  # type: ignore[arg-type]
                speed=speed,
            ) as response:
                async for chunk in response.iter_bytes(8192):
                    yield chunk
        except openai.OpenAIError as exc:
            raise_classified(exc)
