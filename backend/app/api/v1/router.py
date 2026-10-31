"""Aggregates every v1 endpoint module. New feature routers are included here."""

from fastapi import APIRouter

from app.api.v1.endpoints import auth, health, users, workspaces

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(workspaces.router)
