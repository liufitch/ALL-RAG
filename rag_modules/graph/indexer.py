"""把已持久化的文档分段抽取为图节点和关系。"""

import asyncio
from contextlib import aclosing
from hashlib import sha256
import json

from rag_modules.config.settings import settings
from rag_modules.indexing.concurrency import bounded_map, wait_external, record_checkpoint
from rag_modules.indexing.engine import DocumentIndexingEngine

from .extraction import GraphExtractor


class GraphIndexer:
    def __init__(self, store, extractor: GraphExtractor, *, max_concurrency: int = 1,
                 signature: str = "graph-extraction-v1", heartbeat_interval: float = 10) -> None:
        self.store = store
        self.extractor = extractor
        self.max_concurrency = max_concurrency
        self.signature = signature
        self.heartbeat_interval = heartbeat_interval

    async def index(self, command, records, progress) -> None:
        from .factory import get_graph_store

        try:
            if not self.extractor.model.strip():
                raise ValueError("graph extraction model is not configured")
            store = self.store or await asyncio.to_thread(get_graph_store)
            total = len(records)
            if not records:
                return
            fingerprints = {record.id: sha256(json.dumps([
                self.signature, self.extractor.model, settings.graph.extraction_max_characters, record.content,
            ], ensure_ascii=False).encode()).hexdigest() for record in records}
            completed = await wait_external(DocumentIndexingEngine._run_sync(
                store.completed_segments, records[0].dataset_index_id, fingerprints,
            ), progress=progress, heartbeat_interval=self.heartbeat_interval)
            count = len(completed)
            if count:
                await progress.update("graph-extract", 90 + 8 * count // total, count, total)

            async def extract(record):
                return await self.extractor.extract(record.content[:settings.graph.extraction_max_characters])

            requests = bounded_map(
                extract, (record for record in records if record.id not in completed),
                concurrency=self.max_concurrency, progress=progress,
                heartbeat_interval=self.heartbeat_interval,
            )
            async with aclosing(requests):
                async for record, extraction in requests:
                    await progress.check_cancelled()
                    # Serialize Neo4j writes; store the checkpoint in the same
                    # transaction as its facts, including empty extractions.
                    await wait_external(DocumentIndexingEngine._run_sync(
                        lambda: store.upsert_segment(
                            record.dataset_id, record.dataset_index_id, record.document_id,
                            record.id, extraction, fingerprint=fingerprints[record.id],
                        )
                    ), progress=progress, heartbeat_interval=self.heartbeat_interval)
                    count += 1
                    await progress.update("graph-extract", 90 + 8 * count // total, count, total)
                    record_checkpoint(progress)
                    await progress.check_cancelled()
        finally:
            await self.extractor.aclose()

    async def aclose(self):
        await self.extractor.aclose()


def make_graph_indexer():
    if not settings.graph.enabled:
        return None
    extractor = GraphExtractor(
        base_url=settings.graph.extraction_base_url,
        api_key=settings.graph.extraction_api_key.get_secret_value(),
        model=settings.graph.extraction_model,
        entity_types=settings.graph.entity_types,
        relation_types=settings.graph.relation_types,
    )
    signature = json.dumps({
        "version": 3, "base_url": settings.graph.extraction_base_url,
        "entity_types": settings.graph.entity_types, "relation_types": settings.graph.relation_types,
    }, sort_keys=True)
    return GraphIndexer(None, extractor, max_concurrency=settings.graph.max_concurrency,
                        signature=signature, heartbeat_interval=settings.indexing.heartbeat_interval_seconds)
