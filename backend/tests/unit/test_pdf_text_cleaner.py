import pytest

from app.services.pdf_text_cleaner import clean_page_text, count_significant_chars, count_words


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a\x00b", "ab"),                                  # NUL (MuPDF's unmappable glyph)
        ("line1\r\nline2\rline3", "line1\nline2\nline3"),  # line endings
        ("bell\x07 and\x1b esc\x7f del", "bell and esc del"),  # control characters
        ("tab\tseparated", "tab separated"),
        ("many     spaces", "many spaces"),
        ("trailing   \nspaces", "trailing\nspaces"),
        ("  leading\n   indent", "leading\nindent"),
        ("p1\n\n\n\n\np2", "p1\n\np2"),                    # excessive blank lines
        ("soft­hyphen zero​width ﻿bom", "softhyphen zerowidth bom"),
        ("no break", "no break"),
        ("é", "é"),                                  # NFC composition
        ("   \n\n  ", ""),
    ],
)
def test_cleaning_rules(raw: str, expected: str) -> None:
    assert clean_page_text(raw) == expected


def test_paragraph_boundaries_and_headings_are_preserved() -> None:
    raw = "1 Introduction\nDeep learning has...\n\n2 Related Work\nPrior work [3, 7] shows..."
    assert clean_page_text(raw) == raw


@pytest.mark.parametrize(
    "content",
    [
        "Accuracy improved by 12.5% (p < 0.05) over the baseline [12].",
        "∑ᵢ xᵢ² ≤ ∫₀¹ f(x) dx, x ∈ ℝⁿ, α = 0.01, β₁ = 0.9",
        "Lemma 3.1. For all ε > 0 there exists δ > 0 such that |f(x) − f(y)| < ε.",
        "The F1-score was 0.93 ± 0.02; see Table 2 and Eq. (4).",
        "中文摘要 · Résumé en français · Zusammenfassung über Größe",
        "x² ½ ﬁ ℝ ①",  # would be changed by NFKC, must NOT be changed here
        "state-of-the-\nart",  # no de-hyphenation across lines
    ],
)
def test_research_content_is_not_rewritten(content: str) -> None:
    assert clean_page_text(content) == content


def test_counts() -> None:
    text = clean_page_text("Hello  world\n\nsecond   paragraph")
    assert count_words(text) == 4
    assert count_significant_chars(text) == len("Helloworldsecondparagraph")
    assert count_words("") == 0 and count_significant_chars(" \n ") == 0
