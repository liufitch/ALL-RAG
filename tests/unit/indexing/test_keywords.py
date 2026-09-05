import logging

import pytest

from rag_modules.indexing import KeywordExtractor


def test_keywords_keep_chinese_terms_and_business_identifiers():
    """丢弃中日韩词项或标识符，会使经济模式的结果不可用。"""
    text = "订单 A001 已发货。订单 A001 属于华东客户，客户需要发票。"

    words = KeywordExtractor().extract(text, limit=5)

    assert words[:3] == ["A001", "客户", "订单"]
    assert len(words) <= 5


def test_mixed_token_pipeline_counts_identifier_once_per_occurrence():
    """两个分词器同时计数同一标识符，会使它错误地排在 beta 前面。"""
    words = KeywordExtractor().extract("订单A001 beta beta beta beta", limit=3)

    assert words == ["beta", "A001", "订单"]


@pytest.mark.parametrize("cjk_character", ["\U00020000", "\U0002f800"])
def test_astral_cjk_characters_do_not_merge_with_adjacent_identifiers(cjk_character):
    """未明确基本多文种平面外汉字的分词归属，会使 `A001` 变成无法检索的词项。"""
    words = KeywordExtractor().extract(
        f"{cjk_character}A001 beta beta beta beta",
        limit=3,
    )

    assert words == ["beta", "A001", cjk_character]


def test_first_han_extraction_does_not_emit_jieba_initializer_output(capsys, caplog):
    """库初始化时的日志噪声，不应从原本仅执行本地计算的操作中泄漏出来。"""
    caplog.set_level(logging.DEBUG, logger="jieba")

    assert KeywordExtractor().extract("订单") == ["订单"]

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert [record for record in caplog.records if record.name == "jieba"] == []


def test_private_tokenizer_initializes_and_tokenizes_representative_cjk():
    """不兼容的 jieba 升级必须明确报错，不能跳过中日韩词项提取。"""
    words = KeywordExtractor().extract("订单 客户", limit=2)

    assert words == ["客户", "订单"]


def test_keywords_normalize_case_and_keep_canonical_business_identifiers():
    """普通词大小写敏感或标识符规范化不稳定，会把同一概念拆成多个词项。"""
    words = KeywordExtractor().extract(
        "Graph graph GRAPH; a001 A001, b-02 B-02. SHIPPING shipping!",
        limit=4,
    )

    assert words == ["A001", "B-02", "graph", "shipping"]


def test_keywords_remove_explicit_bilingual_stopwords():
    """保留连接词会挤占应输出的索引概念。"""
    words = KeywordExtractor().extract("the graph graph and retrieval 的 图谱 图谱 和 检索")

    assert words == ["graph", "图谱", "retrieval", "检索"]
    assert "the" not in words
    assert "and" not in words
    assert "的" not in words
    assert "和" not in words


def test_keywords_break_equal_scores_by_normalized_term():
    """并列分数的处理顺序不确定，会导致重试时生成不同的关键词数组。"""
    extractor = KeywordExtractor()
    text = "zeta beta alpha"

    assert extractor.extract(text) == ["alpha", "beta", "zeta"]
    assert extractor.extract(text) == KeywordExtractor().extract(text)


def test_repeated_terms_score_above_single_terms():
    """忽略词频会丢失经济模式最主要的排序依据。"""
    words = KeywordExtractor().extract("invoice invoice graph retrieval")

    assert words == ["invoice", "graph", "retrieval"]


def test_blank_text_returns_no_keywords():
    """纯空白分段不得生成占位关键词。"""
    extractor = KeywordExtractor()

    assert extractor.extract("") == []
    assert extractor.extract(" \n\t ") == []


@pytest.mark.parametrize("text", [None, b"invoice", 42])
def test_extract_rejects_non_string_text(text):
    """非文本输入必须在公共入口报错，不能隐式进入分词流程。"""
    with pytest.raises(TypeError):
        KeywordExtractor().extract(text)


@pytest.mark.parametrize("limit", [True, False, 1.5, "3"])
def test_extract_rejects_non_integer_or_boolean_limit(limit):
    """布尔值和小数上限会使结果数量限制的契约含义不明确。"""
    with pytest.raises(TypeError):
        KeywordExtractor().extract("invoice", limit=limit)


@pytest.mark.parametrize("limit", [0, -1])
def test_extract_rejects_non_positive_limit(limit):
    """零或负数上限不是有意义的提取请求。"""
    with pytest.raises(ValueError):
        KeywordExtractor().extract("invoice", limit=limit)


def test_keywords_never_exceed_requested_limit():
    """忽略调用方设置的数量上限，可能使每个持久化的经济模式分段膨胀。"""
    words = KeywordExtractor().extract("delta gamma beta alpha", limit=2)

    assert words == ["alpha", "beta"]


@pytest.mark.parametrize("length", (256, 1_024))
def test_keywords_omit_overlong_tokens_without_truncation_or_collision(length):
    overlong = "X" * length

    words = KeywordExtractor().extract(f"{overlong} bounded bounded", limit=5)

    assert words == ["bounded"]
    assert all(len(word) <= 255 for word in words)
