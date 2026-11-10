import json

import httpx
import pytest

from rag_modules.graph.extraction import GraphExtractor, parse_extraction
from rag_modules.config.settings import GraphSettings, Settings


def test_parse_extraction_keeps_only_grounded_entities_and_relations():
    result = parse_extraction(
        {
            "entities": [
                {"name": "腾讯", "type": "organization", "attributes": {"行业": "科技"}},
                {"name": "深圳", "type": "location", "attributes": {}},
                {"name": "虚构公司", "type": "organization", "attributes": {}},
                {"name": "腾讯", "type": "organization", "attributes": {}},
            ],
            "relations": [
                {"source": "腾讯", "target": "深圳", "type": "位于"},
                {"source": "腾讯", "target": "虚构公司", "type": "合作"},
            ],
        },
        "腾讯位于深圳。",
    )
    assert [(item.name, item.type) for item in result.entities] == [
        ("腾讯", "organization"), ("深圳", "location")
    ]
    assert [(item.source, item.target, item.type) for item in result.relations] == [
        ("腾讯", "深圳", "位于")
    ]


@pytest.mark.asyncio
async def test_extractor_sends_schema_and_reads_openai_compatible_json():
    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "entities": [{"name": "腾讯", "type": "organization"}], "relations": []
        }, ensure_ascii=False)}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        extractor = GraphExtractor(
            base_url="http://model/v1", api_key="secret", model="chat",
            entity_types=("organization",), relation_types=("位于",),
            http_client=http_client,
        )
        result = await extractor.extract("腾讯是一家公司。")

    assert [item.name for item in result.entities] == ["腾讯"]
    assert captured[0]["model"] == "chat"
    assert "organization" in captured[0]["messages"][0]["content"]
    assert "实体名称必须去重" in captured[0]["messages"][0]["content"]
    assert captured[0]["response_format"] == {"type": "json_object"}
    assert captured[0]["frequency_penalty"] == 0.5


def test_parse_extraction_rejects_invalid_payload():
    with pytest.raises(ValueError, match="invalid graph extraction"):
        parse_extraction({"entities": "wrong", "relations": []}, "text")


@pytest.mark.asyncio
@pytest.mark.parametrize("content,finish_reason", [
    ('{"entities":[{"name":"腾讯', "length"),
    ('{"entities":"wrong","relations":[]}', "stop"),
    ('{"entities":[],"relations":[]}', "length"),
])
async def test_extractor_recovers_invalid_or_truncated_response_with_bounded_schema(content, finish_reason):
    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        result = {"entities": [{"name": "腾讯", "type": "organization", "attributes": {}}], "relations": []}
        return httpx.Response(200, json={"choices": [{
            "finish_reason": finish_reason if len(captured) == 1 else "stop",
            "message": {"content": content if len(captured) == 1 else json.dumps(result)},
        }]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        extractor = GraphExtractor(base_url="http://model/v1", api_key="", model="chat",
            entity_types=("organization",), relation_types=("位于",), http_client=client)
        result = await extractor.extract("腾讯是一家公司。")

    assert [entity.name for entity in result.entities] == ["腾讯"]
    assert len(captured) == 2
    assert captured[1]["response_format"]["type"] == "json_schema"
    schema = captured[1]["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["entities"]["maxItems"] <= 6
    assert schema["properties"]["relations"]["maxItems"] <= 8
    assert schema["properties"]["entities"]["items"]["properties"]["attributes"]["additionalProperties"] is False
    assert captured[1]["max_tokens"] >= 2048
    assert captured[1]["messages"][-1]["content"] == "腾讯是一家公司。"


@pytest.mark.asyncio
async def test_extractor_stops_after_one_format_recovery_and_never_returns_partial_facts():
    calls = 0

    def respond(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"choices": [{"finish_reason": "length",
            "message": {"content": '{"entities":[{"name":"腾讯'}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        extractor = GraphExtractor(base_url="http://model/v1", api_key="", model="chat",
            entity_types=("organization",), relation_types=("位于",), http_client=client)
        with pytest.raises(ValueError, match="invalid graph extraction response"):
            await extractor.extract("腾讯是一家公司。")
    assert calls == 2


def test_graph_is_disabled_by_default_and_can_be_configured(monkeypatch):
    assert GraphSettings().enabled is False
    monkeypatch.setenv("GRAPH__ENABLED", "true")
    monkeypatch.setenv("GRAPH__URI", "bolt://graph:7687")
    monkeypatch.setenv("GRAPH__PASSWORD", "secret")
    loaded = Settings(_env_file=None)
    assert loaded.graph.enabled is True
    assert loaded.graph.uri == "bolt://graph:7687"
    assert "secret" not in repr(loaded.graph)
