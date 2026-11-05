"""Token counting: honest labels, local-only tokenizer, safe fallback to word_proxy."""

import logging
import sys

import pytest

from app.services import token_counting
from app.services.token_counting import WORD_PROXY, TokenizerCounter, WordProxyCounter, load_token_counter


@pytest.fixture(autouse=True)
def clear_cache():
    token_counting._load_tokenizer_counter.cache_clear()
    yield
    token_counting._load_tokenizer_counter.cache_clear()


def test_word_proxy_is_labelled_as_a_proxy():
    assert WORD_PROXY.method == "word_proxy"
    assert WordProxyCounter().count("one two\nthree") == 3


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
def test_empty_text_counts_zero(text):
    assert WORD_PROXY.count(text) == 0
    assert TokenizerCounter("x", lambda t: [1, 2, 3]).count(text) == 0


def test_tokenizer_counter_counts_the_encoded_ids_and_says_so():
    counter = TokenizerCounter(name="BAAI/bge-small-en-v1.5", encode=lambda t: list(range(len(t.split()) * 2)))
    assert counter.method == "tokenizer:BAAI/bge-small-en-v1.5"
    assert counter.count("one two three") == 6


def test_long_text_is_counted_fully():
    text = "word " * 50_000
    assert WORD_PROXY.count(text) == 50_000


def test_word_proxy_mode_needs_nothing():
    assert load_token_counter("word_proxy", None, "x") is WORD_PROXY


def test_tokenizer_mode_without_file_falls_back_to_word_proxy(caplog, tmp_path):
    with caplog.at_level(logging.WARNING, logger="app.services.token_counting"):
        assert load_token_counter("tokenizer", None, "x") is WORD_PROXY
        assert load_token_counter("tokenizer", tmp_path / "missing.json", "y") is WORD_PROXY
    assert "using word_proxy" in caplog.text


def test_tokenizer_mode_without_the_package_falls_back(monkeypatch, tmp_path):
    path = tmp_path / "tokenizer.json"
    path.write_text("{}")
    monkeypatch.setitem(sys.modules, "tokenizers", None)  # import fails as if not installed
    assert load_token_counter("tokenizer", path, "x") is WORD_PROXY


def test_corrupt_tokenizer_file_falls_back(tmp_path):
    pytest.importorskip("tokenizers")
    path = tmp_path / "tokenizer.json"
    path.write_text("not json")
    assert load_token_counter("tokenizer", path, "x") is WORD_PROXY


def test_real_tokenizer_file_is_used_when_available(tmp_path):
    tokenizers = pytest.importorskip("tokenizers")
    from tokenizers import models, pre_tokenizers

    vocab = {"[UNK]": 0, "re": 1, "##trieval": 2, "works": 3, "well": 4}
    tok = tokenizers.Tokenizer(models.WordPiece(vocab=vocab, unk_token="[UNK]"))
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    path = tmp_path / "tokenizer.json"
    tok.save(str(path))
    counter = load_token_counter("tokenizer", path, "test/wordpiece")
    assert counter.method == "tokenizer:test/wordpiece"
    assert counter.count("retrieval works well") == 4  # re ##trieval works well
    assert load_token_counter("tokenizer", path, "test/wordpiece") is counter  # loaded once, reused
