import uuid
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Integer, SmallInteger, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.paper import Paper
    from app.models.paper_chunk import PaperChunk

SECTION_TYPES = (
    "abstract",
    "introduction",
    "related_work",
    "methodology",
    "experiments",
    "results",
    "discussion",
    "conclusion",
    "references",
    "other",
)


class PaperSection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "paper_sections"

    paper_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
    )
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    level: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="1")
    heading: Mapped[str | None] = mapped_column(Text)
    section_type: Mapped[str] = mapped_column(String(30), nullable=False, server_default="other")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    page_start: Mapped[int] = mapped_column(Integer, nullable=False)
    page_end: Mapped[int] = mapped_column(Integer, nullable=False)

    paper: Mapped["Paper"] = relationship(back_populates="sections", lazy="raise")
    # Read-only: chunks are attached to sections by setting section_id directly.
    # (Writable relationships on both composite FKs would compete for paper_id.)
    chunks: Mapped[list["PaperChunk"]] = relationship(
        back_populates="section", viewonly=True, lazy="raise"
    )

    __table_args__ = (
        CheckConstraint("order_index >= 0", name="order_index_non_negative"),
        CheckConstraint("level >= 1", name="level_positive"),
        CheckConstraint("page_start >= 1 AND page_end >= page_start", name="page_range_valid"),
        CheckConstraint(
            "section_type IN ({})".format(", ".join(f"'{s}'" for s in SECTION_TYPES)),
            name="section_type_valid",
        ),
        UniqueConstraint("paper_id", "order_index"),
        # Target of the composite FK from paper_chunks (section_id, paper_id):
        # guarantees a chunk's section belongs to the chunk's own paper.
        UniqueConstraint("id", "paper_id"),
    )

    def __repr__(self) -> str:
        return f"<PaperSection id={self.id} order={self.order_index}>"
