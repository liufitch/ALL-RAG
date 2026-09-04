from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

import httpx
import pytest
from pydantic import SecretStr

from rag_modules.config.settings import EmbeddingModelDefinition, EmbeddingSettings
from rag_modules.embeddings import (
    EmbeddingBatch,
    EmbeddingError,
    OpenAICompatibleEmbeddingClient,
)


def make_settings(
    *,
    base_url: str = "http://embed/v1",
    api_key: str = "secret",
    batch_size: int = 32,
    max_input_characters: int = 100,
    request_timeout: int = 7,
    max_retries: int = 0,
    enabled: bool = True,
) -> EmbeddingSettings:
    return EmbeddingSettings(
        base_url=base_url,
        api_key=SecretStr(api_key),
        default_model="bge-m3" if enabled else "fallback",
        models=[
            EmbeddingModelDefinition(
                id="bge-m3",
                model="backend-model",
                display_name="BGE-M3",
                enabled=enabled,
                batch_size=batch_size,
                max_input_characters=max_input_characters,
                request_timeout=request_timeout,
            ),
            *(
                [
                    EmbeddingModelDefinition(
                        id="fallback",
                        model="fallback",
                        display_name="Fallback",
                    )
                ]
                if not enabled
                else []
            ),
        ],
        max_retries=max_retries,
    )


def make_client(
    *,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    **settings_overrides: object,
) -> OpenAICompatibleEmbeddingClient:
    settings = make_settings(**settings_overrides)
    if sleep is None:
        return OpenAICompatibleEmbeddingClient(settings)
    return OpenAICompatibleEmbeddingClient(settings, sleep=sleep)


def assert_safe_error(
    error: EmbeddingError,
    *,
    code: str,
    retryable: bool,
    message: str,
) -> None:
    assert error.code == code
    assert error.retryable is retryable
    assert error.safe_message == message
    assert str(error) == message


@pytest.mark.asyncio
async def test_embed_restores_response_index_order_and_sends_protocol(respx_mock):
    route = respx_mock.post("http://embed/v1/embeddings").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.3, 0.4]},
                    {"index": 0, "embedding": [0.1, 0.2]},
                ]
            },
        )
    )
    client = make_client(base_url="http://embed/v1/", api_key="secret")

    result = await client.embed("bge-m3", ["first", "second"])

    assert result == EmbeddingBatch(
        vectors=((0.1, 0.2), (0.3, 0.4)),
        dimension=2,
    )
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer secret"
    assert request.extensions["timeout"] == {
        "connect": 7,
        "read": 7,
        "write": 7,
        "pool": 7,
    }
    assert request.read() == b'{"model":"backend-model","input":["first","second"]}'
    await client.aclose()


@pytest.mark.asyncio
async def test_embed_batches_requests_and_preserves_global_order(respx_mock):
    route = respx_mock.post("http://embed/v1/embeddings").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 1, "embedding": [2, 20]},
                        {"index": 0, "embedding": [1, 10]},
                    ]
                },
            ),
            httpx.Response(
                200,
                json={"data": [{"index": 0, "embedding": [3, 30]}]},
            ),
        ]
    )
    client = make_client(batch_size=2)

    result = await client.embed("bge-m3", ["one", "two", "three"])

    assert result.vectors == ((1.0, 10.0), (2.0, 20.0), (3.0, 30.0))
    assert [json.loads(call.request.content)["input"] for call in route.calls] == [
        ["one", "two"],
        ["three"],
    ]
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("texts", "private_value"),
    [
        ([], None),
        ([""], None),
        (["   "], None),
        (["x" * 101], "x" * 101),
        ([1], None),
    ],
)
async def test_invalid_input_is_rejected_before_request(
    respx_mock, texts, private_value
):
    route = respx_mock.post("http://embed/v1/embeddings")
    client = make_client()

    with pytest.raises(EmbeddingError) as captured:
        await client.embed("bge-m3", texts)

    assert_safe_error(
        captured.value,
        code="EMBEDDING_INPUT_INVALID",
        retryable=False,
        message="Embedding input is invalid.",
    )
    if private_value is not None:
        assert private_value not in str(captured.value)
    assert not route.called
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("model_id", ["missing", "disabled"])
async def test_unknown_or_disabled_model_is_safe_and_makes_no_request(
    respx_mock, model_id
):
    route = respx_mock.post("http://embed/v1/embeddings")
    client = make_client(enabled=model_id != "disabled")

    with pytest.raises(EmbeddingError) as captured:
        await client.embed(model_id, ["private input"])

    assert_safe_error(
        captured.value,
        code="EMBEDDING_MODEL_UNAVAILABLE",
        retryable=False,
        message="Embedding model is unavailable.",
    )
    assert model_id not in str(captured.value)
    assert not route.called
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failure_is_safe_and_not_retried(respx_mock, status):
    route = respx_mock.post("http://embed/v1/embeddings").mock(
        return_value=httpx.Response(status, text="secret backend detail")
    )
    client = make_client(api_key="top-secret", max_retries=3)

    with pytest.raises(EmbeddingError) as captured:
        await client.embed("bge-m3", ["private input"])

    assert_safe_error(
        captured.value,
        code="EMBEDDING_AUTH_FAILED",
        retryable=False,
        message="Embedding authentication failed.",
    )
    assert "private input" not in str(captured.value)
    assert "secret backend detail" not in str(captured.value)
    assert "top-secret" not in str(captured.value)
    assert route.call_count == 1
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 404, 418, 500])
async def test_non_retryable_http_failure_is_safe_and_not_retried(
    respx_mock, status
):
    route = respx_mock.post("http://embed/v1/embeddings").mock(
        return_value=httpx.Response(status, text="backend detail")
    )
    client = make_client(max_retries=3)

    with pytest.raises(EmbeddingError) as captured:
        await client.embed("bge-m3", ["private input"])

    assert_safe_error(
        captured.value,
        code="EMBEDDING_REQUEST_FAILED",
        retryable=False,
        message="Embedding request failed.",
    )
    assert "backend detail" not in str(captured.value)
    assert route.call_count == 1
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 502, 503, 504])
async def test_retryable_status_uses_bounded_retries_and_backoff(
    respx_mock, status
):
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    route = respx_mock.post("http://embed/v1/embeddings").mock(
        side_effect=[
            httpx.Response(status),
            httpx.Response(status),
            httpx.Response(
                200,
                json={"data": [{"index": 0, "embedding": [1, 2]}]},
            ),
        ]
    )
    client = make_client(max_retries=2, sleep=record_sleep)

    result = await client.embed("bge-m3", ["input"])

    assert result.vectors == ((1.0, 2.0),)
    assert route.call_count == 3
    assert len(delays) == 2
    assert 0 < delays[0] < delays[1] <= 2
    assert delays[1] == delays[0] * 2
    await client.aclose()


@pytest.mark.asyncio
async def test_final_retryable_failure_does_not_sleep_after_last_attempt(respx_mock):
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    route = respx_mock.post("http://embed/v1/embeddings").mock(
        return_value=httpx.Response(503, text="do not disclose")
    )
    client = make_client(max_retries=2, sleep=record_sleep)

    with pytest.raises(EmbeddingError) as captured:
        await client.embed("bge-m3", ["private"])

    assert_safe_error(
        captured.value,
        code="EMBEDDING_REQUEST_FAILED",
        retryable=True,
        message="Embedding request failed.",
    )
    assert route.call_count == 3
    assert len(delays) == 2
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("private host", request=httpx.Request("POST", "http://embed")),
        httpx.ReadTimeout("private timeout", request=httpx.Request("POST", "http://embed")),
    ],
)
async def test_network_failure_is_retried_and_sanitized(respx_mock, failure):
    route = respx_mock.post("http://embed/v1/embeddings").mock(
        side_effect=[
            failure,
            httpx.Response(
                200,
                json={"data": [{"index": 0, "embedding": [1, 2]}]},
            ),
        ]
    )
    client = make_client(max_retries=1, sleep=_no_sleep)

    result = await client.embed("bge-m3", ["private input"])

    assert result.vectors == ((1.0, 2.0),)
    assert route.call_count == 2
    await client.aclose()


async def _no_sleep(_: float) -> None:
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": "not-a-list"},
        {"data": []},
        {"data": [{"index": 0, "embedding": [1]}, {"index": 0, "embedding": [2]}]},
        {"data": [{"index": 2, "embedding": [1]}, {"index": 0, "embedding": [2]}]},
        {"data": [{"index": True, "embedding": [1]}, {"index": 0, "embedding": [2]}]},
        {"data": [{"index": 0, "embedding": "not-a-list"}, {"index": 1, "embedding": [2]}]},
        {"data": [{"index": 0, "embedding": []}, {"index": 1, "embedding": []}]},
        {"data": [{"index": 0, "embedding": [True]}, {"index": 1, "embedding": [2]}]},
        {"data": [{"index": 0, "embedding": ["1"]}, {"index": 1, "embedding": [2]}]},
        {"data": [{"index": 0, "embedding": [10**1000]}, {"index": 1, "embedding": [2]}]},
    ],
)
async def test_malformed_response_is_rejected_without_retry(respx_mock, payload):
    route = respx_mock.post("http://embed/v1/embeddings").mock(
        return_value=httpx.Response(200, json=payload)
    )
    client = make_client(max_retries=2)

    with pytest.raises(EmbeddingError) as captured:
        await client.embed("bge-m3", ["one", "two"])

    assert_safe_error(
        captured.value,
        code="EMBEDDING_RESPONSE_INVALID",
        retryable=False,
        message="Embedding response is invalid.",
    )
    assert route.call_count == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_non_json_response_is_rejected_safely(respx_mock):
    route = respx_mock.post("http://embed/v1/embeddings").mock(
        return_value=httpx.Response(200, content=b"private invalid response")
    )
    client = make_client()

    with pytest.raises(EmbeddingError) as captured:
        await client.embed("bge-m3", ["private input"])

    assert_safe_error(
        captured.value,
        code="EMBEDDING_RESPONSE_INVALID",
        retryable=False,
        message="Embedding response is invalid.",
    )
    assert "private invalid response" not in str(captured.value)
    assert route.call_count == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_dimension_mismatch_within_response_uses_dimension_error(respx_mock):
    route = respx_mock.post("http://embed/v1/embeddings").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"index": 0, "embedding": [1]},
                    {"index": 1, "embedding": [2, 3]},
                ]
            },
        )
    )
    client = make_client()

    with pytest.raises(EmbeddingError) as captured:
        await client.embed("bge-m3", ["one", "two"])

    assert_safe_error(
        captured.value,
        code="EMBEDDING_DIMENSION_MISMATCH",
        retryable=False,
        message="Embedding dimensions do not match.",
    )
    assert route.call_count == 1
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("non_finite", ["NaN", "Infinity", "-Infinity"])
async def test_non_finite_response_number_is_rejected(respx_mock, non_finite):
    route = respx_mock.post("http://embed/v1/embeddings").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=(
                '{"data":['
                f'{{"index":0,"embedding":[{non_finite}]}},'
                '{"index":1,"embedding":[2]}]}'
            ).encode(),
        )
    )
    client = make_client()

    with pytest.raises(EmbeddingError) as captured:
        await client.embed("bge-m3", ["one", "two"])

    assert_safe_error(
        captured.value,
        code="EMBEDDING_RESPONSE_INVALID",
        retryable=False,
        message="Embedding response is invalid.",
    )
    assert route.call_count == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_dimension_mismatch_across_batches_fails_safely(respx_mock):
    route = respx_mock.post("http://embed/v1/embeddings").mock(
        side_effect=[
            httpx.Response(
                200,
                json={"data": [{"index": 0, "embedding": [1, 2]}]},
            ),
            httpx.Response(
                200,
                json={"data": [{"index": 0, "embedding": [3, 4, 5]}]},
            ),
        ]
    )
    client = make_client(batch_size=1)

    with pytest.raises(EmbeddingError) as captured:
        await client.embed("bge-m3", ["one", "two"])

    assert_safe_error(
        captured.value,
        code="EMBEDDING_DIMENSION_MISMATCH",
        retryable=False,
        message="Embedding dimensions do not match.",
    )
    assert route.call_count == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_http_413_halves_batch_until_requests_succeed(respx_mock):
    route = respx_mock.post("http://embed/v1/embeddings").mock(
        side_effect=[
            httpx.Response(413, text="private detail"),
            httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 1, "embedding": [2]},
                        {"index": 0, "embedding": [1]},
                    ]
                },
            ),
            httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 1, "embedding": [4]},
                        {"index": 0, "embedding": [3]},
                    ]
                },
            ),
        ]
    )
    client = make_client(batch_size=4, max_retries=3)

    result = await client.embed("bge-m3", ["a", "b", "c", "d"])

    assert result.vectors == ((1.0,), (2.0,), (3.0,), (4.0,))
    assert [json.loads(call.request.content)["input"] for call in route.calls] == [
        ["a", "b", "c", "d"],
        ["a", "b"],
        ["c", "d"],
    ]
    await client.aclose()


@pytest.mark.asyncio
async def test_http_413_at_size_one_stops_without_retry_or_split(respx_mock):
    route = respx_mock.post("http://embed/v1/embeddings").mock(
        return_value=httpx.Response(413, text="private detail")
    )
    client = make_client(batch_size=1, max_retries=3)

    with pytest.raises(EmbeddingError) as captured:
        await client.embed("bge-m3", ["private input"])

    assert_safe_error(
        captured.value,
        code="EMBEDDING_REQUEST_FAILED",
        retryable=False,
        message="Embedding request failed.",
    )
    assert route.call_count == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_injected_http_client_is_reused_and_not_closed():
    requests: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [1, 2]}]},
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    client = OpenAICompatibleEmbeddingClient(
        make_settings(batch_size=1),
        http_client=http_client,
    )

    async with client:
        await client.embed("bge-m3", ["one"])
        await client.embed("bge-m3", ["two"])

    assert len(requests) == 2
    assert http_client.is_closed is False
    await http_client.aclose()


@pytest.mark.asyncio
async def test_internally_created_http_client_is_closed(monkeypatch):
    from rag_modules.embeddings import openai_compatible

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={"data": [{"index": 0, "embedding": [1]}]},
            )
        )
    )
    monkeypatch.setattr(openai_compatible.httpx, "AsyncClient", lambda: http_client)

    async with OpenAICompatibleEmbeddingClient(make_settings()) as client:
        result = await client.embed("bge-m3", ["one"])

    assert result.vectors == ((1.0,),)
    assert http_client.is_closed is True
