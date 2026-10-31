"""把已持久化的文档分段抽取为图节点和关系。"""

import asyncio

from rag_modules.config.settings import settings

from .extraction import GraphExtractor


class GraphIndexer:
    def __init__(self, store, extractor: GraphExtractor) -> None:
        self.store = store
        self.extractor = extractor

    async def index(self, command, records, progress) -> None:
        from .factory import get_graph_store

        try:
            if not self.extractor.model.strip():
                raise ValueError("graph extraction model is not configured")
            store = self.store or await asyncio.to_thread(get_graph_store)
            total = len(records)
            for index, record in enumerate(records, start=1):
                await progress.check_cancelled()
                extraction = await self.extractor.extract(
                    record.content[:settings.graph.extraction_max_characters]
                )
                await asyncio.to_thread(
                    store.upsert_segment,
                    record.dataset_id, record.dataset_index_id, record.document_id,
                    record.id, extraction,
                )
                await progress.update(
                    "graph-extract",
                    90 + (8 * index // max(1, total)),
                    index,
                    total,
                )
                await progress.check_cancelled()
        finally:
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
    return GraphIndexer(None, extractor)
