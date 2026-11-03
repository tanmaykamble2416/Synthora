import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.schemas.common import OutputSchema


class ExtractionSummary(BaseModel):
    """Outcome of POST /papers/{id}/extract. Returned with HTTP 200 for every handled
    outcome: status is "processed", or "failed" with a user-safe failure_code."""

    paper_id: uuid.UUID
    status: Literal["processed", "failed"]
    failure_code: str | None
    page_count: int
    text_pages: int
    low_text_pages: int
    empty_pages: int
    needs_ocr_pages: int
    total_characters: int
    total_words: int
    processed_at: datetime | None


class PaperPageRead(OutputSchema):
    page_number: int
    text: str
    char_count: int
    word_count: int
    extraction_status: Literal["text", "low_text", "empty"]
    needs_ocr: bool
