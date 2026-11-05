"""Parse GROBID TEI XML into plain dataclasses (Day 6). No I/O, no database.

Only the parts of GROBID's TEI that SYNTHORA uses are read:

  teiHeader/fileDesc/titleStmt/title                      -> title
  teiHeader/fileDesc/sourceDesc/biblStruct
      analytic/author/persName (+ affiliation/orgName)    -> authors
      idno[@type="DOI"]                                   -> doi
      monogr/imprint/date[@type="published"]/@when        -> publication year
      monogr/title[@level="j" | "m"] or monogr/meeting    -> venue
  teiHeader/profileDesc/abstract                          -> abstract (also an "abstract" section)
  text/body/div (+ text/back/div except references)       -> sections: <head n="2.1">, <p>, <s>, list items
  text/back//listBibl/biblStruct                          -> references (+ note[@type="raw_reference"])

GROBID emits divs flat. The section number is taken from <head n="2.1"> (GROBID only
splits Arabic numbers) or from the start of the heading text ("VI. RESULTS", "A. Setup"),
and the level from the numbering style: Roman -> 1, letter -> 2, "2.1" -> 2.

Blocks of a section, in document order (kind):
  <p>, list items        -> "text"
  <formula>              -> "formula"   (kept: it is source content)
Figures and tables (<figure>, outside the divs) are returned separately in
TeiDocument.figures with their coordinates; structure_repair places them into the
section at the same position. Footnotes are not section text.

Headings, numbers and blocks are taken from GROBID as they are. Evidence-based
corrections (headings GROBID split, merged or missed) happen in structure_repair.py,
which also has the PDF's own text lines.

Page numbers come from `coords="page,x,y,w,h;..."` (1-based). Any page outside
1..page_count is ignored rather than trusted.

Security: GROBID never emits a DOCTYPE, so any DOCTYPE/ENTITY declaration is
rejected before parsing. ElementTree does not fetch external entities, and Expat
>= 2.4.1 limits entity amplification ("billion laughs"); older Expat is refused.
"""

import pyexpat
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass, field

from app.models.paper_section import SECTION_TYPES
from app.schemas.paper import normalize_doi

TEI_NS = "http://www.tei-c.org/ns/1.0"
_NS = {"tei": TEI_NS}
MIN_SAFE_EXPAT = (2, 4, 1)

MAX_SECTIONS = 2000
MAX_REFERENCES = 2000
MAX_AUTHORS = 500
MAX_TITLE_CHARS = 1000
MAX_ABSTRACT_CHARS = 20_000
MAX_VENUE_CHARS = 1000
MAX_REFERENCE_TEXT_CHARS = 5000
MAX_AUTHOR_NAME_CHARS = 300
MAX_AFFILIATION_CHARS = 500

_WS = re.compile(r"\s+")
_FORBIDDEN_DECLARATIONS = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)
_YEAR = re.compile(r"^(\d{4})")
_SECTION_NUMBER = re.compile(r"^\d+(\.\d+)*\.?$")
# Section numbering at the start of a heading. Roman numerals are limited to valid forms
# up to XXXIX and need a dot ("II."), letters are a single capital with a dot ("B."),
# Arabic numbers have at most 2 digits per level ("3", "3.2", "3.2.1.").
_ROMAN = r"(?=[IVX])X{0,3}(?:IX|IV|V?I{0,3})"
_HEADING_NUMBER = re.compile(
    r"^(?P<number>(?P<roman>" + _ROMAN + r")\.|(?P<letter>[A-Z])\.|(?P<arabic>\d{1,2}(?:\.\d{1,2})*)\.?)"
    r"(?:\s+(?P<title>\S.*))?$"
)


class TeiParseError(Exception):
    """The TEI is unusable (malformed, unsafe or without body text). Internal message."""


class UnsafeXmlParserError(TeiParseError):
    """The Python runtime's Expat is too old to parse untrusted XML safely."""


# (page, x0, y0, x1, y1) in PDF points, origin top-left, 1-based page.
Box = tuple[int, float, float, float, float]

BLOCK_KINDS = ("text", "table", "formula", "caption")


@dataclass(frozen=True, slots=True)
class TeiSentence:
    text: str
    page_start: int | None
    page_end: int | None
    boxes: tuple[Box, ...] = ()


@dataclass(frozen=True, slots=True)
class TeiParagraph:
    """One block of a section. kind: text | table | formula | caption (BLOCK_KINDS)."""

    text: str
    sentences: tuple[TeiSentence, ...]
    page_start: int | None
    page_end: int | None
    kind: str = "text"
    boxes: tuple[Box, ...] = ()


@dataclass(slots=True)
class TeiSection:
    heading: str | None
    number: str | None
    level: int
    section_type: str
    paragraphs: list[TeiParagraph] = field(default_factory=list)
    head_page: int | None = None
    # "roman" | "letter" | "arabic" | None: decides the level (see level_for_number).
    numbering: str | None = None


@dataclass(frozen=True, slots=True)
class TeiFigure:
    """A GROBID <figure> (kind "figure") or <figure type="table"> (kind "table")."""

    kind: str
    text: str
    page_start: int | None
    page_end: int | None
    boxes: tuple[Box, ...] = ()


@dataclass(frozen=True, slots=True)
class TeiAuthor:
    name: str
    affiliation: str | None


@dataclass(frozen=True, slots=True)
class TeiHeader:
    title: str | None
    authors: tuple[TeiAuthor, ...]
    abstract: str | None
    doi: str | None
    publication_year: int | None
    venue: str | None


@dataclass(frozen=True, slots=True)
class TeiReference:
    raw_text: str | None
    title: str | None
    authors: tuple[str, ...]
    publication_year: int | None
    venue: str | None
    doi: str | None
    page_start: int | None
    page_end: int | None


@dataclass(frozen=True, slots=True)
class TeiDocument:
    header: TeiHeader
    sections: tuple[TeiSection, ...]
    references: tuple[TeiReference, ...]
    figures: tuple[TeiFigure, ...] = ()

    @property
    def paragraph_count(self) -> int:
        return sum(len(s.paragraphs) for s in self.sections)


# --- public entry point ---------------------------------------------------------------


def parse_tei(tei: bytes, *, page_count: int) -> TeiDocument:
    """Parse GROBID TEI. Raises TeiParseError if it is unusable."""
    if pyexpat.version_info < MIN_SAFE_EXPAT:
        raise UnsafeXmlParserError(f"Expat {pyexpat.EXPAT_VERSION} is older than 2.4.1; refusing to parse XML")
    if _FORBIDDEN_DECLARATIONS.search(tei):
        raise TeiParseError("TEI contains a DOCTYPE or ENTITY declaration")
    try:
        root = ET.fromstring(tei)
    except ET.ParseError as exc:
        raise TeiParseError(f"malformed XML: {exc}") from exc
    if root.tag != f"{{{TEI_NS}}}TEI":
        raise TeiParseError("root element is not tei:TEI")

    header = _parse_header(root)
    sections = _parse_sections(root, header, page_count)
    references = _parse_references(root, page_count)
    figures = _parse_figures(root, page_count)
    document = TeiDocument(
        header=header, sections=tuple(sections), references=tuple(references), figures=tuple(figures)
    )
    if not any(s.section_type != "abstract" for s in document.sections if s.paragraphs):
        raise TeiParseError("TEI contains no body paragraphs")
    return document


# --- helpers --------------------------------------------------------------------------


def normalize_text(value: str | None) -> str:
    return _WS.sub(" ", value or "").strip()


def _text(element: ET.Element | None) -> str:
    return "" if element is None else normalize_text("".join(element.itertext()))


def _clip(value: str, limit: int) -> str | None:
    value = value.strip()
    if not value:
        return None
    return value if len(value) <= limit else value[:limit].rstrip()


def parse_coords(value: str | None, page_count: int) -> tuple[int, int] | None:
    """`coords="3,10.0,20.0,30.0,8.0;4,..."` -> (3, 4). Invalid boxes are skipped."""
    if not value:
        return None
    pages: list[int] = []
    for box in value.split(";"):
        first = box.split(",", 1)[0].strip()
        if first.isdigit():
            page = int(first)
            if 1 <= page <= page_count:
                pages.append(page)
    if not pages:
        return None
    return min(pages), max(pages)


def parse_boxes(value: str | None, page_count: int) -> tuple[Box, ...]:
    """`coords` -> boxes (page, x0, y0, x1, y1). Malformed boxes and pages outside the PDF are skipped."""
    boxes: list[Box] = []
    for raw in (value or "").split(";"):
        parts = raw.split(",")
        if len(parts) != 5 or not parts[0].strip().isdigit():
            continue
        try:
            page = int(parts[0])
            x, y, w, h = (float(v) for v in parts[1:])
        except ValueError:
            continue
        if 1 <= page <= page_count and w >= 0 and h >= 0:
            boxes.append((page, x, y, x + w, y + h))
    return tuple(boxes)


def _span(*spans: tuple[int, int] | None) -> tuple[int | None, int | None]:
    known = [s for s in spans if s is not None]
    if not known:
        return None, None
    return min(s[0] for s in known), max(s[1] for s in known)


def _year(when: str | None) -> int | None:
    match = _YEAR.match((when or "").strip())
    if not match:
        return None
    year = int(match.group(1))
    return year if 1800 <= year <= 2100 else None


def _person_name(pers: ET.Element | None) -> str | None:
    if pers is None:
        return None
    parts = [_text(e) for e in pers.findall("tei:forename", _NS)]
    parts += [_text(e) for e in pers.findall("tei:surname", _NS)]
    name = " ".join(p for p in parts if p) or _text(pers)
    return _clip(name, MAX_AUTHOR_NAME_CHARS)


def _affiliation(author: ET.Element) -> str | None:
    affiliation = author.find("tei:affiliation", _NS)
    if affiliation is None:
        return None
    names = [_text(org) for org in affiliation.findall("tei:orgName", _NS)]
    joined = ", ".join(n for n in names if n) or _text(affiliation.find("tei:note[@type='raw_affiliation']", _NS))
    return _clip(joined, MAX_AFFILIATION_CHARS)


def clean_doi(value: str | None) -> str | None:
    """Validated, prefix-free DOI (same rules as manual edits), or None."""
    value = normalize_text(value)
    if not value or len(value) > 255:
        return None
    try:
        return normalize_doi(value)
    except ValueError:
        return None


def _doi(bibl: ET.Element) -> str | None:
    for idno in bibl.iter(f"{{{TEI_NS}}}idno"):
        if (idno.get("type") or "").upper() == "DOI":
            return clean_doi(_text(idno))
    return None


def _venue(monogr: ET.Element | None) -> str | None:
    if monogr is None:
        return None
    for path in ("tei:title[@level='j'][@type='main']", "tei:title[@level='j']", "tei:title[@level='m']", "tei:meeting"):
        value = _text(monogr.find(path, _NS))
        if value:
            return _clip(value, MAX_VENUE_CHARS)
    return None


def _published_year(bibl: ET.Element) -> int | None:
    for date in bibl.iter(f"{{{TEI_NS}}}date"):
        if date.get("type") == "published":
            year = _year(date.get("when")) or _year(_text(date))
            if year:
                return year
    return None


# --- header ---------------------------------------------------------------------------


def _parse_header(root: ET.Element) -> TeiHeader:
    file_desc = root.find("tei:teiHeader/tei:fileDesc", _NS)
    title = None
    authors: list[TeiAuthor] = []
    doi = year = venue = None
    if file_desc is not None:
        title_el = file_desc.find("tei:titleStmt/tei:title[@type='main']", _NS)
        if title_el is None:
            title_el = file_desc.find("tei:titleStmt/tei:title", _NS)
        title = _clip(_text(title_el), MAX_TITLE_CHARS)
        bibl = file_desc.find("tei:sourceDesc/tei:biblStruct", _NS)
        if bibl is not None:
            for author in bibl.findall("tei:analytic/tei:author", _NS)[:MAX_AUTHORS]:
                name = _person_name(author.find("tei:persName", _NS))
                if name:  # GROBID sometimes emits an <author> holding only an affiliation
                    authors.append(TeiAuthor(name=name, affiliation=_affiliation(author)))
            doi = _doi(bibl)
            year = _published_year(bibl)
            venue = _venue(bibl.find("tei:monogr", _NS))
        publication_stmt = file_desc.find("tei:publicationStmt", _NS)
        if year is None and publication_stmt is not None:
            year = _published_year(publication_stmt)

    abstract_el = root.find("tei:teiHeader/tei:profileDesc/tei:abstract", _NS)
    abstract = None
    if abstract_el is not None:
        paragraphs = [_text(p) for p in abstract_el.iter(f"{{{TEI_NS}}}p")]
        joined = "\n\n".join(p for p in paragraphs if p) or _text(abstract_el)
        abstract = _clip(joined, MAX_ABSTRACT_CHARS)

    return TeiHeader(
        title=title, authors=tuple(authors), abstract=abstract, doi=doi, publication_year=year, venue=venue
    )


# --- sections -------------------------------------------------------------------------

# Checked in order; the first keyword phrase found (as whole words) in the heading wins.
# Deliberately conservative: only phrases that name the section's role. Generic words
# ("model", "framework", "overview", "motivation", "summary", "setup", "dataset",
# "implementation", "proposed" on its own) were removed in the Day 6 quality pass because
# they classified legitimate sections wrongly. Unmatched headings stay "other"; the
# heading text itself is always kept.
_SECTION_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("references", ("references", "bibliography")),
    ("abstract", ("abstract",)),
    ("introduction", ("introduction",)),
    ("related_work", (
        "related work", "related works", "literature review", "literature survey", "literature studies",
        "review of literature", "prior work", "previous work", "state of the art", "background",
    )),
    ("conclusion", ("conclusion", "conclusions", "concluding remarks", "future work")),
    ("results", ("results", "findings")),
    ("discussion", ("discussion", "limitations", "threats to validity")),
    ("experiments", ("experiment", "experiments", "experimental", "evaluation")),
    ("methodology", (
        "method", "methods", "methodology", "approach", "materials and methods",
        "proposed method", "proposed approach", "proposed system", "proposed framework", "proposed model",
    )),
)
assert all(kind in SECTION_TYPES for kind, _ in _SECTION_KEYWORDS)
_KEYWORD_PATTERNS = tuple(
    (kind, re.compile(r"\b(" + "|".join(re.escape(k) for k in words) + r")\b", re.IGNORECASE))
    for kind, words in _SECTION_KEYWORDS
)


def classify_heading(heading: str | None) -> str:
    if not heading:
        return "other"
    for kind, pattern in _KEYWORD_PATTERNS:
        if pattern.search(heading):
            return kind
    return "other"


def split_heading_number(text: str | None) -> tuple[str | None, str | None, str | None]:
    """"VI. INNOVATIVE CONTENT" -> ("VI.", "INNOVATIVE CONTENT", "roman").
    "II." -> ("II.", None, "roman"). "Results" -> (None, "Results", None)."""
    text = normalize_text(text)
    if not text:
        return None, None, None
    match = _HEADING_NUMBER.match(text)
    if not match:
        return None, text, None
    style = "roman" if match.group("roman") else "letter" if match.group("letter") else "arabic"
    title = match.group("title")
    if style == "arabic" and title is not None and not title[:1].isalpha():
        return None, text, None  # "2 = x + y", "10 %": not a numbered heading
    return match.group("number"), (title.strip() if title else None), style


def _level(number: str | None) -> int:
    """Depth of an Arabic number: "3" -> 1, "3.2" -> 2 (capped at 6)."""
    if not number:
        return 1
    return min(len([p for p in number.strip(".").split(".") if p]), 6) or 1


def level_for_number(number: str | None, numbering: str | None) -> int:
    if not number:
        return 1
    if numbering == "roman":
        return 1
    if numbering == "letter":
        return 2
    return _level(number)


def assign_levels_and_types(sections: list["TeiSection"]) -> None:
    """Level from the numbering style; an unclassified subsection inherits the type of the
    nearest preceding section with a smaller level ("3.2 Training details" under "3 Method",
    "A. Synthetic Data Module" under "VII. Proposed System Methodology")."""
    stack: list[TeiSection] = []
    for section in sections:
        if section.section_type == "abstract":
            stack = []
            continue
        section.level = level_for_number(section.number, section.numbering)
        section.section_type = classify_heading(section.heading)
        while stack and stack[-1].level >= section.level:
            stack.pop()
        if section.section_type == "other" and stack and section.level > 1:
            section.section_type = stack[-1].section_type
        stack.append(section)


def _sentences(p: ET.Element, page_count: int) -> tuple[TeiSentence, ...]:
    out = []
    for s in p.findall("tei:s", _NS):
        text = _text(s)
        if text:
            start, end = _span(parse_coords(s.get("coords"), page_count))
            out.append(TeiSentence(text=text, page_start=start, page_end=end, boxes=parse_boxes(s.get("coords"), page_count)))
    return tuple(out)


def _paragraph(p: ET.Element, page_count: int, kind: str = "text") -> TeiParagraph | None:
    sentences = _sentences(p, page_count)
    text = " ".join(s.text for s in sentences) if sentences else _text(p)
    if not text:
        return None
    sentence_spans = [(s.page_start, s.page_end) for s in sentences if s.page_start is not None]
    start, end = _span(parse_coords(p.get("coords"), page_count), *sentence_spans)
    return TeiParagraph(
        text=text, sentences=sentences, page_start=start, page_end=end, kind=kind,
        boxes=parse_boxes(p.get("coords"), page_count),
    )


def _div_blocks(div: ET.Element) -> Iterator[tuple[ET.Element, str]]:
    """Block children of a div in document order, with their kind (lists: one block per item)."""
    for child in div:
        if child.tag == f"{{{TEI_NS}}}p":
            yield child, "text"
        elif child.tag == f"{{{TEI_NS}}}list":
            for item in child.findall("tei:item", _NS):
                yield item, "text"
        elif child.tag == f"{{{TEI_NS}}}formula":
            yield child, "formula"


def _abstract_section(root: ET.Element, page_count: int) -> TeiSection | None:
    abstract_el = root.find("tei:teiHeader/tei:profileDesc/tei:abstract", _NS)
    if abstract_el is None:
        return None
    section = TeiSection(heading="Abstract", number=None, level=1, section_type="abstract")
    for p in abstract_el.iter(f"{{{TEI_NS}}}p"):
        paragraph = _paragraph(p, page_count)
        if paragraph:
            section.paragraphs.append(paragraph)
    return section if section.paragraphs else None


def _parse_sections(root: ET.Element, header: TeiHeader, page_count: int) -> list[TeiSection]:
    sections: list[TeiSection] = []
    abstract = _abstract_section(root, page_count)
    if abstract:
        sections.append(abstract)

    divs: list[ET.Element] = []
    body = root.find("tei:text/tei:body", _NS)
    if body is not None:
        divs += body.findall("tei:div", _NS)
    back = root.find("tei:text/tei:back", _NS)
    if back is not None:
        for div in back.iter(f"{{{TEI_NS}}}div"):
            if div.get("type") == "references" or div.find("tei:listBibl", _NS) is not None:
                continue
            if div.find("tei:p", _NS) is not None or div.find("tei:list", _NS) is not None:
                divs.append(div)

    for div in divs:
        if len(sections) >= MAX_SECTIONS:
            break
        head = div.find("tei:head", _NS)
        raw_heading = _clip(_text(head), MAX_TITLE_CHARS) if head is not None else None
        grobid_number = (head.get("n") or "").strip() if head is not None else ""
        if grobid_number and _SECTION_NUMBER.fullmatch(grobid_number):
            number, heading, numbering = grobid_number, raw_heading, "arabic"
        else:
            number, heading, numbering = split_heading_number(raw_heading)
        kind = classify_heading(heading)
        if kind in ("abstract", "references"):
            # A body heading "Abstract" duplicates the header abstract; "References" is
            # parsed from listBibl. Neither becomes body text.
            if kind == "references" or abstract is not None:
                continue
        head_span = parse_coords(head.get("coords"), page_count) if head is not None else None
        section = TeiSection(
            heading=heading, number=number, level=1, section_type=kind,
            head_page=head_span[0] if head_span else None, numbering=numbering,
        )
        for block, block_kind in _div_blocks(div):
            paragraph = _paragraph(block, page_count, block_kind)
            if paragraph:
                section.paragraphs.append(paragraph)
        # A heading with no text of its own (e.g. "4 Method" directly followed by "4.1") is
        # kept: it is part of the outline and the parent of its subsections. A division with
        # neither heading nor text carries nothing.
        if section.paragraphs or section.heading or section.number:
            sections.append(section)
    assign_levels_and_types(sections)
    return sections


def _figure_text(figure: ET.Element) -> str:
    caption = _text(figure.find("tei:figDesc", _NS)) or _text(figure.find("tei:head", _NS))
    table = figure.find("tei:table", _NS)
    if table is None:
        return caption
    rows = []
    for row in table.findall("tei:row", _NS):
        cells = [_text(cell) for cell in row.findall("tei:cell", _NS)]
        if any(cells):
            rows.append(" | ".join(cells))
    if not rows:
        rows = [line for line in (normalize_text(t) for t in table.itertext()) if line]
    return "\n".join([caption, *rows] if caption else rows)


def _parse_figures(root: ET.Element, page_count: int) -> list[TeiFigure]:
    """Every <figure> of the body with text (graphic-only figures carry no text)."""
    body = root.find("tei:text/tei:body", _NS)
    if body is None:
        return []
    figures = []
    for figure in body.iter(f"{{{TEI_NS}}}figure"):
        text = _figure_text(figure)
        if not text:
            continue
        boxes = parse_boxes(figure.get("coords"), page_count)
        start, end = _span(parse_coords(figure.get("coords"), page_count))
        kind = "table" if figure.get("type") == "table" else "figure"
        figures.append(TeiFigure(kind=kind, text=text, page_start=start, page_end=end, boxes=boxes))
    return figures


# --- references -----------------------------------------------------------------------


def _parse_references(root: ET.Element, page_count: int) -> list[TeiReference]:
    back = root.find("tei:text/tei:back", _NS)
    if back is None:
        return []
    references: list[TeiReference] = []
    for bibl in back.iter(f"{{{TEI_NS}}}biblStruct"):
        if len(references) >= MAX_REFERENCES:
            break
        analytic = bibl.find("tei:analytic", _NS)
        monogr = bibl.find("tei:monogr", _NS)
        title_el = analytic.find("tei:title", _NS) if analytic is not None else None
        if title_el is None and monogr is not None:
            title_el = monogr.find("tei:title", _NS)
        title = _clip(_text(title_el), MAX_TITLE_CHARS)
        author_parent = analytic if analytic is not None and analytic.find("tei:author", _NS) is not None else monogr
        authors: list[str] = []
        if author_parent is not None:
            for author in author_parent.findall("tei:author", _NS)[:MAX_AUTHORS]:
                name = _person_name(author.find("tei:persName", _NS))
                if name:
                    authors.append(name)
        raw = _clip(_text(bibl.find("tei:note[@type='raw_reference']", _NS)), MAX_REFERENCE_TEXT_CHARS)
        if raw is None and title is None:
            continue  # nothing citable
        start, end = _span(parse_coords(bibl.get("coords"), page_count))
        references.append(
            TeiReference(
                raw_text=raw,
                title=title,
                authors=tuple(authors),
                publication_year=_published_year(bibl),
                venue=_venue(monogr) if analytic is not None else None,
                doi=_doi(bibl),
                page_start=start,
                page_end=end,
            )
        )
    return references
