"""Voice: workspace policy, speech-to-text and text-to-speech (ADR 0011).

Voice is a client of the existing services: transcribed text goes through the
normal conversation endpoints, with the normal permission checks. Audio is
handled in memory and never stored.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from documind.application.identity.common import AuditRecorder, RequestMeta
from documind.application.ports import SpeechToTextProvider, TextToSpeechProvider, Transcript
from documind.domain.errors import (
    NotFoundError,
    PayloadTooLargeError,
    PermissionDeniedError,
    ServiceUnavailableError,
    UnsupportedMediaTypeError,
    ValidationFailedError,
)
from documind.domain.identity.permissions import Permission
from documind.domain.identity.policies import WorkspaceContext
from documind.domain.processing.errors import ProcessingError
from documind.infrastructure.database.models import Workspace
from documind.infrastructure.database.repositories.identity import AuditLogRepository

VoicePolicy = Literal["enabled", "browser", "disabled"]
POLICIES: tuple[VoicePolicy, ...] = ("enabled", "browser", "disabled")
VOICES = (
    "alloy",
    "ash",
    "ballad",
    "coral",
    "echo",
    "fable",
    "nova",
    "onyx",
    "sage",
    "shimmer",
    "verse",
)
AUDIO_TYPES = frozenset(
    {
        "audio/webm",
        "audio/ogg",
        "audio/mpeg",
        "audio/mp3",
        "audio/mp4",
        "audio/m4a",
        "audio/x-m4a",
        "audio/wav",
        "audio/x-wav",
        "audio/wave",
    }
)
MIN_SPEED, MAX_SPEED = 0.5, 2.0


@dataclass(frozen=True, slots=True)
class VoiceSettings:
    policy: VoicePolicy
    #: True when this workspace may use DocuMind's speech service right now.
    server_speech: bool
    #: Whether the deployment has a speech provider at all.
    provider_configured: bool
    voices: tuple[str, ...]
    max_audio_bytes: int
    max_text_chars: int


class VoiceService:
    def __init__(
        self,
        session: AsyncSession,
        meta: RequestMeta,
        *,
        stt: SpeechToTextProvider | None,
        tts: TextToSpeechProvider | None,
        max_audio_bytes: int,
        max_text_chars: int,
    ) -> None:
        self._session = session
        self._audit = AuditRecorder(AuditLogRepository(session), meta)
        self._stt = stt
        self._tts = tts
        self._max_audio = max_audio_bytes
        self._max_text = max_text_chars

    async def _workspace(self, ctx: WorkspaceContext) -> Workspace:
        workspace = await self._session.get(Workspace, ctx.workspace_id)
        if workspace is None:  # pragma: no cover - ctx guarantees membership
            raise NotFoundError("Workspace not found.")
        return workspace

    def _settings(self, policy: VoicePolicy) -> VoiceSettings:
        configured = self._stt is not None and self._tts is not None
        return VoiceSettings(
            policy=policy,
            server_speech=configured and policy == "enabled",
            provider_configured=configured,
            voices=VOICES if configured else (),
            max_audio_bytes=self._max_audio,
            max_text_chars=self._max_text,
        )

    async def settings(self, ctx: WorkspaceContext) -> VoiceSettings:
        ctx.require(Permission.WORKSPACE_READ)
        workspace = await self._workspace(ctx)
        return self._settings(workspace.voice_policy)  # type: ignore[arg-type]

    async def set_policy(self, ctx: WorkspaceContext, policy: VoicePolicy) -> VoiceSettings:
        ctx.require(Permission.WORKSPACE_UPDATE)
        if policy not in POLICIES:
            raise ValidationFailedError("Unknown voice policy.")
        workspace = await self._workspace(ctx)
        previous = workspace.voice_policy
        if previous != policy:
            workspace.voice_policy = policy
            self._audit.record(
                "workspace.voice_policy_changed",
                actor_user_id=ctx.user_id,
                workspace_id=ctx.workspace_id,
                target_type="workspace",
                target_id=ctx.workspace_id,
                details={"from": previous, "to": policy},
            )
            await self._session.commit()
        return self._settings(policy)

    async def _require_server_speech(self, ctx: WorkspaceContext) -> None:
        policy = (await self._workspace(ctx)).voice_policy
        if policy == "disabled":
            raise PermissionDeniedError(
                "Voice is turned off in this workspace.", code="voice_disabled"
            )
        if policy == "browser":
            raise PermissionDeniedError(
                "This workspace allows only your browser's speech features.",
                code="server_voice_not_allowed",
            )
        if self._stt is None or self._tts is None:
            raise ServiceUnavailableError(
                "Server speech isn't configured. Your browser's speech features still work.",
                code="speech_not_configured",
            )

    async def transcribe(
        self, ctx: WorkspaceContext, audio: bytes, *, media_type: str, language: str | None
    ) -> Transcript:
        ctx.require(Permission.CONVERSATIONS_USE)
        await self._require_server_speech(ctx)
        base_type = media_type.split(";")[0].strip().lower()
        if base_type not in AUDIO_TYPES:
            raise UnsupportedMediaTypeError("Record in WebM, Ogg, MP3, MP4/M4A or WAV.")
        if not audio:
            raise ValidationFailedError("The recording is empty.", code="empty_audio")
        if len(audio) > self._max_audio:
            raise PayloadTooLargeError("The recording is too long. Keep questions under a minute.")
        if language is not None and not (
            2 <= len(language) <= 5 and language.replace("-", "").isalpha()
        ):
            raise ValidationFailedError("Language must be a code like 'en' or 'de'.")
        stt = self._stt
        if stt is None:  # checked by _require_server_speech
            raise ServiceUnavailableError("Server speech isn't configured.")
        try:
            transcript = await stt.transcribe(audio, media_type=base_type, language=language)
        except ProcessingError as exc:
            raise ServiceUnavailableError(
                "Speech recognition is temporarily unavailable.", code="speech_unavailable"
            ) from exc
        return transcript

    async def speech(
        self, ctx: WorkspaceContext, text: str, *, voice: str | None, speed: float
    ) -> AsyncIterator[bytes]:
        """Validate, start synthesis and return the audio stream.

        The first chunk is fetched here, so provider failures become ordinary
        errors instead of a truncated audio response.
        """
        ctx.require(Permission.DOCUMENTS_READ)
        await self._require_server_speech(ctx)
        text = text.strip()
        if not text:
            raise ValidationFailedError("Nothing to read.", code="empty_text")
        if len(text) > self._max_text:
            raise ValidationFailedError(f"Read at most {self._max_text} characters at a time.")
        if voice is not None and voice not in VOICES:
            raise ValidationFailedError("Unknown voice.")
        if not MIN_SPEED <= speed <= MAX_SPEED:
            raise ValidationFailedError(f"Speed must be between {MIN_SPEED} and {MAX_SPEED}.")
        tts = self._tts
        if tts is None:  # checked by _require_server_speech
            raise ServiceUnavailableError("Server speech isn't configured.")
        stream = tts.synthesize(text, voice=voice, speed=speed).__aiter__()
        try:
            first = await anext(stream)
        except StopAsyncIteration:
            first = b""
        except ProcessingError as exc:
            raise ServiceUnavailableError(
                "Speech is temporarily unavailable.", code="speech_unavailable"
            ) from exc

        async def audio() -> AsyncIterator[bytes]:
            if first:
                yield first
            try:
                async for chunk in stream:
                    yield chunk
            except ProcessingError:
                return  # playback simply ends early

        return audio()
