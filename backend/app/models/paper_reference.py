"""Bibliography entries of a paper, as parsed by GROBID (Day 6).

Only stored when GROBID was actually used (papers.grobid_outcome = 'used'); the
page-based fallback never invents references. Values GROBID could not determine
stay NULL. raw_text is the reference string as printed in the paper; at least one
of raw_text and title is always present.
"""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Integer, SmallInteger, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.paper import Paper


class PaperReference(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "paper_references"

    paper_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
    )
    # 0-based position in the paper's bibliography.
    ref_index: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_text: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    # List of author names (strings). SQL NULL when none were recognised.
    authors: Mapped[list[str] | None] = mapped_column(JSONB(none_as_null=True))
    publication_year: Mapped[int | None] = mapped_column(SmallInteger)
    venue: Mapped[str | None] = mapped_column(Text)
    doi: Mapped[str | None] = mapped_column(String(255))
    # Where the entry is printed in the PDF (1-based); both NULL if unknown.
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)

    paper: Mapped["Paper"] = relationship(back_populates="references", lazy="raise")

    __table_args__ = (
        CheckConstraint("ref_index >= 0", name="ref_index_non_negative"),
        CheckConstraint(
            "publication_year IS NULL OR publication_year BETWEEN 1800 AND 2100",
            name="publication_year_range",
        ),
        CheckConstraint(
            "(page_start IS NULL AND page_end IS NULL)"
            # IS NOT NULL on both: "2 >= NULL" is NULL, which a CHECK would accept.
            " OR (page_start IS NOT NULL AND page_end IS NOT NULL AND page_start >= 1 AND page_end >= page_start)",
            name="page_range_valid",
        ),
        CheckConstraint("raw_text IS NOT NULL OR title IS NOT NULL", name="has_content"),
        UniqueConstraint("paper_id", "ref_index"),
    )

    def __repr__(self) -> str:
        return f"<PaperReference id={self.id} index={self.ref_index}>"
