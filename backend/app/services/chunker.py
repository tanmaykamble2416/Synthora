"""Sections and retrieval chunks from extracted structure (Day 6). Pure functions.

Two inputs, one output shape:

  build_from_tei(sections, page_texts, config)   GROBID path: sections + chunks
  build_from_pages(page_texts, config)           fallback path: chunks only (no sections)

Chunking rules (sizes in WORDS, see TOKEN_COUNT_METHOD):
  * A chunk never crosses a section boundary (GROBID path).
  * Paragraphs are packed greedily up to `target_words`. A chunk still smaller than
    `min_words` may grow up to `max_words`; `max_words` is never exceeded.
  * A paragraph longer than `max_words` is split at sentence boundaries (GROBID <s>
    elements, otherwise a conservative regex). A single sentence longer than
    `max_words` is split at word boundaries (last resort).
  * A trailing chunk smaller than `min_words` is merged into the previous chunk of
    the same section when the result still fits in `max_words`.
  * No overlap between chunks.
  * Figures, tables, formulas, footnotes and the bibliography are never chunked.

Page spans are exact and always within 1..page_count:
  1. GROBID coordinates (validated by the TEI parser), else
  2. the page whose PyMuPDF text contains the paragraph's first/last words, else
  3. the page of the preceding paragraph (page 1 at the very start); counted in
     `unlocated_paragraphs` so the approximation is visible in logs.

Sizes are measured by a pluggable TokenCounter (token_counting.py). The Day 6 default is a
PROXY: whitespace-separated words, stored as token_count_method = 'word_proxy'. With a real
tokenizer the sizes are tokens and the label is 'tokenizer:<name>'. Day 7 uses the embedding
model's tokenizer and must not embed 'word_proxy' or 'unknown' chunks.

Day 6 quality pass:
  * Block kinds: "table" blocks are chunked on their own (content_type "table") and never packed
    together with prose; "text", "formula" and "caption" blocks are prose (content_type "text").
  * A section with a heading but no text of its own is kept (empty text) as part of the outline.
  * Sections carry their printed number (section_number).
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from app.services.pdf_text_cleaner import count_words
from app.services.tei_parser import TeiParagraph, TeiSection, normalize_text
from app.services.token_counting import WORD_PROXY, WORD_PROXY_METHOD, TokenCounter

TOKEN_COUNT_METHOD = WORD_PROXY_METHOD  # default label (word proxy)
CONTENT_TYPES = ("text", "table")

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[]?[A-Z0-9])")
_BLANK_LINE = re.compile(r"\n\s*\n")
_HAS_LETTER = re.compile(r"[^\W\d_]")
_WORD = re.compile(r"[^\W_]+")
_REFERENCES_HEADING = re.compile(
    r"^[ \t]*(?:(?:\d+|[IVX]+)\.?[ \t]+)?(references|bibliography|works cited|literature cited)[ \t]*:?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_LOCATE_PROBE_WORDS = 8
_LOCATE_MIN_WORDS = 4


def word_count_proxy(text: str) -> int:
    """Day 6 stand-in for a token count: number of whitespace-separated words."""
    return count_words(text)


@dataclass(frozen=True, slots=True)
class ChunkingConfig:
    """Sizes in the unit of `counter` (words for the default word proxy)."""

    target_words: int
    max_words: int
    min_words: int
    counter: TokenCounter = WORD_PROXY

    def __post_init__(self) -> None:
        if not (0 < self.min_words < self.target_words <= self.max_words):
            raise ValueError("chunk sizes must satisfy 0 < min < target <= max")


@dataclass(frozen=True, slots=True)
class SectionDraft:
    order_index: int
    level: int
    heading: str | None
    section_type: str
    text: str
    page_start: int
    page_end: int
    section_number: str | None = None


@dataclass(frozen=True, slots=True)
class ChunkDraft:
    chunk_index: int
    section_order_index: int | None
    text: str
    token_count: int
    token_count_method: str
    page_start: int
    page_end: int
    content_type: str = "text"  # CONTENT_TYPES


@dataclass(slots=True)
class StructureDrafts:
    sections: list[SectionDraft] = field(default_factory=list)
    chunks: list[ChunkDraft] = field(default_factory=list)
    unlocated_paragraphs: int = 0
    excluded_reference_pages: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class _Unit:
    text: str
    words: int  # size in counter units
    page_start: int
    page_end: int
    paragraph_key: int
    content_type: str = "text"


# --- page location for paragraphs without coordinates ------------------------------------


def _norm_words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


class _PageLocator:
    def __init__(self, page_texts: Sequence[str]) -> None:
        self._pages = [" " + " ".join(_norm_words(t)) + " " for t in page_texts]

    def find(self, words: list[str], start_page: int) -> int | None:
        """1-based page containing the word sequence, searching from start_page first."""
        if len(words) < _LOCATE_MIN_WORDS:
            return None
        probe = " " + " ".join(words) + " "
        order = list(range(start_page - 1, len(self._pages))) + list(range(0, start_page - 1))
        for index in order:
            if probe in self._pages[index]:
                return index + 1
        return None

    def locate(self, text: str, hint: int) -> tuple[int, int] | None:
        words = _norm_words(text)
        start = self.find(words[:_LOCATE_PROBE_WORDS], hint)
        if start is None:
            return None
        end = self.find(words[-_LOCATE_PROBE_WORDS:], start) if len(words) > _LOCATE_PROBE_WORDS else start
        return start, end if end is not None and end >= start else start


# --- splitting and packing -------------------------------------------------------------


def split_sentences(text: str) -> list[str]:
    return [s for s in (normalize_text(p) for p in _SENTENCE_BOUNDARY.split(text)) if s]


def _split_to_fit(text: str, config: ChunkingConfig) -> list[str]:
    """Last resort for one sentence larger than the maximum: split at word boundaries into
    pieces of at most max_words counter units each (a single over-long "word" stays whole)."""
    counter = config.counter
    pieces: list[str] = []
    current: list[str] = []
    for word in text.split():
        if current and counter.count(" ".join([*current, word])) > config.max_words:
            pieces.append(" ".join(current))
            current = []
        current.append(word)
    if current:
        pieces.append(" ".join(current))
    return pieces


def _units_for_paragraph(
    text: str,
    sentences: Iterable[tuple[str, int, int]],
    page_start: int,
    page_end: int,
    key: int,
    config: ChunkingConfig,
    content_type: str = "text",
) -> list[_Unit]:
    size = config.counter.count(text)
    if size == 0:
        return []
    if size <= config.max_words:
        return [_Unit(text, size, page_start, page_end, key, content_type)]
    units: list[_Unit] = []
    for sentence, s_start, s_end in sentences:
        for piece in _split_to_fit(sentence, config):
            units.append(_Unit(piece, config.counter.count(piece), s_start, s_end, key, content_type))
    return units


def _pack(units: Sequence[_Unit], config: ChunkingConfig) -> list[list[_Unit]]:
    groups: list[list[_Unit]] = []
    current: list[_Unit] = []
    size = 0
    for unit in units:
        fits_target = size + unit.words <= config.target_words
        may_grow = size < config.min_words and size + unit.words <= config.max_words
        other_kind = bool(current) and current[-1].content_type != unit.content_type
        if current and (other_kind or not (fits_target or may_grow)):
            groups.append(current)
            current, size = [], 0
        current.append(unit)
        size += unit.words
    if current:
        groups.append(current)
    # Merge a small trailing remainder into the previous chunk when it still fits.
    if len(groups) >= 2:
        last_words = sum(u.words for u in groups[-1])
        prev_words = sum(u.words for u in groups[-2])
        same_kind = groups[-1][0].content_type == groups[-2][0].content_type
        if same_kind and last_words < config.min_words and prev_words + last_words <= config.max_words:
            groups[-2].extend(groups.pop())
    return groups


def _join(group: Sequence[_Unit]) -> str:
    parts: list[str] = []
    previous_key: int | None = None
    for unit in group:
        if parts:
            parts.append(" " if unit.paragraph_key == previous_key else "\n\n")
        parts.append(unit.text)
        previous_key = unit.paragraph_key
    return "".join(parts)


def _fit(group: Sequence[_Unit], config: ChunkingConfig) -> list[Sequence[_Unit]]:
    """Guarantee the max for the JOINED text: a subword tokenizer can count a joined text
    differently from the sum of its parts. Split the group until every part fits."""
    if len(group) == 1 or config.counter.count(_join(group)) <= config.max_words:
        return [group]
    middle = len(group) // 2
    return _fit(group[:middle], config) + _fit(group[middle:], config)


def _chunks_from_groups(
    groups: Sequence[Sequence[_Unit]], section_order_index: int | None, first_index: int, config: ChunkingConfig
) -> list[ChunkDraft]:
    chunks: list[ChunkDraft] = []
    for whole in groups:
        for group in _fit(whole, config):
            text = _join(group)
            token_count = config.counter.count(text)
            if token_count == 0:
                continue
            chunks.append(
                ChunkDraft(
                    chunk_index=first_index + len(chunks),
                    section_order_index=section_order_index,
                    text=text,
                    token_count=token_count,
                    token_count_method=config.counter.method,
                    page_start=min(u.page_start for u in group),
                    page_end=max(u.page_end for u in group),
                    content_type=group[0].content_type,
                )
            )
    return chunks


# --- GROBID path -----------------------------------------------------------------------


def build_from_tei(
    sections: Sequence[TeiSection], page_texts: Sequence[str], config: ChunkingConfig
) -> StructureDrafts:
    page_count = len(page_texts)
    if page_count == 0:
        return StructureDrafts()
    locator = _PageLocator(page_texts)
    drafts = StructureDrafts()
    last_page = 1
    key = 0
    for section in sections:
        resolved: list[tuple[TeiParagraph, int, int]] = []
        for paragraph in section.paragraphs:
            if paragraph.page_start is not None and paragraph.page_end is not None:
                start, end = paragraph.page_start, paragraph.page_end
            else:
                located = locator.locate(paragraph.text, last_page)
                if located is None:
                    drafts.unlocated_paragraphs += 1
                    located = (last_page, last_page)
                start, end = located
            resolved.append((paragraph, start, end))
            last_page = end
        if not resolved and not (section.heading or section.number):
            continue

        order_index = len(drafts.sections)
        pages = [p for _, s, e in resolved for p in (s, e)]
        if section.head_page is not None and 1 <= section.head_page <= page_count:
            pages.append(section.head_page)
        if not pages:  # a heading-only section without coordinates: where reading has got to
            pages = [last_page]
        else:
            last_page = max(last_page, max(pages)) if not resolved else last_page
        drafts.sections.append(
            SectionDraft(
                order_index=order_index,
                level=section.level,
                heading=section.heading,
                section_type=section.section_type,
                text="\n\n".join(p.text for p, _, _ in resolved),
                page_start=min(pages),
                page_end=max(pages),
                section_number=section.number,
            )
        )

        units: list[_Unit] = []
        for paragraph, start, end in resolved:
            key += 1
            if paragraph.sentences:
                sentences = [
                    (s.text, s.page_start or start, s.page_end or s.page_start or end) for s in paragraph.sentences
                ]
            else:
                sentences = [(s, start, end) for s in split_sentences(paragraph.text)]
            content_type = "table" if paragraph.kind == "table" else "text"
            units += _units_for_paragraph(paragraph.text, sentences, start, end, key, config, content_type)
        drafts.chunks += _chunks_from_groups(_pack(units, config), order_index, len(drafts.chunks), config)

    _check_pages(drafts, page_count)
    return drafts


# --- fallback path ---------------------------------------------------------------------


def find_references_start(page_texts: Sequence[str]) -> tuple[int, int] | None:
    """(1-based page, character offset) of the LAST bibliography heading in the second
    half of the document, or None. Everything from there on is excluded from chunks."""
    page_count = len(page_texts)
    found: tuple[int, int] | None = None
    for index, text in enumerate(page_texts):
        page = index + 1
        if page_count > 2 and page <= page_count / 2:
            continue
        for match in _REFERENCES_HEADING.finditer(text):
            found = (page, match.start())
    return found


def build_from_pages(page_texts: Sequence[str], config: ChunkingConfig) -> StructureDrafts:
    page_count = len(page_texts)
    drafts = StructureDrafts()
    cut = find_references_start(page_texts)
    if cut is not None:
        drafts.excluded_reference_pages = tuple(range(cut[0], page_count + 1))
    units: list[_Unit] = []
    key = 0
    for index, text in enumerate(page_texts):
        page = index + 1
        if cut is not None:
            if page > cut[0]:
                break
            if page == cut[0]:
                text = text[: cut[1]]
        for raw in _BLANK_LINE.split(text):
            paragraph = normalize_text(raw)
            if not paragraph or not _HAS_LETTER.search(paragraph):
                continue  # page numbers and similar noise
            key += 1
            sentences = [(s, page, page) for s in split_sentences(paragraph)]
            units += _units_for_paragraph(paragraph, sentences, page, page, key, config)
    drafts.chunks = _chunks_from_groups(_pack(units, config), None, 0, config)
    _check_pages(drafts, page_count)
    return drafts


def _check_pages(drafts: StructureDrafts, page_count: int) -> None:
    for item in (*drafts.sections, *drafts.chunks):
        if not (1 <= item.page_start <= item.page_end <= page_count):
            raise ValueError(f"page span {item.page_start}-{item.page_end} outside 1..{page_count}")
