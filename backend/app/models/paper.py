import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import CHAR, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.paper_chunk import PaperChunk
    from app.models.paper_section import PaperSection
    from app.models.workspace import Workspace

PAPER_STATUSES = ("uploaded", "processing", "processed", "failed")


class Paper(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "papers"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    # Display only. Never used to build a filesystem path.
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    # Server-generated storage location (Day 4).
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="uploaded")
    error_message: Mapped[str | None] = mapped_column(Text)

    # Metadata filled by GROBID on Day 5. Nullable: never invented.
    title: Mapped[str | None] = mapped_column(Text)
    abstract: Mapped[str | None] = mapped_column(Text)
    # none_as_null: Python None is stored as SQL NULL, not the JSON value 'null',
    # so "authors IS NULL" reliably means "no authors known". (No DDL change.)
    authors: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB(none_as_null=True))
    publication_year: Mapped[int | None] = mapped_column(SmallInteger)
    doi: Mapped[str | None] = mapped_column(String(255))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    workspace: Mapped["Workspace"] = relationship(back_populates="papers", lazy="raise")
    sections: Mapped[list["PaperSection"]] = relationship(
        back_populates="paper", passive_deletes=True, lazy="raise", order_by="PaperSection.order_index"
    )
    chunks: Mapped[list["PaperChunk"]] = relationship(
        back_populates="paper", passive_deletes=True, lazy="raise", order_by="PaperChunk.chunk_index"
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ({})".format(", ".join(f"'{s}'" for s in PAPER_STATUSES)), name="status_valid"
        ),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_hex"),
        CheckConstraint("file_size_bytes > 0", name="file_size_positive"),
        CheckConstraint("page_count IS NULL OR page_count >= 0", name="page_count_non_negative"),
        CheckConstraint(
            "publication_year IS NULL OR publication_year BETWEEN 1800 AND 2100",
            name="publication_year_range",
        ),
        # The same file cannot be uploaded twice into one workspace.
        UniqueConstraint("workspace_id", "sha256"),
        # Target of the composite FK from paper_chunks (paper_id, workspace_id).
        UniqueConstraint("id", "workspace_id"),
        Index(None, "workspace_id", "status"),
        Index(None, "workspace_id", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<Paper id={self.id} status={self.status}>"
