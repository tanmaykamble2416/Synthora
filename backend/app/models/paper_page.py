"""Page-level extracted text (Day 5).

One row per physical PDF page, numbered from 1 exactly as in the source document.
Pages without extractable text are stored too (text == ""), so page numbers never
shift and every later citation can point at a real page.

Kept separate from paper_sections (logical structure from Day 6) and paper_chunks
(retrieval units): pages, sections and chunks are different, overlapping divisions.
"""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint, false
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.paper import Paper

# text:     enough extractable text
# low_text: some text, below the threshold (a caption, a page number, a scanned page with a stamp)
# empty:    no extractable text at all
PAGE_EXTRACTION_STATUSES = ("text", "low_text", "empty")


class PaperPage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "paper_pages"

    paper_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-based
    text: Mapped[str] = mapped_column(Text, nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False)
    word_count: Mapped[int] = mapped_column(Integer, nullable=False)
    extraction_status: Mapped[str] = mapped_column(String(20), nullable=False)
    # True when the page has images but (almost) no text layer: likely scanned.
    # Flagged only; no OCR is performed.
    needs_ocr: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())

    paper: Mapped["Paper"] = relationship(back_populates="pages", lazy="raise")

    __table_args__ = (
        CheckConstraint("page_number >= 1", name="page_number_positive"),
        CheckConstraint("char_count >= 0", name="char_count_non_negative"),
        CheckConstraint("word_count >= 0", name="word_count_non_negative"),
        CheckConstraint(
            "extraction_status IN ({})".format(", ".join(f"'{s}'" for s in PAGE_EXTRACTION_STATUSES)),
            name="extraction_status_valid",
        ),
        # Exactly one row per page: re-extraction can never create duplicates.
        # paper_id is the leading column, so this index also serves "pages of a paper".
        UniqueConstraint("paper_id", "page_number"),
    )

    def __repr__(self) -> str:
        return f"<PaperPage paper={self.paper_id} page={self.page_number}>"
