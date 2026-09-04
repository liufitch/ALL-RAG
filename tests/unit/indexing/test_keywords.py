import logging

import pytest

from rag_modules.indexing import KeywordExtractor


def test_keywords_keep_chinese_terms_and_business_identifiers():
    """Dropping CJK terms or identifiers would make economy results unusable."""
    text = "订单 A001 已发货。订单 A001 属于华东客户，客户需要发票。"

    words = KeywordExtractor().extract(text, limit=5)

    assert words[:3] == ["A001", "客户", "订单"]
    assert len(words) <= 5


def test_mixed_token_pipeline_counts_identifier_once_per_occurrence():
    """Counting an identifier via both tokenizers would incorrectly outrank beta."""
    words = KeywordExtractor().extract("订单A001 beta beta beta beta", limit=3)

    assert words == ["beta", "A001", "订单"]


@pytest.mark.parametrize("cjk_character", ["\U00020000", "\U0002f800"])
def test_astral_cjk_characters_do_not_merge_with_adjacent_identifiers(cjk_character):
    """Missing astral Han ownership would turn `A001` into an unsearchable word."""
    words = KeywordExtractor().extract(
        f"{cjk_character}A001 beta beta beta beta",
        limit=3,
    )

    assert words == ["beta", "A001", cjk_character]


def test_first_han_extraction_does_not_emit_jieba_initializer_output(capsys, caplog):
    """Library initialization chatter would leak from an otherwise local operation."""
    caplog.set_level(logging.DEBUG, logger="jieba")

    assert KeywordExtractor().extract("订单") == ["订单"]

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert [record for record in caplog.records if record.name == "jieba"] == []


def test_private_tokenizer_initializes_and_tokenizes_representative_cjk():
    """An incompatible jieba upgrade must fail visibly, not skip CJK extraction."""
    words = KeywordExtractor().extract("订单 客户", limit=2)

    assert words == ["客户", "订单"]


def test_keywords_normalize_case_and_keep_canonical_business_identifiers():
    """Case-sensitive ordinary words and unstable identifiers split one concept."""
    words = KeywordExtractor().extract(
        "Graph graph GRAPH; a001 A001, b-02 B-02. SHIPPING shipping!",
        limit=4,
    )

    assert words == ["A001", "B-02", "graph", "shipping"]


def test_keywords_remove_explicit_bilingual_stopwords():
    """Leaving connective words in output would crowd out indexed concepts."""
    words = KeywordExtractor().extract("the graph graph and retrieval 的 图谱 图谱 和 检索")

    assert words == ["graph", "图谱", "retrieval", "检索"]
    assert "the" not in words
    assert "and" not in words
    assert "的" not in words
    assert "和" not in words


def test_keywords_break_equal_scores_by_normalized_term():
    """Unordered tie resolution would make retries produce different keyword arrays."""
    extractor = KeywordExtractor()
    text = "zeta beta alpha"

    assert extractor.extract(text) == ["alpha", "beta", "zeta"]
    assert extractor.extract(text) == KeywordExtractor().extract(text)


def test_repeated_terms_score_above_single_terms():
    """Ignoring frequency would lose the primary economy ranking signal."""
    words = KeywordExtractor().extract("invoice invoice graph retrieval")

    assert words == ["invoice", "graph", "retrieval"]


def test_blank_text_returns_no_keywords():
    """Whitespace-only segments must not create placeholder keywords."""
    extractor = KeywordExtractor()

    assert extractor.extract("") == []
    assert extractor.extract(" \n\t ") == []


@pytest.mark.parametrize("text", [None, b"invoice", 42])
def test_extract_rejects_non_string_text(text):
    """Non-text input must fail at the public boundary rather than tokenize implicitly."""
    with pytest.raises(TypeError):
        KeywordExtractor().extract(text)


@pytest.mark.parametrize("limit", [True, False, 1.5, "3"])
def test_extract_rejects_non_integer_or_boolean_limit(limit):
    """Boolean and fractional limits make a bounded result contract ambiguous."""
    with pytest.raises(TypeError):
        KeywordExtractor().extract("invoice", limit=limit)


@pytest.mark.parametrize("limit", [0, -1])
def test_extract_rejects_non_positive_limit(limit):
    """A zero or negative bound is not a meaningful extraction request."""
    with pytest.raises(ValueError):
        KeywordExtractor().extract("invoice", limit=limit)


def test_keywords_never_exceed_requested_limit():
    """Ignoring the caller's cap can bloat every persisted economy segment."""
    words = KeywordExtractor().extract("delta gamma beta alpha", limit=2)

    assert words == ["alpha", "beta"]
