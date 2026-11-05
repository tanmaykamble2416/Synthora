"""TEI parser: metadata, sections, page spans, references and XML safety."""

from pathlib import Path

import pyexpat
import pytest

from app.services import tei_parser
from app.services.tei_parser import (
    TeiParseError,
    UnsafeXmlParserError,
    classify_heading,
    clean_doi,
    parse_coords,
    parse_tei,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "tei"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture(scope="module")
def full():
    return parse_tei(fixture("full_paper.tei.xml"), page_count=5)


# --- header ------------------------------------------------------------------------


def test_header_metadata(full):
    header = full.header
    assert header.title == "Structured Retrieval for Literature Reviews"  # whitespace normalised
    assert header.doi == "10.1234/synthora.test.2021"
    assert header.publication_year == 2021
    assert header.venue == "Journal of Synthetic Research"  # main title, not the abbreviation
    assert header.abstract.startswith("Sentence 1 about the abstract")


def test_authors_skip_affiliation_only_entries(full):
    authors = full.header.authors
    assert [a.name for a in authors] == ["Ada M Lovelace", "Alan Turing"]
    assert authors[0].affiliation == "Department of Computing, Example University"
    assert authors[1].affiliation is None


def test_empty_header_gives_none_not_empty_strings():
    document = parse_tei(fixture("no_coordinates.tei.xml"), page_count=2)
    header = document.header
    assert header.title is None
    assert header.abstract is None
    assert header.authors == ()
    assert header.doi is None and header.publication_year is None and header.venue is None


# --- sections ----------------------------------------------------------------------


def test_section_order_types_and_levels(full):
    summary = [(s.section_type, s.level, s.heading) for s in full.sections]
    assert summary == [
        ("abstract", 1, "Abstract"),
        ("introduction", 1, "Introduction"),
        ("related_work", 1, "Related Work"),
        ("methodology", 1, "Proposed Method"),
        ("methodology", 2, "Training details"),  # unclassified subsection inherits "3"
        ("experiments", 1, "Experiments"),
        ("results", 1, "Results"),
        ("conclusion", 1, "Conclusion"),
        ("other", 1, "Acknowledgements"),
    ]


def test_captions_footnotes_and_references_are_not_section_text(full):
    all_text = " ".join(p.text for s in full.sections for p in s.paragraphs)
    assert "FIGURE CAPTION TEXT" not in all_text  # figures are returned separately (see below)
    assert "FOOTNOTE TEXT" not in all_text
    assert "Attention is all you need" not in all_text


def test_formulas_are_kept_as_formula_blocks(full):
    """Day 6 quality pass: formulas are source content and are no longer dropped."""
    training = full.sections[4]
    formulas = [p for p in training.paragraphs if p.kind == "formula"]
    assert [f.text for f in formulas] == ["L = sum of losses (1)"]
    assert (formulas[0].page_start, formulas[0].page_end) == (3, 3)


def test_figures_are_returned_with_their_position(full):
    assert len(full.figures) == 1
    figure = full.figures[0]
    assert (figure.kind, figure.text, figure.page_start) == ("figure", "FIGURE CAPTION TEXT IS KEPT.", 3)
    assert figure.boxes and figure.boxes[0][0] == 3


def test_list_items_become_paragraphs(full):
    training = full.sections[4]
    assert [p.kind for p in training.paragraphs] == ["text", "formula", "text", "text"]
    assert training.paragraphs[2].text.startswith("First listed hyper parameter")


def test_paragraph_pages_come_from_coordinates(full):
    method = full.sections[3]
    paragraph = method.paragraphs[0]
    assert (paragraph.page_start, paragraph.page_end) == (2, 3)  # continues onto the next page
    assert paragraph.sentences[-1].page_end == 3
    assert paragraph.sentences[0].page_start == 2


def test_sentences_are_kept(full):
    intro = full.sections[1].paragraphs[0]
    assert len(intro.sentences) == 14
    assert intro.text == " ".join(s.text for s in intro.sentences)


def test_paragraphs_without_coordinates_have_unknown_pages():
    document = parse_tei(fixture("no_coordinates.tei.xml"), page_count=2)
    assert [s.heading for s in document.sections] == [None, "Background"]
    assert document.sections[1].level == 1  # "2." -> level 1
    assert document.sections[1].section_type == "related_work"
    paragraph = document.sections[0].paragraphs[0]
    assert (paragraph.page_start, paragraph.page_end) == (None, None)


def test_body_without_paragraphs_is_rejected():
    with pytest.raises(TeiParseError, match="no body paragraphs"):
        parse_tei(fixture("header_only.tei.xml"), page_count=1)


@pytest.mark.parametrize(
    ("heading", "kind"),
    [
        ("Introduction", "introduction"),
        ("1 INTRODUCTION AND MOTIVATION", "introduction"),
        ("Related Work", "related_work"),
        ("Background", "related_work"),
        ("Introduction and Background", "introduction"),
        ("Background and Related Work", "related_work"),
        ("RELATED WORK OR LITERATURE STUDIES", "related_work"),
        ("Literature Review", "related_work"),
        ("Results And Discussion", "results"),
        ("Methods", "methodology"),
        ("PROPOSED SYSTEM ARCHITECHTURE AND METHODOLOGY", "methodology"),
        # Day 6 quality pass: generic words no longer force a type.
        ("MOTIVATION", "other"),
        ("Overview", "other"),
        ("Model Training", "other"),
        ("Summary", "other"),
        ("Dataset", "other"),
        ("Implementation", "other"),
        ("Framework", "other"),
        ("Materials and Methods", "methodology"),
        ("Our Approach", "methodology"),
        ("Experimental Setup", "experiments"),
        ("Evaluation", "experiments"),
        ("Results", "results"),
        ("Discussion", "discussion"),
        ("Limitations", "discussion"),
        ("Conclusions and Future Work", "conclusion"),
        ("References", "references"),
        ("Acknowledgements", "other"),
        ("Remodelling of tissue", "other"),  # "model" must match as a whole word only
        (None, "other"),
    ],
)
def test_classify_heading(heading, kind):
    assert classify_heading(heading) == kind


# --- references --------------------------------------------------------------------


def test_references_are_parsed_in_order(full):
    refs = full.references
    assert len(refs) == 4  # the empty biblStruct b3 is skipped
    first = refs[0]
    assert first.title == "Attention is all you need"
    assert first.authors == ("Ashish Vaswani", "Noam Shazeer")
    assert first.publication_year == 2017
    assert first.venue == "Advances in Neural Information Processing Systems"
    assert first.doi == "10.5555/3295222.3295349"
    assert first.raw_text.startswith("A. Vaswani")
    assert (first.page_start, first.page_end) == (4, 4)


def test_book_reference_uses_monograph_title_and_spans_pages(full):
    book = full.references[1]
    assert book.title == "Speech and Language Processing"
    assert book.authors == ("Daniel Jurafsky",)
    assert book.venue is None
    assert (book.page_start, book.page_end) == (4, 5)


def test_raw_only_reference_is_kept(full):
    raw_only = full.references[2]
    assert raw_only.title is None
    assert raw_only.raw_text.startswith("Anonymous technical report")


def test_invalid_values_are_dropped_not_stored(full):
    bad = full.references[3]
    assert bad.doi is None  # "not-a-doi"
    assert bad.publication_year is None  # 1700 is outside 1800..2100
    assert (bad.page_start, bad.page_end) == (None, None)  # page 9 of a 5-page PDF


# --- coordinates and DOIs ----------------------------------------------------------


@pytest.mark.parametrize(
    ("coords", "expected"),
    [
        ("3,72.0,100.0,10.0,5.0", (3, 3)),
        ("3,1,1,1,1;4,1,1,1,1", (3, 4)),
        ("4,1,1,1,1;2,1,1,1,1", (2, 4)),
        ("0,1,1,1,1", None),
        ("99,1,1,1,1", None),
        ("x,1,1,1,1;2,1,1,1,1", (2, 2)),
        ("", None),
        (None, None),
        ("-1,1,1,1,1", None),
    ],
)
def test_parse_coords(coords, expected):
    assert parse_coords(coords, page_count=5) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10.1000/xyz123", "10.1000/xyz123"),
        ("https://doi.org/10.1000/xyz123", "10.1000/xyz123"),
        ("doi:10.1000/xyz123", "10.1000/xyz123"),
        ("not a doi", None),
        ("", None),
        ("10.1000/" + "x" * 300, None),
    ],
)
def test_clean_doi(raw, expected):
    assert clean_doi(raw) == expected


# --- XML safety and robustness -----------------------------------------------------


def test_malformed_xml_is_rejected():
    with pytest.raises(TeiParseError, match="malformed"):
        parse_tei(fixture("full_paper.tei.xml")[:5000], page_count=5)


def test_non_tei_root_is_rejected():
    with pytest.raises(TeiParseError, match="root element"):
        parse_tei(b"<html><body><p>hi</p></body></html>", page_count=1)


def test_entity_expansion_is_rejected_before_parsing():
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE TEI [<!ENTITY a "aaaaaaaaaa">'
        b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
        b'<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body><div><p>&b;</p></div></body></text></TEI>'
    )
    with pytest.raises(TeiParseError, match="DOCTYPE"):
        parse_tei(bomb, page_count=1)


def test_external_entity_is_rejected():
    xxe = (
        b'<?xml version="1.0"?><!DOCTYPE TEI [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        b'<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body><div><p>&x;</p></div></body></text></TEI>'
    )
    with pytest.raises(TeiParseError):
        parse_tei(xxe, page_count=1)


def test_old_expat_is_refused(monkeypatch):
    monkeypatch.setattr(tei_parser.pyexpat, "version_info", (2, 2, 0))
    with pytest.raises(UnsafeXmlParserError):
        parse_tei(fixture("full_paper.tei.xml"), page_count=5)


def test_this_runtime_has_a_safe_expat():
    assert pyexpat.version_info >= (2, 4, 1)


def test_reference_count_is_capped(monkeypatch):
    monkeypatch.setattr(tei_parser, "MAX_REFERENCES", 2)
    assert len(parse_tei(fixture("full_paper.tei.xml"), page_count=5).references) == 2


def test_parsed_text_never_contains_markup(full):
    for section in full.sections:
        for paragraph in section.paragraphs:
            assert "<" not in paragraph.text and "coords=" not in paragraph.text


# --- Day 6 quality pass: heading numbers, levels, inheritance ---------------------------

from app.services.tei_parser import level_for_number, split_heading_number  # noqa: E402


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("II.", ("II.", None, "roman")),
        ("VI. INNOVATIVE CONTENt", ("VI.", "INNOVATIVE CONTENt", "roman")),
        ("XIV. Appendix", ("XIV.", "Appendix", "roman")),
        ("A. SYNTHETIC DATA GENERATION MODULE", ("A.", "SYNTHETIC DATA GENERATION MODULE", "letter")),
        ("B.", ("B.", None, "letter")),
        ("3.2 Training details", ("3.2", "Training details", "arabic")),
        ("4. Results", ("4.", "Results", "arabic")),
        ("Results", (None, "Results", None)),
        ("A Survey of Retrieval", (None, "A Survey of Retrieval", None)),  # no dot: not a number
        ("IIII. Not Roman", (None, "IIII. Not Roman", None)),
        ("2 = x + y", (None, "2 = x + y", None)),
        ("2026 Results", (None, "2026 Results", None)),  # a year, not a section number
        ("   ", (None, None, None)),
        (None, (None, None, None)),
    ],
)
def test_split_heading_number(raw, expected):
    assert split_heading_number(raw) == expected


@pytest.mark.parametrize(
    ("number", "numbering", "level"),
    [("II.", "roman", 1), ("A.", "letter", 2), ("3", "arabic", 1), ("3.2", "arabic", 2), ("3.2.1", "arabic", 3), (None, None, 1)],
)
def test_level_for_number(number, numbering, level):
    assert level_for_number(number, numbering) == level


def _tei(*divs: str) -> bytes:
    body = "".join(f'<div xmlns="http://www.tei-c.org/ns/1.0">{d}</div>' for d in divs)
    return f'<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body>{body}</body></text></TEI>'.encode()


P = '<p coords="1,1,1,1,1">Some body text of this section.</p>'


def test_roman_and_letter_numbers_are_split_from_headings():
    doc = parse_tei(_tei(f"<head>VII. PROPOSED SYSTEM METHODOLOGY</head>{P}", f"<head>A. Synthetic Data Module</head>{P}"), page_count=1)
    first, second = doc.sections
    assert (first.number, first.heading, first.level, first.section_type) == ("VII.", "PROPOSED SYSTEM METHODOLOGY", 1, "methodology")
    assert (second.number, second.heading, second.level) == ("A.", "Synthetic Data Module", 2)
    assert second.section_type == "methodology"  # unclassified subsection inherits its parent's type


def test_number_only_heading_keeps_number_and_no_invented_title():
    doc = parse_tei(_tei(f"<head>II.</head><p>RELATED WORK OR LITERATURE STUDIES The authors of [1].</p>"), page_count=1)
    section = doc.sections[0]
    assert (section.number, section.heading, section.section_type) == ("II.", None, "other")
    # Without PDF evidence the parser does not move text into the heading (see structure_repair).
    assert section.paragraphs[0].text.startswith("RELATED WORK OR LITERATURE STUDIES")


def test_missing_heading():
    doc = parse_tei(_tei(P), page_count=1)
    assert (doc.sections[0].heading, doc.sections[0].number, doc.sections[0].level) == (None, None, 1)


def test_grobid_arabic_number_attribute_is_used():
    doc = parse_tei(_tei(f'<head n="2.1">Data Collection</head>{P}'), page_count=1)
    assert (doc.sections[0].number, doc.sections[0].heading, doc.sections[0].level) == ("2.1", "Data Collection", 2)


def test_nested_headings_inherit_from_the_nearest_parent_only():
    doc = parse_tei(_tei(
        f"<head>III. Methods</head>{P}",
        f"<head>A. Pipeline</head>{P}",
        f"<head>IV. Results</head>{P}",
        f"<head>A. Accuracy breakdown</head>{P}",
        f"<head>B. Error analysis</head>{P}",
        f"<head>V. Acknowledgements</head>{P}",
    ), page_count=1)
    assert [(s.number, s.level, s.section_type) for s in doc.sections] == [
        ("III.", 1, "methodology"), ("A.", 2, "methodology"),
        ("IV.", 1, "results"), ("A.", 2, "results"), ("B.", 2, "results"),
        ("V.", 1, "other"),  # a top-level section never inherits
    ]


def test_heading_only_section_is_kept_in_the_outline():
    doc = parse_tei(_tei('<head n="4">Method</head>', f'<head n="4.1">Encoder</head>{P}'), page_count=1)
    assert [(s.number, s.heading, len(s.paragraphs)) for s in doc.sections] == [("4", "Method", 0), ("4.1", "Encoder", 1)]
    assert doc.sections[1].section_type == "methodology"
