"""Deterministic, local keyword extraction for economy indexing."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from hashlib import md5
import marshal
import os
import re
import tempfile
import threading

import jieba

_CJK_CHARACTERS = (
    r"\u3400-\u4dbf"  # Extension A
    r"\u4e00-\u9fff"  # Unified Ideographs
    r"\uf900-\ufaff"  # Compatibility Ideographs
    r"\U00020000-\U0002a6df"  # Extension B
    r"\U0002a700-\U0002b73f"  # Extension C
    r"\U0002b740-\U0002b81f"  # Extension D
    r"\U0002b820-\U0002ceaf"  # Extension E
    r"\U0002ceb0-\U0002ebef"  # Extension F
    r"\U0002ebf0-\U0002ee5f"  # Extension I
    r"\U0002f800-\U0002fa1f"  # Compatibility Ideographs Supplement
    r"\U00030000-\U0003134f"  # Extension G
    r"\U00031350-\U000323af"  # Extension H
    r"\U000323b0-\U0003347f"  # Extension J
)
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
_JIEBA_INITIALIZATION_ATTRIBUTES = frozenset(
    {"DEFAULT_DICT", "DICT_WRITING", "_get_abs_path"}
)


class _QuietTokenizer(jieba.Tokenizer):
    """Private jieba tokenizer that preserves initialization without its logs.

    The supported jieba API exposes no instance logger. Its inherited initializer
    writes four records through the module-global logger, so this private override
    retains its cache and lock behavior while omitting only those calls. It never
    changes the shared logger, handler, level, or propagation state. Required
    jieba internals are checked explicitly so an incompatible upgrade fails
    visibly and receives a compatibility review instead of silently diverging.
    """

    def __init__(self) -> None:
        if not all(
            hasattr(jieba, name) for name in _JIEBA_INITIALIZATION_ATTRIBUTES
        ):
            raise RuntimeError("jieba tokenizer initialization API is unsupported")
        super().__init__()

    def initialize(self, dictionary: str | None = None) -> None:
        if dictionary:
            abs_path = jieba._get_abs_path(dictionary)
            if self.dictionary == abs_path and self.initialized:
                return
            self.dictionary = abs_path
            self.initialized = False
        else:
            abs_path = self.dictionary

        with self.lock:
            try:
                with jieba.DICT_WRITING[abs_path]:
                    pass
            except KeyError:
                pass
            if self.initialized:
                return

            if self.cache_file:
                cache_file = self.cache_file
            elif abs_path == jieba.DEFAULT_DICT:
                cache_file = "jieba.cache"
            else:
                cache_file = "jieba.u%s.cache" % md5(
                    abs_path.encode("utf-8", "replace")
                ).hexdigest()
            cache_file = os.path.join(self.tmp_dir or tempfile.gettempdir(), cache_file)
            cache_directory = os.path.dirname(cache_file)

            loaded_from_cache = False
            if os.path.isfile(cache_file) and (
                abs_path == jieba.DEFAULT_DICT
                or os.path.getmtime(cache_file) > os.path.getmtime(abs_path)
            ):
                try:
                    with open(cache_file, "rb") as cache_handle:
                        self.FREQ, self.total = marshal.load(cache_handle)
                    loaded_from_cache = True
                except (EOFError, OSError, ValueError):
                    pass

            if not loaded_from_cache:
                write_lock = jieba.DICT_WRITING.get(abs_path, threading.RLock())
                jieba.DICT_WRITING[abs_path] = write_lock
                with write_lock:
                    self.FREQ, self.total = self.gen_pfdict(self.get_dict_file())
                    try:
                        descriptor, temporary_path = tempfile.mkstemp(dir=cache_directory)
                        with os.fdopen(descriptor, "wb") as cache_handle:
                            marshal.dump((self.FREQ, self.total), cache_handle)
                        os.replace(temporary_path, cache_file)
                    except OSError:
                        pass

                try:
                    del jieba.DICT_WRITING[abs_path]
                except KeyError:
                    pass

            self.initialized = True


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
        self._tokenizer = _QuietTokenizer()

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
