"""Chunker: size limits, section boundaries, page spans and the fallback path."""

from pathlib import Path

import pytest

from app.services.chunker import (
    TOKEN_COUNT_METHOD,
    ChunkingConfig,
    build_from_pages,
    build_from_tei,
    find_references_start,
    split_sentences,
    word_count_proxy,
)
from app.services.tei_parser import TeiParagraph, TeiSection, TeiSentence, parse_tei

CONFIG = ChunkingConfig(target_words=350, max_words=500, min_words=60)
SMALL = ChunkingConfig(target_words=20, max_words=30, min_words=8)
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "tei" / "full_paper.tei.xml"


def words(n: int, prefix: str = "w") -> str:
    return " ".join(f"{prefix}{i}" for i in range(n))


def sentence_text(n_words: int, label: str) -> str:
    return " ".join([label.capitalize()] + [f"{label}{i}" for i in range(n_words - 1)]) + "."


def para(text: str, start: int | None = 1, end: int | None = 1, sentences=()) -> TeiParagraph:
    return TeiParagraph(text=text, sentences=tuple(sentences), page_start=start, page_end=end)


def section(*paragraphs: TeiParagraph, heading="H", kind="other", level=1) -> TeiSection:
    return TeiSection(heading=heading, number=None, level=level, section_type=kind, paragraphs=list(paragraphs))


def test_word_count_proxy_counts_whitespace_separated_words():
    assert word_count_proxy("  one two\nthree\tfour  ") == 4
    assert word_count_proxy("") == 0
    assert TOKEN_COUNT_METHOD == "word_proxy"


@pytest.mark.parametrize(("minimum", "target", "maximum"), [(0, 10, 20), (10, 10, 20), (5, 30, 20)])
def test_invalid_config_is_rejected(minimum, target, maximum):
    with pytest.raises(ValueError):
        ChunkingConfig(target_words=target, max_words=maximum, min_words=minimum)


def test_chunks_respect_max_words_and_carry_word_proxy_counts():
    paragraphs = [para(words(120, f"p{i}_")) for i in range(10)]
    drafts = build_from_tei([section(*paragraphs)], ["page"], CONFIG)
    assert drafts.chunks
    for chunk in drafts.chunks:
        assert chunk.token_count == word_count_proxy(chunk.text)
        assert chunk.token_count <= CONFIG.max_words
        assert chunk.token_count_method == "word_proxy"
    # Whole 120-word paragraphs: 240 + 120 would pass the 350 target, so each chunk holds two.
    assert [c.token_count for c in drafts.chunks] == [240] * 5


def test_no_overlap_and_no_text_lost():
    paragraphs = [para(words(90, f"p{i}_")) for i in range(7)]
    drafts = build_from_tei([section(*paragraphs)], ["page"], CONFIG)
    chunk_words = [w for c in drafts.chunks for w in c.text.split()]
    source_words = [w for p in paragraphs for w in p.text.split()]
    assert chunk_words == source_words  # same words, same order, nothing duplicated


def test_chunks_never_cross_sections():
    first = section(para(words(40, "a")), heading="Intro", kind="introduction")
    second = section(para(words(40, "b")), heading="Method", kind="methodology")
    drafts = build_from_tei([first, second], ["page"], CONFIG)
    assert [c.section_order_index for c in drafts.chunks] == [0, 1]
    assert all(w.startswith("a") for w in drafts.chunks[0].text.split())
    assert all(w.startswith("b") for w in drafts.chunks[1].text.split())


def test_small_trailing_remainder_is_merged():
    paragraphs = [para(words(300, "a")), para(words(30, "b"))]
    drafts = build_from_tei([section(*paragraphs)], ["page"], CONFIG)
    assert [c.token_count for c in drafts.chunks] == [330]


def test_small_remainder_is_kept_when_merging_would_exceed_max():
    paragraphs = [para(words(480, "a")), para(words(30, "b"))]
    drafts = build_from_tei([section(*paragraphs)], ["page"], CONFIG)
    assert [c.token_count for c in drafts.chunks] == [480, 30]


def test_chunk_below_minimum_may_grow_up_to_max():
    paragraphs = [para(words(50, "a")), para(words(400, "b"))]
    drafts = build_from_tei([section(*paragraphs)], ["page"], CONFIG)
    assert [c.token_count for c in drafts.chunks] == [450]


def test_chunk_below_minimum_never_grows_past_max():
    paragraphs = [para(words(50, "a")), para(words(480, "b"))]
    drafts = build_from_tei([section(*paragraphs)], ["page"], CONFIG)
    assert [c.token_count for c in drafts.chunks] == [50, 480]


def test_packing_respects_max_before_the_refit_safety_net():
    # The packer itself must stop at the maximum. If it let [50 + 480] grow and relied on the
    # later refit to split it, the 15-word remainder could no longer merge into the 480 chunk.
    paragraphs = [para(words(50, "a")), para(words(480, "b")), para(words(15, "c"))]
    drafts = build_from_tei([section(*paragraphs)], ["page"], CONFIG)
    assert [c.token_count for c in drafts.chunks] == [50, 495]


def test_long_paragraph_is_split_at_grobid_sentences():
    sentences = [TeiSentence(sentence_text(100, f"s{i}x"), 1, 1) for i in range(8)]
    text = " ".join(s.text for s in sentences)
    drafts = build_from_tei([section(para(text, sentences=sentences))], ["page"], CONFIG)
    assert [c.token_count for c in drafts.chunks] == [300, 300, 200]
    for chunk in drafts.chunks:
        assert chunk.text.endswith(".")  # every chunk ends at a sentence boundary


def test_long_paragraph_without_sentences_uses_regex_split():
    text = " ".join(sentence_text(100, f"r{i}x") for i in range(6))
    drafts = build_from_tei([section(para(text))], ["page"], CONFIG)
    assert [c.token_count for c in drafts.chunks] == [300, 300]


def test_single_overlong_sentence_is_split_at_words():
    text = words(1200, "x")  # no sentence boundary at all
    drafts = build_from_tei([section(para(text))], ["page"], CONFIG)
    assert all(c.token_count <= CONFIG.max_words for c in drafts.chunks)
    assert sum(c.token_count for c in drafts.chunks) == 1200


def test_sentence_pages_define_chunk_pages():
    sentences = [TeiSentence(sentence_text(10, f"q{i}x"), 1 + i // 3, 1 + i // 3) for i in range(9)]
    text = " ".join(s.text for s in sentences)
    drafts = build_from_tei([section(para(text, 1, 3, sentences))], ["p1", "p2", "p3"], SMALL)
    spans = [(c.page_start, c.page_end) for c in drafts.chunks]
    assert spans[0] == (1, 1)
    assert spans[-1][1] == 3
    assert all(1 <= s <= e <= 3 for s, e in spans)


def test_paragraph_without_coordinates_is_located_by_text():
    pages = ["Unrelated first page text here.", "The quick brown fox jumps over the lazy dog near the river bank."]
    paragraph = para("The quick brown fox jumps over the lazy dog near the river bank.", None, None)
    drafts = build_from_tei([section(paragraph)], pages, CONFIG)
    assert (drafts.chunks[0].page_start, drafts.chunks[0].page_end) == (2, 2)
    assert drafts.sections[0].page_start == 2
    assert drafts.unlocated_paragraphs == 0


def test_unlocatable_paragraph_inherits_previous_page_and_is_counted():
    located = para(words(70, "a"), 2, 2)
    unknown = para("Text that appears on no page at all in the extracted layer.", None, None)
    drafts = build_from_tei([section(located, unknown)], ["x", "y", "z"], CONFIG)
    assert drafts.unlocated_paragraphs == 1
    assert (drafts.chunks[-1].page_start, drafts.chunks[-1].page_end) == (2, 2)


def test_sections_are_ordered_with_exact_page_ranges():
    document = parse_tei(FIXTURE.read_bytes(), page_count=5)
    drafts = build_from_tei(document.sections, ["p"] * 5, CONFIG)
    assert [s.order_index for s in drafts.sections] == list(range(len(drafts.sections)))
    method = next(s for s in drafts.sections if s.heading == "Proposed Method")
    assert (method.page_start, method.page_end) == (2, 3)
    assert [c.chunk_index for c in drafts.chunks] == list(range(len(drafts.chunks)))
    texts = " ".join(c.text for c in drafts.chunks)
    # build_from_tei only chunks section blocks; figures are placed by structure_repair.
    assert "FIGURE CAPTION TEXT" not in texts and "Attention is all you need" not in texts


def test_no_pages_means_no_structure():
    drafts = build_from_tei([section(para("text"))], [], CONFIG)
    assert drafts.sections == [] and drafts.chunks == []


# --- fallback --------------------------------------------------------------------------


def test_fallback_chunks_have_no_section_and_exact_pages():
    pages = [words(200, "a"), words(200, "b"), words(200, "c")]
    drafts = build_from_pages(pages, CONFIG)
    assert drafts.sections == []
    assert all(c.section_order_index is None for c in drafts.chunks)
    # One 200-word paragraph per page; 200 + 200 would pass the 350 target.
    assert [(c.page_start, c.page_end) for c in drafts.chunks] == [(1, 1), (2, 2), (3, 3)]
    for chunk in drafts.chunks:
        assert 1 <= chunk.page_start <= chunk.page_end <= 3
        assert chunk.token_count <= CONFIG.max_words
    assert sum(c.token_count for c in drafts.chunks) == 600


def test_fallback_skips_empty_pages_and_page_numbers():
    pages = ["Some real text on the first page of the paper.\n\n1", "", "  \n ", "More real text.\n\n4"]
    drafts = build_from_pages(pages, CONFIG)
    assert len(drafts.chunks) == 1
    text = drafts.chunks[0].text
    assert "\n\n1" not in text and not text.endswith("4")
    assert (drafts.chunks[0].page_start, drafts.chunks[0].page_end) == (1, 4)


def test_fallback_excludes_bibliography():
    pages = [words(100, "intro"), words(100, "body"), "Conclusion text here.\n\nReferences\n[1] A. Author. A title. 2020.", "[2] B. Author. Another title. 2021."]
    drafts = build_from_pages(pages, CONFIG)
    text = " ".join(c.text for c in drafts.chunks)
    assert "Conclusion text here." in text
    assert "Author" not in text
    assert drafts.excluded_reference_pages == (3, 4)


@pytest.mark.parametrize(
    "heading", ["References", "REFERENCES", "7 References", "VII. REFERENCES", "Bibliography", "References:"]
)
def test_references_heading_variants(heading):
    pages = ["a", "b", "c", f"text\n{heading}\n[1] x"]
    assert find_references_start(pages) is not None


def test_references_mentioned_in_the_first_half_are_ignored():
    pages = ["References\nthis is a table of contents entry", "b", "c", "d"]
    assert find_references_start(pages) is None


def test_inline_mention_is_not_a_heading():
    assert find_references_start(["a", "see the references below for details"]) is None


def test_split_sentences():
    assert split_sentences("First one. Second one! Third? fourth stays.") == [
        "First one.", "Second one!", "Third? fourth stays.",
    ]
    assert split_sentences("e.g. this is not split. But this is.") == ["e.g. this is not split.", "But this is."]


# --- Day 6 quality pass: block kinds, content types, outline, pluggable counter -------------

from app.services.token_counting import TokenizerCounter  # noqa: E402


def block(text: str, kind: str = "text", page: int = 1) -> TeiParagraph:
    return TeiParagraph(text=text, sentences=(), page_start=page, page_end=page, kind=kind)


def test_table_blocks_are_never_packed_with_prose():
    sec = section(block(words(40, "a")), block(words(30, "cell"), "table"), block(words(40, "b")), heading="II.")
    drafts = build_from_tei([sec], ["page"], CONFIG)
    assert [(c.content_type, c.text.split()[0]) for c in drafts.chunks] == [("text", "a0"), ("table", "cell0"), ("text", "b0")]


def test_small_table_remainder_is_not_merged_into_prose():
    sec = section(block(words(300, "a")), block(words(20, "cell"), "table"))
    drafts = build_from_tei([sec], ["page"], CONFIG)
    assert [c.content_type for c in drafts.chunks] == ["text", "table"]


def test_long_table_is_split_into_table_chunks_within_the_maximum():
    rows = " ".join(sentence_text(60, f"row{i}x") for i in range(12))  # 720 words of table cells
    drafts = build_from_tei([section(block(rows, "table"))], ["page"], CONFIG)
    assert {c.content_type for c in drafts.chunks} == {"table"}
    assert all(c.token_count <= CONFIG.max_words for c in drafts.chunks)
    assert sum(c.token_count for c in drafts.chunks) == 720


def test_formulas_and_captions_are_prose_and_kept_in_order():
    sec = section(block("We define it as"), block("DTI = EMI / Income (1)", "formula"), block("Fig. 2. Pipeline", "caption"))
    drafts = build_from_tei([sec], ["page"], CONFIG)
    assert [(c.content_type, c.text) for c in drafts.chunks] == [
        ("text", "We define it as\n\nDTI = EMI / Income (1)\n\nFig. 2. Pipeline"),
    ]


def test_section_number_and_heading_only_sections_are_kept():
    parent = TeiSection(heading="MOTIVATION", number="III.", level=1, section_type="other", paragraphs=[], head_page=4)
    child = TeiSection(heading="Real-World Problem", number="A.", level=2, section_type="other",
                       paragraphs=[block(words(70, "m"), page=4)], head_page=4)
    drafts = build_from_tei([parent, child], ["p"] * 5, CONFIG)
    assert [(s.section_number, s.heading, s.text, s.page_start) for s in drafts.sections] == [
        ("III.", "MOTIVATION", "", 4), ("A.", "Real-World Problem", words(70, "m"), 4),
    ]
    assert [c.section_order_index for c in drafts.chunks] == [1]  # no chunk for the heading-only section


def test_no_text_lost_or_duplicated_across_kinds():
    blocks = [block(words(120, f"t{i}_"), "table" if i % 3 == 1 else "text") for i in range(9)]
    drafts = build_from_tei([section(*blocks)], ["page"], CONFIG)
    assert [w for c in drafts.chunks for w in c.text.split()] == [w for b in blocks for w in b.text.split()]


class CharPairTokenizer:
    """Deterministic stand-in for a subword tokenizer: one token per 2 characters of each word."""

    def __call__(self, text: str) -> list[int]:
        return [0] * sum((len(w) + 1) // 2 for w in text.split())


def test_pluggable_counter_sizes_and_labels_chunks():
    counter = TokenizerCounter(name="test/char-pairs", encode=CharPairTokenizer())
    config = ChunkingConfig(target_words=100, max_words=150, min_words=10, counter=counter)
    text = " ".join(sentence_text(20, f"w{i}x") for i in range(20))  # 400 words, ~3 "tokens" per word
    drafts = build_from_tei([section(block(text))], ["page"], config)
    assert {c.token_count_method for c in drafts.chunks} == {"tokenizer:test/char-pairs"}
    for chunk in drafts.chunks:
        assert chunk.token_count == counter.count(chunk.text) <= 150
    assert [w for c in drafts.chunks for w in c.text.split()] == text.split()


def test_default_counter_still_labels_word_proxy():
    drafts = build_from_tei([section(block(words(10)))], ["page"], CONFIG)
    assert drafts.chunks[0].token_count_method == "word_proxy"


class CharCounter:
    """Counts the JOINED text (separators included): ceil(characters / 4). Joining blocks with
    blank lines therefore costs extra "tokens", as it can with a real subword tokenizer."""

    method = "tokenizer:test/chars"

    def count(self, text: str) -> int:
        return -(-len(text) // 4) if text.strip() else 0


def test_joined_text_is_refitted_when_separators_push_it_over_the_maximum():
    config = ChunkingConfig(target_words=100, max_words=100, min_words=10, counter=CharCounter())
    blocks = [block("x" * 99) for _ in range(4)]  # 25 "tokens" each: 4 blocks sum to exactly 100
    drafts = build_from_tei([section(*blocks)], ["page"], config)
    assert all(c.token_count <= 100 for c in drafts.chunks)  # joined: 4*99 + 3*2 = 402 chars = 101
    assert len(drafts.chunks) == 2
    assert "".join(c.text.replace("\n\n", "") for c in drafts.chunks) == "x" * 396
