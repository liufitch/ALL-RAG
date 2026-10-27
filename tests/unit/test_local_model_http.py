"""Exercise local model clients through a real HTTP connection, without a model dependency."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from pydantic import SecretStr

from rag_modules.config.settings import EmbeddingSettings
from rag_modules.embeddings import OpenAICompatibleEmbeddingClient
from rag_modules.graph.extraction import GraphExtractor


@pytest.fixture
def local_model_http():
    authorizations = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            authorizations.append(self.headers.get("Authorization"))
            if self.path == "/v1/embeddings":
                assert request["input"] == ["腾讯位于深圳。"]
                payload = {"data": [{"index": 0, "embedding": [0.1, 0.2]}]}
            else:
                assert self.path == "/v1/chat/completions"
                payload = {"choices": [{"message": {"content": json.dumps({
                    "entities": [{"name": "腾讯", "type": "organization"}], "relations": [],
                })}}]}
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1", authorizations
        finally:
            server.shutdown()
            thread.join(timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["embedding", "graph"])
@pytest.mark.parametrize("api_key", ["", "local-test-token"])
async def test_model_clients_support_local_http_with_and_without_auth(local_model_http, kind, api_key):
    base_url, authorizations = local_model_http
    if kind == "embedding":
        client = OpenAICompatibleEmbeddingClient(EmbeddingSettings(
            base_url=base_url, api_key=SecretStr(api_key), max_retries=0,
        ))
    else:
        client = GraphExtractor(
            base_url=base_url, api_key=api_key, model="chat",
            entity_types=("organization",), relation_types=("位于",),
        )
    try:
        if kind == "embedding":
            result = await client.embed("bge-m3", ["腾讯位于深圳。"])
            assert result.dimension == 2
        else:
            result = await client.extract("腾讯位于深圳。")
            assert [entity.name for entity in result.entities] == ["腾讯"]
    finally:
        await client.aclose()
    assert authorizations == [f"Bearer {api_key}" if api_key else None]
