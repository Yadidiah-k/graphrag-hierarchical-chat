"""Ingestion pipeline: document text -> hierarchical chunks -> vector store
+ graph store, with provenance preserved end-to-end.

Flow:
    (document_id, title)
    -> ParentChunkStore.save_document  (documents row)
    text
    -> HierarchicalChunker            (parents + children)
    -> ParentChunkStore.save_many      (durable parent context)
    -> EmbeddingProvider.embed_texts   (child chunk vectors)
    -> PostgresVectorStore.upsert_children
    -> GraphExtractor.extract (per parent, using its child ids as provenance)
    -> EntityResolver.resolve (rewrite provisional node ids onto existing
       cross-document matches, where confirmed)
    -> Neo4jGraphStore.write_extraction
    -> ParentChunkStore.update_metadata / PostgresVectorStore.update_children_metadata

The metadata backfill happens last, as a follow-up UPDATE, rather than being
folded into the initial parent/child INSERTs above: extraction is the most
failure-prone step (external LLM call), and this ordering means an
extraction failure still leaves chunks and vectors durably saved.
"""

from __future__ import annotations

import logging

from app.chunking.hierarchical_chunker import HierarchicalChunker
from app.graph.entity_resolution import EntityResolver
from app.graph.extraction import GraphExtractor
from app.graph.neo4j_client import Neo4jGraphStore
from app.schemas.models import ExtractionResult, IngestResponse, IngestJobStatus
from app.services.embeddings import EmbeddingProvider
from app.services.parent_store import ParentChunkStore
from app.vectorstore.postgres_store import PostgresVectorStore

logger = logging.getLogger("graphrag.ingestion")


class IngestionService:
    def __init__(
        self,
        chunker: HierarchicalChunker,
        embedding_provider: EmbeddingProvider,
        vector_store: PostgresVectorStore,
        parent_store: ParentChunkStore,
        graph_extractor: GraphExtractor,
        graph_store: Neo4jGraphStore,
        entity_resolver: EntityResolver,
    ) -> None:
        self._chunker = chunker
        self._embeddings = embedding_provider
        self._vectors = vector_store
        self._parents = parent_store
        self._extractor = graph_extractor
        self._graph = graph_store
        self._resolver = entity_resolver

    def ingest(self, document_id: str, title: str, text: str) -> IngestResponse:
        self._parents.save_document(document_id, title)

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
                    "start_char": c.start_char,
                    "end_char": c.end_char,
                    "chunk_order": c.order,
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
            extraction = self._resolver.resolve(extraction, document_id, parent.text)
            if extraction.nodes:
                self._graph.write_extraction(extraction)
                total_nodes += len(extraction.nodes)
                total_relationships += len(extraction.relationships)

            metadata = {"section_title": extraction.section_title, "content_type": extraction.content_type}
            self._parents.update_metadata(parent.parent_id, metadata)
            if child_ids:
                self._vectors.update_children_metadata(child_ids, metadata)

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
