from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EmbeddingBatch:
    """An immutable, ordered batch of embedding vectors."""

    vectors: tuple[tuple[float, ...], ...]
    dimension: int


class EmbeddingError(Exception):
    """A sanitized embedding failure suitable for crossing service boundaries."""

    def __init__(self, code: str, retryable: bool, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.retryable = retryable
        self.safe_message = safe_message
