"""Importing this package registers every model on Base.metadata (needed by Alembic)."""

from app.models.paper import Paper
from app.models.paper_chunk import PaperChunk
from app.models.paper_section import PaperSection
from app.models.user import User
from app.models.workspace import Workspace

__all__ = ["Paper", "PaperChunk", "PaperSection", "User", "Workspace"]
