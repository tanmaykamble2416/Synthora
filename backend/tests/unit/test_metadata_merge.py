"""Fill-only-NULL metadata policy and validation of extracted values."""

import pytest

from app.services.metadata_merge import MERGEABLE_FIELDS, fill_only_null, proposal_from_header
from app.services.tei_parser import TeiAuthor, TeiHeader


def header(**overrides) -> TeiHeader:
    values = {
        "title": "A Title",
        "abstract": "An abstract.",
        "authors": (TeiAuthor("Ada Lovelace", "Example University"), TeiAuthor("Alan Turing", None)),
        "doi": "10.1234/abc",
        "publication_year": 2021,
        "venue": "Journal of Tests",
    }
    values.update(overrides)
    return TeiHeader(**values)


EMPTY = dict.fromkeys(MERGEABLE_FIELDS)


def test_every_field_is_proposed_when_valid():
    proposal = proposal_from_header(header())
    assert proposal == {
        "title": "A Title",
        "abstract": "An abstract.",
        "authors": [
            {"name": "Ada Lovelace", "affiliation": "Example University"},
            {"name": "Alan Turing", "affiliation": None},
        ],
        "publication_year": 2021,
        "doi": "10.1234/abc",
        "venue": "Journal of Tests",
    }


def test_missing_values_are_not_proposed():
    proposal = proposal_from_header(
        TeiHeader(title=None, authors=(), abstract=None, doi=None, publication_year=None, venue=None)
    )
    assert proposal == {}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "x" * 1001),
        ("title", "   "),
        ("abstract", "y" * 20_001),
        ("publication_year", 1700),
        ("publication_year", 2200),
        ("doi", "not-a-doi"),
        ("venue", "v" * 1001),
        ("authors", tuple(TeiAuthor(f"A{i}", None) for i in range(501))),
        ("authors", (TeiAuthor("n" * 301, None),)),
    ],
)
def test_invalid_values_are_dropped(field, value):
    proposal = proposal_from_header(header(**{field: value}))
    assert field not in proposal
    assert len(proposal) == len(MERGEABLE_FIELDS) - 1  # the other fields are unaffected


def test_doi_prefix_is_normalised_like_a_manual_edit():
    assert proposal_from_header(header(doi="https://doi.org/10.1234/abc"))["doi"] == "10.1234/abc"


def test_only_null_fields_are_filled():
    proposal = proposal_from_header(header())
    current = {**EMPTY, "title": "My own title", "publication_year": 1999}
    filled = fill_only_null(current, proposal)
    assert "title" not in filled
    assert "publication_year" not in filled
    assert set(filled) == {"abstract", "authors", "doi", "venue"}


def test_nothing_is_filled_when_every_field_has_a_value():
    current = {
        "title": "t", "abstract": "a", "authors": [{"name": "x", "affiliation": None}],
        "publication_year": 2000, "doi": "10.1/x", "venue": "v",
    }
    assert fill_only_null(current, proposal_from_header(header())) == {}


def test_explicit_empty_author_list_is_a_value_not_null():
    filled = fill_only_null({**EMPTY, "authors": []}, proposal_from_header(header()))
    assert "authors" not in filled


def test_unknown_fields_are_never_merged():
    filled = fill_only_null(EMPTY, {"title": "t", "status": "processed", "storage_key": "x"})
    assert filled == {"title": "t"}
