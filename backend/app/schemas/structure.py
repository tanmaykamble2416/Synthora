"""Day 6: document structure read models (sections, chunks, references)."""

import uuid
from typing import Literal

from pydantic import BaseModel

from app.schemas.common import OutputSchema


class PaperSectionRead(OutputSchema):
    id: uuid.UUID
    order_index: int
    level: int
    # Printed number ("II.", "B.", "3.2"), separate from the heading text (Day 6 quality pass).
    section_number: str | None
    heading: str | None
    section_type: str
    text: str
    page_start: int
    page_end: int


class PaperChunkRead(BaseModel):
    """A retrieval unit. token_count is measured by token_count_method:
    'word_proxy' = whitespace-separated words (Day 6, NOT tokenizer tokens),
    'unknown' = created before Day 6, 'tokenizer:<name>' = real tokens (Day 7+).

    content_type: "text" (prose, formulas, captions) or "table" (table cells, never mixed
    with prose); null for chunks stored before the Day 6 quality pass."""

    chunk_index: int
    section_id: uuid.UUID | None
    section_number: str | None
    section_heading: str | None
    section_type: str | None
    content_type: Literal["text", "table"] | None
    text: str
    token_count: int
    token_count_method: str
    page_start: int
    page_end: int


class PaperReferenceRead(OutputSchema):
    ref_index: int
    raw_text: str | None
    title: str | None
    authors: list[str] | None
    publication_year: int | None
    venue: str | None
    doi: str | None
    page_start: int | None
    page_end: int | None


StructureSource = Literal["grobid", "page_fallback"]
GrobidOutcome = Literal["used", "disabled", "unavailable", "timeout", "invalid_response", "error"]
