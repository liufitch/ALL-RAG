from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import httpx

from rag_modules.config.settings import EmbeddingModelDefinition, EmbeddingSettings

from .models import EmbeddingBatch, EmbeddingError

_MODEL_ERROR = (
    "EMBEDDING_MODEL_UNAVAILABLE",
    False,
    "Embedding model is unavailable.",
)
_INPUT_ERROR = (
    "EMBEDDING_INPUT_INVALID",
    False,
    "Embedding input is invalid.",
)
_AUTH_ERROR = (
    "EMBEDDING_AUTH_FAILED",
    False,
    "Embedding authentication failed.",
)
_CLOSED_ERROR = (
    "EMBEDDING_CLIENT_CLOSED",
    False,
    "Embedding client is closed.",
)
_REQUEST_ERROR = "EMBEDDING_REQUEST_FAILED", "Embedding request failed."
_RESPONSE_ERROR = (
    "EMBEDDING_RESPONSE_INVALID",
    False,
    "Embedding response is invalid.",
)
_DIMENSION_ERROR = (
    "EMBEDDING_DIMENSION_MISMATCH",
    False,
    "Embedding dimensions do not match.",
)

_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
_AUTH_STATUSES = frozenset({401, 403})
_MAX_BACKOFF_SECONDS = 2.0
_INITIAL_BACKOFF_SECONDS = 0.1


class _BatchTooLarge(Exception):
    pass


class OpenAICompatibleEmbeddingClient:
    """兼容 OpenAI 嵌入协议的异步客户端。

    仅在未注入 HTTP 客户端时，由本客户端负责创建和关闭连接。
    所有对外异常均使用稳定、已脱敏的错误码和消息。
    """

    def __init__(
        self,
        settings: EmbeddingSettings,
        *,
        http_client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._http_client = http_client or httpx.AsyncClient()
        self._owns_http_client = http_client is None
        self._closed = False
        self._sleep = sleep
        self._endpoint = f"{settings.base_url.rstrip('/')}/embeddings"

    async def embed(
        self,
        model_id: str,
        texts: Sequence[str],
    ) -> EmbeddingBatch:
        if self._closed:
            raise EmbeddingError(*_CLOSED_ERROR)
        definition = self._resolve_model(model_id)
        normalized_texts = self._validate_texts(texts, definition)

        vectors: list[tuple[float, ...]] = []
        dimension: int | None = None
        for start in range(0, len(normalized_texts), definition.batch_size):
            batch = normalized_texts[start : start + definition.batch_size]
            result = await self._embed_batch_adaptive(definition, batch)
            if dimension is None:
                dimension = result.dimension
            elif dimension != result.dimension:
                raise EmbeddingError(*_DIMENSION_ERROR)
            vectors.extend(result.vectors)

        # 前面已拒绝空输入，因此执行成功时必然已确定向量维度。
        assert dimension is not None
        return EmbeddingBatch(vectors=tuple(vectors), dimension=dimension)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_http_client:
            await self._http_client.aclose()

    async def __aenter__(self) -> OpenAICompatibleEmbeddingClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    def _resolve_model(self, model_id: str) -> EmbeddingModelDefinition:
        try:
            return self._settings.get_model(model_id)
        except ValueError:
            raise EmbeddingError(*_MODEL_ERROR) from None

    @staticmethod
    def _validate_texts(
        texts: Sequence[str],
        definition: EmbeddingModelDefinition,
    ) -> tuple[str, ...]:
        if isinstance(texts, (str, bytes)) or not texts:
            raise EmbeddingError(*_INPUT_ERROR)

        validated: list[str] = []
        for text in texts:
            if (
                not isinstance(text, str)
                or not text.strip()
                or len(text) > definition.max_input_characters
            ):
                raise EmbeddingError(*_INPUT_ERROR)
            validated.append(text)
        return tuple(validated)

    async def _embed_batch_adaptive(
        self,
        definition: EmbeddingModelDefinition,
        texts: tuple[str, ...],
    ) -> EmbeddingBatch:
        try:
            return await self._request_batch(definition, texts)
        except _BatchTooLarge:
            if len(texts) == 1:
                raise EmbeddingError(
                    _REQUEST_ERROR[0],
                    False,
                    _REQUEST_ERROR[1],
                ) from None

            midpoint = len(texts) // 2
            left = await self._embed_batch_adaptive(definition, texts[:midpoint])
            right = await self._embed_batch_adaptive(definition, texts[midpoint:])
            if left.dimension != right.dimension:
                raise EmbeddingError(*_DIMENSION_ERROR)
            return EmbeddingBatch(
                vectors=left.vectors + right.vectors,
                dimension=left.dimension,
            )

    async def _request_batch(
        self,
        definition: EmbeddingModelDefinition,
        texts: tuple[str, ...],
    ) -> EmbeddingBatch:
        for attempt in range(self._settings.max_retries + 1):
            try:
                response = await self._http_client.post(
                    self._endpoint,
                    json={"model": definition.model, "input": list(texts)},
                    headers={
                        "Authorization": (
                            f"Bearer {self._settings.api_key.get_secret_value()}"
                        )
                    },
                    timeout=definition.request_timeout,
                )
            except httpx.RequestError:
                if attempt == self._settings.max_retries:
                    raise EmbeddingError(
                        _REQUEST_ERROR[0],
                        True,
                        _REQUEST_ERROR[1],
                    ) from None
                await self._sleep(self._backoff(attempt))
                continue

            if response.status_code == 413:
                raise _BatchTooLarge
            if response.status_code in _AUTH_STATUSES:
                raise EmbeddingError(*_AUTH_ERROR)
            if response.status_code in _RETRYABLE_STATUSES:
                if attempt == self._settings.max_retries:
                    raise EmbeddingError(
                        _REQUEST_ERROR[0],
                        True,
                        _REQUEST_ERROR[1],
                    )
                await self._sleep(self._backoff(attempt))
                continue
            if not 200 <= response.status_code < 300:
                raise EmbeddingError(
                    _REQUEST_ERROR[0],
                    False,
                    _REQUEST_ERROR[1],
                )
            return self._parse_response(response, len(texts))

        raise AssertionError("retry loop exhausted without returning or raising")

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(
            _INITIAL_BACKOFF_SECONDS * (2**attempt),
            _MAX_BACKOFF_SECONDS,
        )

    @staticmethod
    def _parse_response(response: httpx.Response, expected_count: int) -> EmbeddingBatch:
        try:
            payload: Any = response.json()
        except ValueError:
            raise EmbeddingError(*_RESPONSE_ERROR) from None

        if not isinstance(payload, dict):
            raise EmbeddingError(*_RESPONSE_ERROR)
        data = payload.get("data")
        if not isinstance(data, list) or len(data) != expected_count:
            raise EmbeddingError(*_RESPONSE_ERROR)

        ordered: list[tuple[float, ...] | None] = [None] * expected_count
        dimension: int | None = None
        for item in data:
            if not isinstance(item, dict):
                raise EmbeddingError(*_RESPONSE_ERROR)
            index = item.get("index")
            vector = item.get("embedding")
            if (
                type(index) is not int
                or index < 0
                or index >= expected_count
                or ordered[index] is not None
                or not isinstance(vector, list)
                or not vector
            ):
                raise EmbeddingError(*_RESPONSE_ERROR)

            converted: list[float] = []
            for value in vector:
                if type(value) not in (int, float):
                    raise EmbeddingError(*_RESPONSE_ERROR)
                try:
                    number = float(value)
                except OverflowError:
                    raise EmbeddingError(*_RESPONSE_ERROR) from None
                if not math.isfinite(number):
                    raise EmbeddingError(*_RESPONSE_ERROR)
                converted.append(number)

            if dimension is None:
                dimension = len(converted)
            elif dimension != len(converted):
                raise EmbeddingError(*_DIMENSION_ERROR)
            ordered[index] = tuple(converted)

        if dimension is None or any(vector is None for vector in ordered):
            raise EmbeddingError(*_RESPONSE_ERROR)
        return EmbeddingBatch(
            vectors=tuple(vector for vector in ordered if vector is not None),
            dimension=dimension,
        )
