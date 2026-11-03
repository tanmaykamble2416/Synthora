"""Conservative normalisation of text extracted from one PDF page.

The goal is to remove encoding noise, NOT to edit the paper. Every rule below only
touches characters that carry no meaning in the source:

  * NUL and other control characters (except newline and tab) are removed. MuPDF emits
    U+0000 for glyphs a font cannot map to Unicode.
  * Invisible formatting characters are removed: soft hyphen U+00AD, zero-width
    space U+200B and BOM / zero-width no-break space U+FEFF.
  * CRLF / CR become LF.
  * Runs of spaces and tabs become one space; trailing spaces on a line are removed.
  * Three or more consecutive newlines become two, so paragraph breaks survive.
  * Unicode is normalised to NFC (canonical composition: "e" + combining accent -> "é").

Deliberately NOT done, because each would change research content:
  * NFKC/NFKD (would turn "x²" into "x2", "ℝ" into "R" and "½" into "1/2").
  * De-hyphenation across line breaks ("state-of-the-\\nart" is ambiguous).
  * Joining lines, reflowing paragraphs, spell correction or any rewriting.
"""

import re
import unicodedata

_INVISIBLE = dict.fromkeys(map(ord, "­​﻿"))
# C0 controls except \t (0x09) and \n (0x0A), plus DEL and the C1 range.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_HORIZONTAL_WS = re.compile(r"[ \t ]+")  # includes no-break space
_SPACE_AROUND_NEWLINE = re.compile(r" *\n *")
_EXCESS_NEWLINES = re.compile(r"\n{3,}")


def clean_page_text(raw: str) -> str:
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = text.translate(_INVISIBLE)
    text = _CONTROL.sub("", text)
    text = unicodedata.normalize("NFC", text)
    text = _HORIZONTAL_WS.sub(" ", text)
    text = _SPACE_AROUND_NEWLINE.sub("\n", text)
    text = _EXCESS_NEWLINES.sub("\n\n", text)
    return text.strip()


def count_words(text: str) -> int:
    return len(text.split())


def count_significant_chars(text: str) -> int:
    """Non-whitespace characters: used to decide whether a page has real text."""
    return sum(1 for ch in text if not ch.isspace())
