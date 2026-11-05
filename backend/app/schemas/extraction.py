import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.schemas.common import OutputSchema
from app.schemas.structure import GrobidOutcome, StructureSource


class ExtractionSummary(BaseModel):
    """Outcome of POST /papers/{id}/extract. Returned with HTTP 200 for every handled
    outcome.

    status describes THIS attempt: "processed", or "failed" with a user-safe failure_code.
    paper_status is the paper's status afterwards. When a re-extraction fails but a
    previous successful extraction exists, that extraction is kept:
    status = "failed", retained_previous_extraction = true, paper_status = "processed".

    The page/section/chunk/reference counts describe the content stored by THIS attempt
    (all 0 when it failed). structure_source and grobid_outcome describe it too
    (null when it failed). metadata_fields_filled lists the paper fields that were
    empty and have been filled from GROBID; existing values are never overwritten.
    processed_at is the time of the last successful extraction (may be null).
    """

    paper_id: uuid.UUID
    status: Literal["processed", "failed"]
    paper_status: Literal["processed", "failed"]
    failure_code: str | None
    retained_previous_extraction: bool
    page_count: int
    text_pages: int
    low_text_pages: int
    empty_pages: int
    needs_ocr_pages: int
    total_characters: int
    total_words: int
    structure_source: StructureSource | None
    grobid_outcome: GrobidOutcome | None
    section_count: int
    chunk_count: int
    reference_count: int
    metadata_fields_filled: list[str]
    processed_at: datetime | None


class PaperPageRead(OutputSchema):
    page_number: int
    text: str
    char_count: int
    word_count: int
    extraction_status: Literal["text", "low_text", "empty"]
    needs_ocr: bool
