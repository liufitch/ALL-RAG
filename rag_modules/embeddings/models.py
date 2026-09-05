from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EmbeddingBatch:
    """不可变且有序的一批嵌入向量。"""

    vectors: tuple[tuple[float, ...], ...]
    dimension: int


class EmbeddingError(Exception):
    """适合跨服务边界传递的已脱敏嵌入异常。"""

    def __init__(self, code: str, retryable: bool, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.retryable = retryable
        self.safe_message = safe_message
