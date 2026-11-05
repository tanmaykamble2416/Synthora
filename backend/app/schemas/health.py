from typing import Literal

from pydantic import BaseModel


class LivenessResponse(BaseModel):
    status: Literal["ok"]
    app_name: str
    version: str
    environment: str


class DatabaseHealthResponse(BaseModel):
    status: Literal["ok"]
    database: str
    server_version: str
    pgvector_version: str
    latency_ms: float


class GrobidHealthResponse(BaseModel):
    """status "disabled": GROBID_ENABLED=false (extraction uses the page fallback).
    The GROBID URL is deliberately not included."""

    status: Literal["ok", "disabled"]
    latency_ms: float | None
