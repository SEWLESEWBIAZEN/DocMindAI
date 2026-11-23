"""Top-level API router (versioned)."""

from fastapi import APIRouter

from documind.api.routes import auth, health, me, workspaces

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(me.router)
api_router.include_router(workspaces.organizations_router)
api_router.include_router(workspaces.router)
api_router.include_router(workspaces.invitations_router)
