"""Retrieval unit for RAG. Embedding columns are added by a later migration (Day 7)."""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.paper import Paper
    from app.models.paper_section import PaperSection

# How token_count was computed.
#   word_proxy       Day 6: whitespace-separated WORDS, not tokenizer tokens.
#   unknown          chunks that existed before migration 0003 (method never recorded).
#   tokenizer:<name> Day 7+: the embedding model's real tokenizer.
# Day 7 must not embed chunks whose method is word_proxy or unknown.
TOKEN_COUNT_METHODS = ("word_proxy", "unknown")
CHUNK_CONTENT_TYPES = ("text", "table")
TOKENIZER_METHOD_PREFIX = "tokenizer:"
TOKEN_COUNT_COMMENT = (
    "Size of the chunk measured by token_count_method. 'word_proxy' = whitespace-separated "
    "words (Day 6 proxy, NOT tokenizer tokens); 'unknown' = pre-0003 rows; 'tokenizer:<name>' = real tokens."
)


class PaperChunk(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "paper_chunks"

    paper_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    # Denormalised for fast workspace-scoped retrieval. Kept consistent with the
    # paper by the composite FK below, so it can never disagree with papers.workspace_id.
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    section_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, comment=TOKEN_COUNT_COMMENT)
    token_count_method: Mapped[str] = mapped_column(String(64), nullable=False)
    # "text" (prose, formulas, captions) or "table" (table cells, chunked on their own).
    # NULL for chunks stored before migration 0004 (Day 6 quality pass): their content was
    # never classified, so it is not claimed to be either.
    content_type: Mapped[str | None] = mapped_column(String(10))
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
        CheckConstraint(
            "content_type IS NULL OR content_type IN ({})".format(", ".join(f"'{t}'" for t in CHUNK_CONTENT_TYPES)),
            name="content_type_valid",
        ),
        CheckConstraint(
            "token_count_method IN ({}) OR token_count_method LIKE '{}%'".format(
                ", ".join(f"'{m}'" for m in TOKEN_COUNT_METHODS), TOKENIZER_METHOD_PREFIX
            ),
            name="token_count_method_valid",
        ),
        CheckConstraint("page_start >= 1 AND page_end >= page_start", name="page_range_valid"),
        UniqueConstraint("paper_id", "chunk_index"),
        Index(None, "workspace_id"),
        Index(None, "section_id"),
    )

    def __repr__(self) -> str:
        return f"<PaperChunk id={self.id} index={self.chunk_index}>"
