"""Top-level API router (versioned)."""

from fastapi import APIRouter

from documind.api.routes import (
    ai_providers,
    auth,
    billing,
    conversations,
    documents,
    extraction,
    health,
    infrastructure,
    me,
    metrics,
    onboarding,
    rag,
    usage,
    voice,
    worker_api,
    workspaces,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(me.router)
api_router.include_router(workspaces.organizations_router)
api_router.include_router(workspaces.router)
api_router.include_router(workspaces.invitations_router)
api_router.include_router(documents.router)
api_router.include_router(onboarding.router)
api_router.include_router(extraction.router)
api_router.include_router(rag.router)
api_router.include_router(conversations.router)
api_router.include_router(usage.router)
api_router.include_router(billing.public_router)
api_router.include_router(billing.router)
api_router.include_router(voice.router)
api_router.include_router(ai_providers.router)
api_router.include_router(infrastructure.router)
api_router.include_router(worker_api.router)
api_router.include_router(metrics.router)
