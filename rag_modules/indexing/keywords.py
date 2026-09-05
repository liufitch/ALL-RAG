"""供经济模式索引使用的本地确定性关键词提取。"""

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

from .constants import MAX_KEYWORD_LENGTH

_CJK_CHARACTERS = (
    r"\u3400-\u4dbf"  # 扩展 A 区
    r"\u4e00-\u9fff"  # 统一表意文字区
    r"\uf900-\ufaff"  # 兼容表意文字区
    r"\U00020000-\U0002a6df"  # 扩展 B 区
    r"\U0002a700-\U0002b73f"  # 扩展 C 区
    r"\U0002b740-\U0002b81f"  # 扩展 D 区
    r"\U0002b820-\U0002ceaf"  # 扩展 E 区
    r"\U0002ceb0-\U0002ebef"  # 扩展 F 区
    r"\U0002ebf0-\U0002ee5f"  # 扩展 I 区
    r"\U0002f800-\U0002fa1f"  # 兼容表意文字补充区
    r"\U00030000-\U0003134f"  # 扩展 G 区
    r"\U00031350-\U000323af"  # 扩展 H 区
    r"\U000323b0-\U0003347f"  # 扩展 J 区
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
    """私有 jieba 分词器，保留初始化行为并省略初始化日志。

    当前支持的 jieba API 不提供实例级日志器。继承的初始化方法会通过模块级日志器
    写入四条日志，因此这里通过私有重写保留缓存和锁行为，仅省略这些日志调用。
    此实现不修改共享日志器、处理器、日志级别或传播状态。
    显式检查所依赖的 jieba 内部接口，遇到不兼容升级时明确报错并进行兼容性审查，
    避免行为在无提示的情况下发生偏离。
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
    """提取数量受限、结果可重复的本地经济模式关键词列表。

    仅扫描一次输入，并以流式方式生成原始词元：连续的中日韩字符片段仅由
    :mod:`jieba` 处理，其他 Unicode 单词片段仅由正则表达式处理，避免混合边界重复计数。
    计数器为每个被接受的不同词项最多保留一条记录，内存占用与不同词元的数量成正比。
    """

    def __init__(self) -> None:
        """将分词器状态保存在私有实例中，避免修改 jieba 全局状态。"""
        self._tokenizer = _QuietTokenizer()

    def extract(self, text: str, limit: int = 15) -> list[str]:
        """最多返回 ``limit`` 个关键词，先按分数排序，再按词项排序。"""
        self._validate_inputs(text, limit)
        if not text.strip():
            return []

        frequencies: Counter[str] = Counter()
        identifiers: set[str] = set()
        for raw_token in self._iter_raw_tokens(text):
            normalized, is_identifier = self._normalize(raw_token)
            if (
                not normalized
                or len(normalized) > MAX_KEYWORD_LENGTH
                or normalized in _STOPWORDS
            ):
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
