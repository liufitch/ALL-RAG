from __future__ import annotations

from types import SimpleNamespace

import pytest


def test_preview_does_not_create_or_dispatch_a_process_rule(client, monkeypatch):
    from rag_modules.api import indexing_configuration_api

    calls = []

    async def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("configuration must only be saved after confirmation")

    monkeypatch.setattr(indexing_configuration_api, "get_configuration_service", forbidden, raising=False)
    assert calls == []


def test_configuration_route_is_registered(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/knowledge_base/{dataset_id}/indexing/configure" in paths
    assert "post" in paths["/api/knowledge_base/{dataset_id}/indexing/configure"]
