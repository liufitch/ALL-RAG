"""Async client for the Jina/Cohere-compatible rerank protocol."""

from __future__ import annotations

import math
from collections.abc import Sequence

import httpx

from rag_modules.config.settings import RerankSettings


class RerankError(RuntimeError):
    """Safe provider error; retrieval can fall back to its original ranking."""


class RerankClient:
    def __init__(self, config: RerankSettings, *, http_client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._http = http_client or httpx.AsyncClient()
        self._owns_http = http_client is None

    async def __call__(self, query: str, documents: Sequence[str]) -> list[float]:
        if not documents:
            return []
        key = self.config.api_key.get_secret_value()
        try:
            response = await self._http.post(
                f"{self.config.base_url.rstrip('/')}/rerank",
                json={"model": self.config.model, "query": query, "documents": list(documents),
                      "top_n": len(documents)},
                headers={"Authorization": f"Bearer {key}"} if key else {},
                timeout=self.config.request_timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            raise RerankError("Rerank provider is unavailable.") from None
        try:
            payload = response.json()
            results = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(results, list) or len(results) != len(documents):
                raise ValueError
            scores: list[float | None] = [None] * len(documents)
            for item in results:
                if not isinstance(item, dict):
                    raise ValueError
                index, score = item.get("index"), item.get("relevance_score")
                if (type(index) is not int or not 0 <= index < len(scores) or scores[index] is not None
                        or type(score) not in (int, float) or not math.isfinite(float(score))):
                    raise ValueError
                scores[index] = float(score)
            return [float(score) for score in scores]
        except (ValueError, TypeError, OverflowError):
            raise RerankError("Rerank provider response is invalid.") from None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()
