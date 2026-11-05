"""Fill-only-NULL merge of GROBID header metadata into a paper (Day 6).

Policy (approved): extraction may fill a bibliographic field only while it is SQL
NULL. A value that is already present, whether typed by the user (PATCH /papers/{id})
or filled by an earlier extraction, is never overwritten. An explicitly stored empty
list of authors is a value, not NULL, and is kept.

Every proposed value goes through the same validation as a manual edit
(app.schemas.paper); a value that fails validation is dropped, never stored.
The repository applies the result with COALESCE(column, :value), so even a value
that appeared after it was read cannot be overwritten.
"""

import logging
from collections.abc import Mapping
from typing import Any

from pydantic import TypeAdapter, ValidationError

from app.schemas.paper import Abstract, Authors, Doi, PublicationYear, Title, Venue
from app.services.tei_parser import TeiHeader

logger = logging.getLogger(__name__)

MERGEABLE_FIELDS = ("title", "abstract", "authors", "publication_year", "doi", "venue")

_ADAPTERS: dict[str, TypeAdapter[Any]] = {
    "title": TypeAdapter(Title),
    "abstract": TypeAdapter(Abstract),
    "authors": TypeAdapter(Authors),
    "publication_year": TypeAdapter(PublicationYear),
    "doi": TypeAdapter(Doi),
    "venue": TypeAdapter(Venue),
}


def proposal_from_header(header: TeiHeader) -> dict[str, Any]:
    """Validated metadata values from a GROBID header; absent or invalid ones omitted."""
    raw: dict[str, Any] = {
        "title": header.title,
        "abstract": header.abstract,
        "authors": [{"name": a.name, "affiliation": a.affiliation} for a in header.authors] or None,
        "publication_year": header.publication_year,
        "doi": header.doi,
        "venue": header.venue,
    }
    proposal: dict[str, Any] = {}
    for name, value in raw.items():
        if value is None:
            continue
        try:
            validated = _ADAPTERS[name].validate_python(value)
        except ValidationError:
            logger.info("Dropping invalid extracted metadata field %s", name)  # value not logged
            continue
        if name == "authors":
            validated = [a.model_dump() for a in validated]
        proposal[name] = validated
    return proposal


def fill_only_null(current: Mapping[str, Any], proposal: Mapping[str, Any]) -> dict[str, Any]:
    """The subset of `proposal` whose field is NULL in `current`."""
    return {
        name: value
        for name, value in proposal.items()
        if name in MERGEABLE_FIELDS and value is not None and current.get(name) is None
    }
