"""Evidence-based repair of GROBID's section structure (Day 6 quality pass). Pure functions.

GROBID's heading detection is statistical. On papers whose headings look like body text
(same font, size and weight) it splits, merges or misses headings. This module corrects
those cases, but ONLY with evidence from the PDF itself: the page text extracted by
PyMuPDF (Day 5), in which every printed line is preserved. Nothing is invented. Text is
moved (never rewritten or dropped), and when the evidence is missing nothing changes.

Evidence: the numbered heading lines of the PDF (`PdfLineIndex.headings`): a line
"VI. Results And Discussion", or a line "II." directly followed by the line
"RELATED WORK OR LITERATURE STUDIES". Only Roman and letter numbering is used here;
GROBID already handles Arabic numbering itself, and numbered list items ("1. Load the
data") are too easily mistaken for headings.

Repairs, in order (each counted in RepairReport):
  1. caption merged into a heading   "TABLE I. <caption> B. Behavioral Feature Distribution"
                                     -> heading "Behavioral Feature Distribution" (B.), caption kept as a block
  2. equation taken for a heading    "DTI = Total EMI Outflows / Monthly Salary Income(1)"
                                     -> formula block of the previous section, its text moves there
  3. number-only heading             "II." + paragraph "RELATED WORK OR LITERATURE STUDIES The authors…"
                                     -> heading "RELATED WORK OR LITERATURE STUDIES" (II.)
  4. number missing from a heading   "PROBLEM DOMAIN" printed as "IV." + "PROBLEM DOMAIN"
                                     -> number "IV."; a trailing "IV." left at the end of the
                                        previous section's last paragraph is removed (it moved)
  5. heading trapped in a paragraph  "…downstream product mapping. VI. Results And Discussion This section…"
                                     -> new section starting at the heading, with the rest of the
                                        paragraph and the following paragraphs of that section
  then levels and types are recomputed (tei_parser.assign_levels_and_types),
  6. table text: sentences whose PDF boxes lie inside a PyMuPDF table region become a
     "table" block (never chunked together with prose),
  7. figures / GROBID tables are inserted at their position as "caption" / "table" blocks,
  8. a paragraph that is exactly a printed caption line ("Fig. 3. …") becomes a "caption" block.

Known limitations (documented, not guessed): a table row GROBID placed in the wrong section
stays there; table regions are matched on unrotated pages only; figure placement uses
(page, y) order, which can be off within a page of a two-column layout.
"""

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.services.tei_parser import (
    Box,
    TeiFigure,
    TeiParagraph,
    TeiSection,
    TeiSentence,
    assign_levels_and_types,
    normalize_text,
    split_heading_number,
)

logger = logging.getLogger(__name__)

# (x0, y0, x1, y1) in PDF points, one tuple per detected table, per page.
TableRegion = tuple[float, float, float, float]

_CAPTION_START = re.compile(r"^(?:fig\.?|figure|table)\s*(?:[ivx]+|\d+)\b", re.IGNORECASE)
_CAPTION_ANYWHERE = re.compile(r"(?:^|\s)(?:fig\.?|figure|table)\s*(?:[ivx]+|\d+)\b", re.IGNORECASE)
_EQUATION_NUMBER_END = re.compile(r"\(\d{1,3}\)\s*$")
MAX_TITLE_WORDS = 15
TABLE_OVERLAP = 0.6  # share of a sentence's box area that must lie inside a table region


@dataclass(frozen=True, slots=True)
class PdfHeading:
    page: int
    number: str
    title: str
    numbering: str  # "roman" | "letter"

    @property
    def full(self) -> str:
        return f"{self.number} {self.title}"


@dataclass(slots=True)
class RepairReport:
    captions_split_from_headings: int = 0
    equations_demoted: int = 0
    titles_recovered: int = 0
    numbers_recovered: int = 0
    number_fragments_removed: int = 0
    trapped_headings_recovered: int = 0
    table_blocks: int = 0
    figures_placed: int = 0
    captions_marked: int = 0

    def total(self) -> int:
        return sum(getattr(self, name) for name in self.__slots__)


@dataclass(slots=True)
class RepairResult:
    sections: list[TeiSection]
    report: RepairReport = field(default_factory=RepairReport)


# --- evidence: the PDF's own lines --------------------------------------------------------


def _plausible_title(text: str) -> bool:
    words = text.split()
    return (
        0 < len(words) <= MAX_TITLE_WORDS
        and text[:1].isalpha()
        and not text.endswith((".", ",", ";"))
        and text.count(",") < 2
        and split_heading_number(text)[0] is None
        and not _CAPTION_START.match(text)
    )


class PdfLineIndex:
    """Normalised text lines per page, and the numbered heading lines among them."""

    def __init__(self, page_texts: Sequence[str]) -> None:
        self.lines: list[list[str]] = [
            [line for line in (normalize_text(raw) for raw in text.split("\n")) if line] for text in page_texts
        ]
        self.headings: list[PdfHeading] = []
        for page, lines in enumerate(self.lines, start=1):
            for i, line in enumerate(lines):
                number, title, numbering = split_heading_number(line)
                if numbering not in ("roman", "letter"):
                    continue
                if title is None and i + 1 < len(lines):
                    title = lines[i + 1]  # "II." on its own line, title on the next one
                if title is not None and _plausible_title(title):
                    self.headings.append(PdfHeading(page=page, number=number, title=title, numbering=numbering))

    @property
    def page_count(self) -> int:
        return len(self.lines)

    def near(self, page: int | None, spread: int = 1) -> list[PdfHeading]:
        if page is None:
            return []
        return [h for h in self.headings if abs(h.page - page) <= spread]

    def is_printed(self, text: str, page: int | None, spread: int = 1, max_lines: int = 3) -> bool:
        """True if `text` is one PDF line, or up to `max_lines` consecutive lines, near `page`."""
        text = normalize_text(text)
        if not text or page is None:
            return False
        for p in range(max(1, page - spread), min(self.page_count, page + spread) + 1):
            lines = self.lines[p - 1]
            for i in range(len(lines)):
                joined = lines[i]
                if joined == text:
                    return True
                for k in range(1, max_lines):
                    if i + k >= len(lines) or len(joined) >= len(text):
                        break
                    joined = f"{joined} {lines[i + k]}"
                    if joined == text:
                        return True
        return False


# --- text surgery that keeps sentences, pages and boxes -----------------------------------


def _block(text: str, sentences: Sequence[TeiSentence], like: TeiParagraph, kind: str | None = None) -> TeiParagraph | None:
    text = normalize_text(text)
    if not text:
        return None
    pages = [(s.page_start, s.page_end) for s in sentences if s.page_start is not None]
    if pages:
        start, end = min(p[0] for p in pages), max(p[1] for p in pages)
    else:
        start, end = like.page_start, like.page_end
    boxes = tuple(b for s in sentences for b in s.boxes) if sentences else like.boxes
    return TeiParagraph(
        text=text, sentences=tuple(sentences), page_start=start, page_end=end, kind=kind or like.kind, boxes=boxes
    )


def cut_block(block: TeiParagraph, start: int, end: int) -> tuple[TeiParagraph | None, TeiParagraph | None]:
    """Remove block.text[start:end]; return what remains before and after (None when empty).
    A sentence cut in two keeps its original pages and boxes on both halves."""
    if not block.sentences:
        return _block(block.text[:start], (), block), _block(block.text[end:], (), block)
    before: list[TeiSentence] = []
    after: list[TeiSentence] = []
    position = 0
    for sentence in block.sentences:
        s_start, s_end = position, position + len(sentence.text)
        position = s_end + 1  # sentences are joined with one space
        if s_end <= start:
            before.append(sentence)
        elif s_start >= end:
            after.append(sentence)
        else:
            head = sentence.text[: max(0, start - s_start)].strip()
            tail = sentence.text[max(0, end - s_start):].strip()
            if head:
                before.append(TeiSentence(head, sentence.page_start, sentence.page_end, sentence.boxes))
            if tail:
                after.append(TeiSentence(tail, sentence.page_start, sentence.page_end, sentence.boxes))
    return (
        _block(" ".join(s.text for s in before), before, block),
        _block(" ".join(s.text for s in after), after, block),
    )


def _find_phrase(text: str, phrase: str) -> int:
    """Index of `phrase` in `text` at word boundaries, or -1."""
    match = re.search(r"(?:(?<=\s)|^)" + re.escape(phrase) + r"(?=\s|$)", text)
    return match.start() if match else -1


def _first_page(section: TeiSection) -> int | None:
    if section.head_page is not None:
        return section.head_page
    return next((p.page_start for p in section.paragraphs if p.page_start is not None), None)


def _plain(text: str, page: int | None, kind: str) -> TeiParagraph:
    return TeiParagraph(text=text, sentences=(), page_start=page, page_end=page, kind=kind)


def _copy(section: TeiSection) -> TeiSection:
    return TeiSection(
        heading=section.heading, number=section.number, level=section.level, section_type=section.section_type,
        paragraphs=list(section.paragraphs), head_page=section.head_page, numbering=section.numbering,
    )


# --- repairs --------------------------------------------------------------------------------


def repair_structure(
    sections: Sequence[TeiSection],
    page_texts: Sequence[str],
    *,
    figures: Sequence[TeiFigure] = (),
    table_regions: Sequence[Sequence[TableRegion]] = (),
) -> RepairResult:
    result = RepairResult(sections=[_copy(s) for s in sections])
    index = PdfLineIndex(page_texts)
    used: set[PdfHeading] = set()
    work = result.sections
    report = result.report

    _split_captions_from_headings(work, index, used, report)
    _demote_equation_headings(work, index, report)
    _recover_titles_of_number_only_headings(work, index, used, report)
    _recover_numbers(work, index, used, report)
    _recover_trapped_headings(work, index, used, report)
    for section in work:
        section.paragraphs = [p for p in section.paragraphs if p.text]
    assign_levels_and_types(work)
    _mark_table_text(work, table_regions, report)
    _place_figures(work, figures, report)
    _mark_printed_captions(work, index, report)
    if report.total():
        logger.info("Structure repair: %s", {k: getattr(report, k) for k in report.__slots__ if getattr(report, k)})
    return result


def _is_body(section: TeiSection) -> bool:
    return section.section_type != "abstract"


def _split_captions_from_headings(work: list[TeiSection], index: PdfLineIndex, used: set, report: RepairReport) -> None:
    for position, section in enumerate(work):
        if not _is_body(section) or not section.heading or not _CAPTION_ANYWHERE.search(section.heading):
            continue
        heading = section.heading if section.number is None else f"{section.number} {section.heading}"
        page = _first_page(section)
        for candidate in index.near(page):
            if candidate in used:
                continue
            for target in (candidate.full, candidate.title):
                at = _find_phrase(heading, target)
                if at < 0:
                    continue
                before, after = heading[:at].strip(), heading[at + len(target):].strip()
                parts = [p for p in (before, after) if p]
                if not parts or not all(_CAPTION_START.match(p) and index.is_printed(p, page) for p in parts):
                    continue
                had_number = section.number is not None
                section.heading, section.number, section.numbering = candidate.title, candidate.number, candidate.numbering
                used.add(candidate)
                report.captions_split_from_headings += 1
                if not had_number:
                    report.numbers_recovered += 1
                    _strip_trailing_number_before(work, position, candidate.number, report)
                if before:  # printed before the heading: belongs to the end of the previous section
                    target_section = work[position - 1] if position > 0 else section
                    caption = _plain(before, page, "caption")
                    if target_section is section:
                        section.paragraphs.insert(0, caption)
                    else:
                        target_section.paragraphs.append(caption)
                if after:  # printed after the heading: the start of this section
                    section.paragraphs.insert(0, _plain(after, page, "caption"))
                break
            else:
                continue
            break


def _demote_equation_headings(work: list[TeiSection], index: PdfLineIndex, report: RepairReport) -> None:
    position = 1
    while position < len(work):
        section = work[position]
        heading = section.heading
        if (
            _is_body(section)
            and heading
            and "=" in heading
            and _EQUATION_NUMBER_END.search(heading)
            and index.is_printed(heading, _first_page(section))
            and _is_body(work[position - 1])
        ):
            previous = work[position - 1]
            previous.paragraphs.append(_plain(heading, _first_page(section), "formula"))
            previous.paragraphs.extend(section.paragraphs)
            del work[position]
            report.equations_demoted += 1
            continue
        position += 1


def _recover_titles_of_number_only_headings(work: list[TeiSection], index: PdfLineIndex, used: set, report: RepairReport) -> None:
    for section in work:
        if section.heading is not None or not section.number or not section.paragraphs:
            continue
        first = section.paragraphs[0]
        for candidate in index.near(_first_page(section)):
            if candidate in used or candidate.number != section.number:
                continue
            if first.text == candidate.title or first.text.startswith(candidate.title + " "):
                _, rest = cut_block(first, 0, len(candidate.title))
                section.paragraphs[0:1] = [rest] if rest else []
                section.heading, section.numbering = candidate.title, candidate.numbering
                used.add(candidate)
                report.titles_recovered += 1
                break


def _strip_trailing_number(section: TeiSection, number: str, report: RepairReport) -> None:
    if not section.paragraphs:
        return
    last = section.paragraphs[-1]
    if last.text == number:
        section.paragraphs.pop()
        report.number_fragments_removed += 1
    elif last.text.endswith(" " + number):
        before, _ = cut_block(last, len(last.text) - len(number), len(last.text))
        section.paragraphs[-1:] = [before] if before else []
        report.number_fragments_removed += 1


def _strip_trailing_number_before(work: list[TeiSection], position: int, number: str, report: RepairReport) -> None:
    """GROBID often leaves a heading's number at the end of the previous body paragraph."""
    if position > 0 and _is_body(work[position - 1]):
        _strip_trailing_number(work[position - 1], number, report)


def _recover_numbers(work: list[TeiSection], index: PdfLineIndex, used: set, report: RepairReport) -> None:
    for position, section in enumerate(work):
        if not _is_body(section) or not section.heading:
            continue
        for candidate in index.near(_first_page(section)):
            if candidate in used or candidate.title != section.heading:
                continue
            if section.number not in (None, candidate.number):
                continue
            if section.number is None:
                section.number, section.numbering = candidate.number, candidate.numbering
                report.numbers_recovered += 1
            used.add(candidate)
            _strip_trailing_number_before(work, position, candidate.number, report)
            break


def _recover_trapped_headings(work: list[TeiSection], index: PdfLineIndex, used: set, report: RepairReport) -> None:
    position = 0
    while position < len(work):
        section = work[position]
        if not _is_body(section):
            position += 1
            continue
        found = None  # (paragraph index, offset, heading)
        for p_index, block in enumerate(section.paragraphs):
            if block.kind != "text" or block.page_start is None:
                continue
            for candidate in index.headings:
                if candidate in used or not (block.page_start <= candidate.page <= (block.page_end or block.page_start)):
                    continue
                at = _find_phrase(block.text, candidate.full)
                if at >= 0 and (found is None or (p_index, at) < (found[0], found[1])):
                    found = (p_index, at, candidate)
            if found is not None:
                break
        if found is None:
            position += 1
            continue
        p_index, at, candidate = found
        before, after = cut_block(section.paragraphs[p_index], at, at + len(candidate.full))
        moved = ([after] if after else []) + section.paragraphs[p_index + 1:]
        section.paragraphs = section.paragraphs[:p_index] + ([before] if before else [])
        work.insert(position + 1, TeiSection(
            heading=candidate.title, number=candidate.number, level=1, section_type="other",
            paragraphs=moved, head_page=candidate.page, numbering=candidate.numbering,
        ))
        used.add(candidate)
        report.trapped_headings_recovered += 1
        # Re-scan the same section: an earlier heading may remain before the split point.


def _area(box: tuple[float, float, float, float]) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _inside_tables(boxes: Sequence[Box], regions: Sequence[Sequence[TableRegion]]) -> bool:
    total = covered = 0.0
    for page, x0, y0, x1, y1 in boxes:
        area = _area((x0, y0, x1, y1))
        total += area
        for rx0, ry0, rx1, ry1 in regions[page - 1] if 0 < page <= len(regions) else ():
            covered += _area((max(x0, rx0), max(y0, ry0), min(x1, rx1), min(y1, ry1)))
    return total > 0 and covered / total >= TABLE_OVERLAP


def _mark_table_text(work: list[TeiSection], regions: Sequence[Sequence[TableRegion]], report: RepairReport) -> None:
    if not any(regions):
        return
    for section in work:
        blocks: list[TeiParagraph] = []
        for block in section.paragraphs:
            if block.kind != "text":
                blocks.append(block)
                continue
            if not block.sentences:
                if block.boxes and _inside_tables(block.boxes, regions):
                    blocks.append(TeiParagraph(block.text, block.sentences, block.page_start, block.page_end, "table", block.boxes))
                    report.table_blocks += 1
                else:
                    blocks.append(block)
                continue
            runs: list[tuple[bool, list[TeiSentence]]] = []
            for sentence in block.sentences:
                is_table = bool(sentence.boxes) and _inside_tables(sentence.boxes, regions)
                if runs and runs[-1][0] == is_table:
                    runs[-1][1].append(sentence)
                else:
                    runs.append((is_table, [sentence]))
            if len(runs) == 1 and not runs[0][0]:
                blocks.append(block)
                continue
            for is_table, sentences in runs:
                piece = _block(" ".join(s.text for s in sentences), sentences, block, "table" if is_table else "text")
                if piece:
                    blocks.append(piece)
                    report.table_blocks += int(is_table)
        section.paragraphs = blocks


def _position(block: TeiParagraph) -> tuple[int, float] | None:
    boxes = block.boxes or tuple(b for s in block.sentences for b in s.boxes)
    if boxes:
        return boxes[0][0], boxes[0][2]
    if block.page_start is not None:
        return block.page_start, 0.0
    return None


def _place_figures(work: list[TeiSection], figures: Sequence[TeiFigure], report: RepairReport) -> None:
    """Insert each figure caption / GROBID table after the last block (in reading order) that
    starts at or before the figure's (page, y). Without coordinates: at the end of the body."""
    body = [s for s in work if _is_body(s)]
    if not body:
        return
    for figure in figures:
        kind = "table" if figure.kind == "table" else "caption"
        block = TeiParagraph(figure.text, (), figure.page_start, figure.page_end, kind, figure.boxes)
        if figure.boxes:
            where: tuple[int, float] | None = (figure.boxes[0][0], figure.boxes[0][2])
        else:
            where = (figure.page_start, 0.0) if figure.page_start is not None else None
        target_section, target_index = body[-1], len(body[-1].paragraphs)
        if where is not None:
            target_section, target_index = body[0], 0
            for section in body:
                for i, existing in enumerate(section.paragraphs):
                    pos = _position(existing)
                    if pos is not None and pos <= where:
                        target_section, target_index = section, i + 1
        target_section.paragraphs.insert(target_index, block)
        report.figures_placed += 1


def _mark_printed_captions(work: list[TeiSection], index: PdfLineIndex, report: RepairReport) -> None:
    for section in work:
        for i, block in enumerate(section.paragraphs):
            if block.kind == "text" and _CAPTION_START.match(block.text) and index.is_printed(block.text, block.page_start):
                section.paragraphs[i] = TeiParagraph(block.text, block.sentences, block.page_start, block.page_end, "caption", block.boxes)
                report.captions_marked += 1
