"""Voice settings, speech-to-text and text-to-speech (ADR 0011)."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from documind.api.dependencies import ContainerDep, MetaDep, SessionDep, WorkspaceCtx
from documind.application.voice.service import MAX_SPEED, MIN_SPEED, VoiceService, VoiceSettings

router = APIRouter(prefix="/workspaces/{workspace_id}/voice", tags=["voice"])


class VoiceSettingsResponse(BaseModel):
    policy: Literal["enabled", "browser", "disabled"] = Field(
        description=(
            '"enabled": DocuMind\'s speech service and browser speech; "browser": only the '
            'browser\'s own speech features (DocuMind never receives audio); "disabled": no voice.'
        )
    )
    server_speech: bool = Field(description="Whether /transcribe and /speech can be used here.")
    provider_configured: bool
    voices: list[str]
    max_audio_bytes: int
    max_text_chars: int


class VoicePolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy: Literal["enabled", "browser", "disabled"]


class TranscriptResponse(BaseModel):
    text: str
    language: str | None


class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=4096)
    voice: str | None = Field(default=None, max_length=50)
    speed: float = Field(default=1.0, ge=MIN_SPEED, le=MAX_SPEED)


def get_voice_service(session: SessionDep, container: ContainerDep, meta: MetaDep) -> VoiceService:
    s = container.settings
    return VoiceService(
        session,
        meta,
        stt=container.speech_to_text,
        tts=container.text_to_speech,
        max_audio_bytes=s.speech_max_audio_bytes,
        max_text_chars=s.speech_max_text_chars,
    )


VoiceServiceDep = Annotated[VoiceService, Depends(get_voice_service)]


async def limit_voice_requests(ctx: WorkspaceCtx, container: ContainerDep) -> None:
    s = container.settings
    await container.rate_limiter.hit(
        "voice:user",
        str(ctx.user_id),
        s.rate_limit_voice_per_user,
        s.rate_limit_voice_window_seconds,
    )


def _settings(v: VoiceSettings) -> VoiceSettingsResponse:
    return VoiceSettingsResponse(
        policy=v.policy,
        server_speech=v.server_speech,
        provider_configured=v.provider_configured,
        voices=list(v.voices),
        max_audio_bytes=v.max_audio_bytes,
        max_text_chars=v.max_text_chars,
    )


@router.get("", response_model=VoiceSettingsResponse, summary="Voice settings for this workspace")
async def get_voice(ctx: WorkspaceCtx, voice: VoiceServiceDep) -> VoiceSettingsResponse:
    return _settings(await voice.settings(ctx))


@router.put(
    "", response_model=VoiceSettingsResponse, summary="Set the workspace voice policy (admins)"
)
async def set_voice_policy(
    body: VoicePolicyRequest, ctx: WorkspaceCtx, voice: VoiceServiceDep
) -> VoiceSettingsResponse:
    return _settings(await voice.set_policy(ctx, body.policy))


@router.post(
    "/transcribe",
    response_model=TranscriptResponse,
    dependencies=[Depends(limit_voice_requests)],
    summary="Speech to text (audio is not stored)",
)
async def transcribe(
    ctx: WorkspaceCtx,
    voice: VoiceServiceDep,
    container: ContainerDep,
    audio: Annotated[UploadFile, File(description="WebM/Ogg/MP3/MP4/WAV recording")],
    language: Annotated[str | None, Form(max_length=5)] = None,
) -> TranscriptResponse:
    data = await audio.read(container.settings.speech_max_audio_bytes + 1)
    transcript = await voice.transcribe(
        ctx, data, media_type=audio.content_type or "", language=language or None
    )
    return TranscriptResponse(text=transcript.text, language=transcript.language)


@router.post(
    "/speech",
    response_class=StreamingResponse,
    dependencies=[Depends(limit_voice_requests)],
    summary="Text to speech, streamed as MP3 (nothing is stored)",
    responses={200: {"content": {"audio/mpeg": {}}}},
)
async def speech(
    body: SpeechRequest, ctx: WorkspaceCtx, voice: VoiceServiceDep
) -> StreamingResponse:
    audio = await voice.speech(ctx, body.text, voice=body.voice, speed=body.speed)
    return StreamingResponse(audio, media_type="audio/mpeg", headers={"Cache-Control": "no-store"})
