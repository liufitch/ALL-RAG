import json

import httpx
import pytest
from pydantic import SecretStr

from rag_modules.config.settings import RerankSettings
from rag_modules.reranking import RerankClient, RerankError


@pytest.mark.asyncio
@pytest.mark.parametrize("api_key", ["", "test-token"])
async def test_rerank_maps_sorted_provider_results_to_input_positions(api_key):
    def respond(request):
        assert request.url == "http://models.test/v1/rerank"
        assert request.headers.get("Authorization") == (f"Bearer {api_key}" if api_key else None)
        assert json.loads(request.content) == {"model": "reranker", "query": "question", "documents": ["first", "second"], "top_n": 2}
        return httpx.Response(200, json={"results": [
            {"index": 1, "relevance_score": .9}, {"index": 0, "relevance_score": .2},
        ]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        reranker = RerankClient(RerankSettings(base_url="http://models.test/v1/", model="reranker", api_key=SecretStr(api_key)), http_client=http)
        assert await reranker("question", ["first", "second"]) == [.2, .9]
        await reranker.aclose()
        assert not http.is_closed  # The injected HTTP client belongs to the caller.


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    None, {}, {"results": []}, {"results": [{"index": 0, "relevance_score": .1}]},
    {"results": [{"index": 0, "relevance_score": .1}, {"index": 0, "relevance_score": .9}]},
    {"results": [{"index": 0, "relevance_score": .1}, {"index": 2, "relevance_score": .9}]},
    {"results": [{"index": 0, "relevance_score": .1}, {"index": True, "relevance_score": .9}]},
    {"results": [{"index": 0, "relevance_score": .1}, {"index": 1, "relevance_score": "secret-invalid-score"}]},
    {"results": [{"index": 0, "relevance_score": .1}, {"index": 1, "relevance_score": None}]},
])
async def test_invalid_provider_result_fails_safely(payload):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))) as http:
        reranker = RerankClient(RerankSettings(), http_client=http)
        with pytest.raises(RerankError, match="response is invalid") as error:
            await reranker("private question", ["private first", "private second"])
        assert "private" not in str(error.value)
        assert "secret" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [b'not-json', b'{"results":[{"index":0,"relevance_score":NaN}]}', b'{"results":[{"index":0,"relevance_score":Infinity}]}'])
async def test_malformed_or_nonfinite_scores_are_rejected(content):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=content))) as http:
        with pytest.raises(RerankError):
            await RerankClient(RerankSettings(), http_client=http)("question", ["first"])


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 429, 500, 503])
async def test_provider_error_does_not_expose_response_body(status):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(status, text="secret-token private source"))) as http:
        with pytest.raises(RerankError) as error:
            await RerankClient(RerankSettings(), http_client=http)("question", ["first"])
        assert str(error.value) == "Rerank provider is unavailable."


@pytest.mark.asyncio
async def test_owned_http_client_is_closed():
    client = RerankClient(RerankSettings())
    await client.aclose()
    assert client._http.is_closed
