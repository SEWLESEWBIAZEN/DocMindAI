"""Top-level API router (versioned)."""

from fastapi import APIRouter

from documind.api.routes import (
    auth,
    conversations,
    documents,
    health,
    me,
    rag,
    usage,
    voice,
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
api_router.include_router(rag.router)
api_router.include_router(conversations.router)
api_router.include_router(usage.router)
api_router.include_router(voice.router)
