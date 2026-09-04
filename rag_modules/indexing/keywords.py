"""Deterministic, local keyword extraction for economy indexing."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
import re

import jieba

_CJK_CHARACTERS = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_TOKEN_PATTERN = re.compile(
    rf"(?P<cjk>[{_CJK_CHARACTERS}]+)"
    rf"|(?P<word>[^\W_{_CJK_CHARACTERS}]+(?:[._-][^\W_{_CJK_CHARACTERS}]+)*)",
    re.UNICODE,
)
_IDENTIFIER_PATTERN = re.compile(
    r"(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9][A-Za-z0-9._-]*\Z"
)

_ENGLISH_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "was",
        "were",
        "with",
    }
)
_CHINESE_STOPWORDS = frozenset(
    {
        "的",
        "了",
        "和",
        "是",
        "在",
        "有",
        "与",
        "及",
        "对",
        "为",
        "已",
        "将",
        "把",
        "被",
        "从",
        "到",
        "等",
        "属于",
        "需要",
    }
)
_STOPWORDS = _ENGLISH_STOPWORDS | _CHINESE_STOPWORDS
_IDENTIFIER_BONUS = 2


class KeywordExtractor:
    """Extract a bounded, repeatable list of local economy keywords.

    The input is scanned once and raw tokens are streamed: contiguous CJK
    spans are owned solely by :mod:`jieba`, while non-CJK Unicode word spans
    are owned solely by the regex.  This avoids duplicate counting at mixed
    boundaries.  The counter retains at most one entry per distinct accepted
    term, so its memory use is linear in the input's distinct token count.
    """

    def __init__(self) -> None:
        """Keep tokenizer state private instead of changing jieba globals."""
        self._tokenizer = jieba.Tokenizer()

    def extract(self, text: str, limit: int = 15) -> list[str]:
        """Return at most ``limit`` keywords, sorted by score then term."""
        self._validate_inputs(text, limit)
        if not text.strip():
            return []

        frequencies: Counter[str] = Counter()
        identifiers: set[str] = set()
        for raw_token in self._iter_raw_tokens(text):
            normalized, is_identifier = self._normalize(raw_token)
            if not normalized or normalized in _STOPWORDS:
                continue
            frequencies[normalized] += 1
            if is_identifier:
                identifiers.add(normalized)

        ranked_terms = sorted(
            frequencies,
            key=lambda term: (
                -(frequencies[term] + (_IDENTIFIER_BONUS if term in identifiers else 0)),
                term,
            ),
        )
        return ranked_terms[:limit]

    @staticmethod
    def _validate_inputs(text: str, limit: int) -> None:
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit must be an integer")
        if limit <= 0:
            raise ValueError("limit must be positive")

    def _iter_raw_tokens(self, text: str) -> Iterator[str]:
        for match in _TOKEN_PATTERN.finditer(text):
            cjk_span = match.group("cjk")
            if cjk_span is not None:
                yield from self._tokenizer.cut(cjk_span, HMM=False)
            else:
                yield match.group("word")

    @staticmethod
    def _normalize(token: str) -> tuple[str, bool]:
        token = token.strip()
        if not token:
            return "", False
        if _IDENTIFIER_PATTERN.fullmatch(token):
            return token.upper(), True
        return token.lower(), False
