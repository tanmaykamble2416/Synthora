import re
import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.schemas.common import InputSchema, OutputSchema

# Same bounds as the database CHECK constraint ck_papers_publication_year_range.
PUBLICATION_YEAR_MIN = 1800
PUBLICATION_YEAR_MAX = 2100
MAX_AUTHORS = 500

_DOI_PATTERN = re.compile(r"^10\.\d{4,9}/\S+$")
_DOI_PREFIXES = ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/", "doi:")


def _normalize_doi(value: str) -> str:
    lowered = value.lower()
    for prefix in _DOI_PREFIXES:
        if lowered.startswith(prefix):
            value = value[len(prefix):]
            break
    if not _DOI_PATTERN.fullmatch(value):
        raise ValueError("doi must look like '10.1234/abc' (optionally prefixed with https://doi.org/).")
    return value


Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
Abstract = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000)]
Doi = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255), AfterValidator(_normalize_doi)
]
PublicationYear = Annotated[int, Field(ge=PUBLICATION_YEAR_MIN, le=PUBLICATION_YEAR_MAX)]


class AuthorIn(InputSchema):
    name: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    affiliation: Annotated[str, StringConstraints(min_length=1, max_length=500)] | None = None


class PaperUpdate(InputSchema):
    """Client-editable bibliographic metadata only.

    extra='forbid' (from InputSchema) rejects every other field, including internal
    processing state: id, workspace_id, storage_key, sha256, file_size_bytes, status,
    error_message, processed_at, owner_id. Send null to clear a field.
    """

    title: Title | None = None
    abstract: Abstract | None = None
    authors: Annotated[list[AuthorIn], Field(max_length=MAX_AUTHORS)] | None = None
    publication_year: PublicationYear | None = None
    doi: Doi | None = None

    @model_validator(mode="after")
    def _require_at_least_one_field(self) -> "PaperUpdate":
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to update.")
        return self

    def to_column_values(self) -> dict[str, Any]:
        """Only the fields the client actually sent (explicit nulls included)."""
        return self.model_dump(exclude_unset=True)


class AuthorRead(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    affiliation: str | None = None


class PaperRead(OutputSchema):
    """Public representation of a paper.

    Deliberately omitted internal fields (they stay in the database for server-side use):
      * storage_key: the server's file location.
      * error_message: raw processing errors can contain file paths, hostnames or
        stack-trace fragments. Clients see status == "failed"; operators use the
        DB column and server logs. A user-safe failure reason, if needed, must be
        a separate field drawn from a fixed set of codes.
    """

    id: uuid.UUID
    workspace_id: uuid.UUID
    original_filename: str
    sha256: str
    file_size_bytes: int
    page_count: int | None
    status: str
    title: str | None
    abstract: str | None
    authors: list[AuthorRead] | None
    publication_year: int | None
    doi: str | None
    processed_at: datetime | None
    created_at: datetime
    updated_at: datetime
