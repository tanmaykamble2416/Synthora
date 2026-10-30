"""Aggregates every v1 endpoint module. New feature routers are included here."""

from fastapi import APIRouter

from app.api.v1.endpoints import health

api_router = APIRouter()
api_router.include_router(health.router)
