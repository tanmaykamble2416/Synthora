"""Retrieval unit for RAG. Embedding columns are added by a later migration (Day 7)."""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.paper import Paper
    from app.models.paper_section import PaperSection


class PaperChunk(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "paper_chunks"

    paper_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    # Denormalised for fast workspace-scoped retrieval. Kept consistent with the
    # paper by the composite FK below, so it can never disagree with papers.workspace_id.
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    section_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # NOT NULL: every chunk must be citable by page.
    page_start: Mapped[int] = mapped_column(Integer, nullable=False)
    page_end: Mapped[int] = mapped_column(Integer, nullable=False)

    # Setting chunk.paper = paper fills BOTH paper_id and workspace_id from the paper.
    paper: Mapped["Paper"] = relationship(
        back_populates="chunks",
        foreign_keys=[paper_id, workspace_id],
        lazy="raise",
    )
    section: Mapped["PaperSection | None"] = relationship(
        back_populates="chunks",
        foreign_keys=[section_id, paper_id],
        viewonly=True,
        lazy="raise",
    )

    __table_args__ = (
        # Chunk's workspace must equal its paper's workspace.
        ForeignKeyConstraint(
            ["paper_id", "workspace_id"],
            ["papers.id", "papers.workspace_id"],
            ondelete="CASCADE",
        ),
        # Chunk's section must belong to the chunk's own paper. MATCH SIMPLE (default)
        # skips the check when section_id IS NULL. "SET NULL (section_id)" (PostgreSQL 15+)
        # clears only section_id on section deletion; a plain SET NULL would also try
        # to null paper_id, which is NOT NULL.
        ForeignKeyConstraint(
            ["section_id", "paper_id"],
            ["paper_sections.id", "paper_sections.paper_id"],
            ondelete="SET NULL (section_id)",
        ),
        CheckConstraint("chunk_index >= 0", name="chunk_index_non_negative"),
        CheckConstraint("token_count > 0", name="token_count_positive"),
        CheckConstraint("page_start >= 1 AND page_end >= page_start", name="page_range_valid"),
        UniqueConstraint("paper_id", "chunk_index"),
        Index(None, "workspace_id"),
        Index(None, "section_id"),
    )

    def __repr__(self) -> str:
        return f"<PaperChunk id={self.id} index={self.chunk_index}>"
