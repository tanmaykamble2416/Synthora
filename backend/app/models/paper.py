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
    Uuid,
)
from sqlalchemy.dialects.postgresql import CHAR, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.paper_chunk import PaperChunk
    from app.models.paper_page import PaperPage
    from app.models.paper_reference import PaperReference
    from app.models.paper_section import PaperSection
    from app.models.workspace import Workspace

PAPER_STATUSES = ("uploaded", "processing", "processed", "failed")
# User-safe reasons for status == "failed" (Day 5). Raw details stay in error_message
# (internal) and server logs. "no_extractable_text" is reserved: a fully scanned PDF is
# currently *processed* with every page flagged needs_ocr (see PaperPage).
FAILURE_CODES = (
    "file_missing",
    "pdf_corrupted",
    "pdf_encrypted",
    "too_many_pages",
    "no_extractable_text",
    "extraction_error",
)
# Where the stored sections/chunks of the last SUCCESSFUL extraction came from (Day 6).
STRUCTURE_SOURCES = ("grobid", "page_fallback")
# What happened when GROBID was asked during that extraction. "used" is the only
# outcome whose metadata and references were applied; every other outcome means
# structure_source = page_fallback (enforced by ck_papers_structure_outcome_consistent).
GROBID_OUTCOMES = ("used", "disabled", "unavailable", "timeout", "invalid_response", "error")
GROBID_FALLBACK_OUTCOMES = tuple(o for o in GROBID_OUTCOMES if o != "used")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return "{} IS NULL OR {} IN ({})".format(column, column, ", ".join(f"'{v}'" for v in values))


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
    # Internal only (never in API responses): short technical summary for operators.
    error_message: Mapped[str | None] = mapped_column(Text)
    # Public, fixed vocabulary: why status == "failed" (Day 5).
    failure_code: Mapped[str | None] = mapped_column(String(40))

    # Bibliographic metadata (Day 6 extraction or user edits). Nullable: never invented.
    title: Mapped[str | None] = mapped_column(Text)
    abstract: Mapped[str | None] = mapped_column(Text)
    # none_as_null: Python None is stored as SQL NULL, not the JSON value 'null',
    # so "authors IS NULL" reliably means "no authors known". (No DDL change.)
    authors: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB(none_as_null=True))
    publication_year: Mapped[int | None] = mapped_column(SmallInteger)
    doi: Mapped[str | None] = mapped_column(String(255))
    venue: Mapped[str | None] = mapped_column(Text)
    # Day 6 meaning: when the extraction whose pages/sections/chunks are stored finished
    # (the last SUCCESS). A failed later attempt does not change it.
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- Day 6: structure provenance and the latest attempt ---------------------------
    structure_source: Mapped[str | None] = mapped_column(String(20))
    grobid_outcome: Mapped[str | None] = mapped_column(String(20))
    # Outcome of the most recent attempt, which may differ from the stored extraction:
    # a failed re-extraction keeps the previous result (status stays processed) and is
    # recorded only here. NULL when the latest attempt succeeded.
    last_extraction_failure_code: Mapped[str | None] = mapped_column(String(40))
    last_extraction_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Internal only (never in API responses): identifies the extraction attempt that holds
    # the current 'processing' claim. Set by every claim, cleared when the paper leaves
    # 'processing'. T2/T3 of an attempt write only while their claim id still matches, so a
    # worker superseded by stale recovery cannot change the paper or its extracted content.
    extraction_claim_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)

    workspace: Mapped["Workspace"] = relationship(back_populates="papers", lazy="raise")
    sections: Mapped[list["PaperSection"]] = relationship(
        back_populates="paper", passive_deletes=True, lazy="raise", order_by="PaperSection.order_index"
    )
    chunks: Mapped[list["PaperChunk"]] = relationship(
        back_populates="paper", passive_deletes=True, lazy="raise", order_by="PaperChunk.chunk_index"
    )
    pages: Mapped[list["PaperPage"]] = relationship(
        back_populates="paper", passive_deletes=True, lazy="raise", order_by="PaperPage.page_number"
    )
    references: Mapped[list["PaperReference"]] = relationship(
        back_populates="paper", passive_deletes=True, lazy="raise", order_by="PaperReference.ref_index"
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
        CheckConstraint(
            "failure_code IS NULL OR failure_code IN ({})".format(", ".join(f"'{c}'" for c in FAILURE_CODES)),
            name="failure_code_valid",
        ),
        # A failure reason can only exist on a failed paper.
        CheckConstraint("failure_code IS NULL OR status = 'failed'", name="failure_code_only_when_failed"),
        CheckConstraint(_in_list("structure_source", STRUCTURE_SOURCES), name="structure_source_valid"),
        CheckConstraint(_in_list("grobid_outcome", GROBID_OUTCOMES), name="grobid_outcome_valid"),
        CheckConstraint(
            "(structure_source IS NULL AND grobid_outcome IS NULL)"
            # Both NOT NULL first: a comparison with NULL is NULL, and a CHECK accepts NULL.
            " OR (structure_source IS NOT NULL AND grobid_outcome IS NOT NULL AND ("
            "(structure_source = 'grobid' AND grobid_outcome = 'used')"
            " OR (structure_source = 'page_fallback' AND grobid_outcome IN ({}))))".format(
                ", ".join(f"'{o}'" for o in GROBID_FALLBACK_OUTCOMES)
            ),
            name="structure_outcome_consistent",
        ),
        CheckConstraint(
            _in_list("last_extraction_failure_code", FAILURE_CODES), name="last_extraction_failure_code_valid"
        ),
        # A claim exists only while processing (status is NOT NULL, so this is NULL-safe).
        CheckConstraint("extraction_claim_id IS NULL OR status = 'processing'", name="claim_only_when_processing"),
        # The same file cannot be uploaded twice into one workspace.
        UniqueConstraint("workspace_id", "sha256"),
        # Target of the composite FK from paper_chunks (paper_id, workspace_id).
        UniqueConstraint("id", "workspace_id"),
        Index(None, "workspace_id", "status"),
        Index(None, "workspace_id", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<Paper id={self.id} status={self.status}>"
