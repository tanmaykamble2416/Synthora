"""How chunk sizes are measured (Day 6 quality pass). Pluggable; word_proxy by default.

A TokenCounter has a `method` label, stored in paper_chunks.token_count_method, and a
`count(text)` function, stored in paper_chunks.token_count. The label always says what was
actually counted, so an approximation is never presented as a tokenizer count:

  word_proxy         whitespace-separated words (Day 6 default; NOT model tokens)
  tokenizer:<name>   tokens of a real tokenizer, without special tokens such as [CLS]/[SEP]

Extraction must never depend on downloading a model. CHUNK_TOKEN_COUNTER=tokenizer therefore
only uses a tokenizer.json that already exists on disk (CHUNK_TOKENIZER_FILE), via the optional
`tokenizers` package. If either is missing, or the file cannot be loaded, extraction logs a
warning and falls back to word_proxy, and the chunks are labelled word_proxy.

Day 7 plugs in the embedding model's tokenizer (BAAI/bge-small-en-v1.5) and enforces its
512-token input limit.
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from app.services.pdf_text_cleaner import count_words

logger = logging.getLogger(__name__)

WORD_PROXY_METHOD = "word_proxy"
TOKENIZER_METHOD_PREFIX = "tokenizer:"


class TokenCounter(Protocol):
    @property
    def method(self) -> str: ...

    def count(self, text: str) -> int: ...


@dataclass(frozen=True, slots=True)
class WordProxyCounter:
    method: str = WORD_PROXY_METHOD

    def count(self, text: str) -> int:
        return count_words(text)


WORD_PROXY = WordProxyCounter()


@dataclass(frozen=True, slots=True)
class TokenizerCounter:
    """Counts with a real tokenizer. `encode(text)` returns the token ids WITHOUT special tokens."""

    name: str
    encode: Callable[[str], Sequence[int]]

    @property
    def method(self) -> str:
        return f"{TOKENIZER_METHOD_PREFIX}{self.name}"

    def count(self, text: str) -> int:
        if not text or not text.strip():
            return 0
        return len(self.encode(text))


def load_token_counter(mode: str, tokenizer_file: Path | None, tokenizer_name: str) -> TokenCounter:
    """The configured counter, or WORD_PROXY when a tokenizer is not available locally."""
    if mode == WORD_PROXY_METHOD:
        return WORD_PROXY
    return _load_tokenizer_counter(str(tokenizer_file) if tokenizer_file else None, tokenizer_name)


@lru_cache(maxsize=4)
def _load_tokenizer_counter(tokenizer_file: str | None, tokenizer_name: str) -> TokenCounter:
    # Loaded once per process and reused (lru_cache), never per chunk or per request.
    if not tokenizer_file or not Path(tokenizer_file).is_file():
        logger.warning("CHUNK_TOKEN_COUNTER=tokenizer but CHUNK_TOKENIZER_FILE is not an existing file; using word_proxy")
        return WORD_PROXY
    try:
        from tokenizers import Tokenizer  # optional dependency (Day 7 adds it)
    except ImportError:
        logger.warning("CHUNK_TOKEN_COUNTER=tokenizer but the 'tokenizers' package is not installed; using word_proxy")
        return WORD_PROXY
    try:
        tokenizer = Tokenizer.from_file(tokenizer_file)
    except Exception as exc:  # a corrupt or incompatible file must not break extraction
        logger.warning("Could not load the tokenizer file (%s); using word_proxy", type(exc).__name__)
        return WORD_PROXY
    return TokenizerCounter(name=tokenizer_name, encode=lambda text: tokenizer.encode(text, add_special_tokens=False).ids)
