"""Ingestion pipeline: document text -> hierarchical chunks -> vector store
+ graph store, with provenance preserved end-to-end.

Flow:
    text
    -> HierarchicalChunker            (parents + children)
    -> ParentChunkStore.save_many      (durable parent context)
    -> EmbeddingProvider.embed_texts   (child chunk vectors)
    -> QdrantVectorStore.upsert_children
    -> GraphExtractor.extract (per parent, using its child ids as provenance)
    -> Neo4jGraphStore.write_extraction
"""

from __future__ import annotations

import logging

from app.chunking.hierarchical_chunker import HierarchicalChunker
from app.graph.extraction import GraphExtractor
from app.graph.neo4j_client import Neo4jGraphStore
from app.schemas.models import ExtractionResult, IngestResponse, IngestJobStatus
from app.services.embeddings import EmbeddingProvider
from app.services.parent_store import ParentChunkStore
from app.vectorstore.qdrant_store import QdrantVectorStore

logger = logging.getLogger("graphrag.ingestion")


class IngestionService:
    def __init__(
        self,
        chunker: HierarchicalChunker,
        embedding_provider: EmbeddingProvider,
        vector_store: QdrantVectorStore,
        parent_store: ParentChunkStore,
        graph_extractor: GraphExtractor,
        graph_store: Neo4jGraphStore,
    ) -> None:
        self._chunker = chunker
        self._embeddings = embedding_provider
        self._vectors = vector_store
        self._parents = parent_store
        self._extractor = graph_extractor
        self._graph = graph_store

    def ingest(self, document_id: str, text: str) -> IngestResponse:
        chunk_result = self._chunker.chunk_document(document_id, text)
        logger.info(
            "chunked document",
            extra={
                "document_id": document_id,
                "parent_count": len(chunk_result.parents),
                "child_count": len(chunk_result.children),
            },
        )

        self._parents.save_many(chunk_result.parents)

        if chunk_result.children:
            texts = [c.text for c in chunk_result.children]
            vectors = self._embeddings.embed_texts(texts)
            payloads = [
                {
                    "parent_id": c.parent_id,
                    "document_id": c.document_id,
                    "text": c.text,
                }
                for c in chunk_result.children
            ]
            self._vectors.upsert_children(
                child_ids=[c.child_id for c in chunk_result.children],
                vectors=vectors,
                payloads=payloads,
            )

        children_by_parent: dict[str, list[str]] = {}
        for child in chunk_result.children:
            children_by_parent.setdefault(child.parent_id, []).append(child.child_id)

        total_nodes = 0
        total_relationships = 0
        for parent in chunk_result.parents:
            child_ids = children_by_parent.get(parent.parent_id, [])
            extraction: ExtractionResult = self._extractor.extract(
                parent_id=parent.parent_id,
                document_id=document_id,
                child_ids=child_ids,
                text=parent.text,
            )
            if extraction.nodes:
                self._graph.write_extraction(extraction)
                total_nodes += len(extraction.nodes)
                total_relationships += len(extraction.relationships)

        logger.info(
            "graph extraction complete",
            extra={
                "document_id": document_id,
                "node_count": total_nodes,
                "relationship_count": total_relationships,
            },
        )

        return IngestResponse(
            document_id=document_id,
            status=IngestJobStatus.succeeded,
            parent_chunk_count=len(chunk_result.parents),
            child_chunk_count=len(chunk_result.children),
            node_count=total_nodes,
            relationship_count=total_relationships,
        )
