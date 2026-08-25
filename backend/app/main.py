"""FastAPI app factory.

Builds every stateful client (embeddings, Qdrant, SQLite, Neo4j) once in
the lifespan context and hangs them off `app.state`, so request
handlers reuse connections instead of opening new ones per call.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import chat, graph, health, ingest
from app.chunking.hierarchical_chunker import HierarchicalChunker
from app.core.config import get_settings
from app.graph.extraction import build_graph_extractor
from app.graph.neo4j_client import build_graph_store
from app.rag.pipeline import build_rag_pipeline
from app.schemas.models import IngestResponse
from app.services.embeddings import build_embedding_provider
from app.services.ingestion import IngestionService
from app.services.parent_store import build_parent_store
from app.vectorstore.qdrant_store import build_vector_store


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()

    embedding_provider = build_embedding_provider(settings)
    vector_store = build_vector_store(settings, dimension=embedding_provider.dimension)
    parent_store = build_parent_store(settings)
    graph_extractor = build_graph_extractor(settings)
    graph_store = build_graph_store(settings)
    chunker = HierarchicalChunker(
        parent_chunk_tokens=settings.parent_chunk_tokens,
        child_chunk_tokens=settings.child_chunk_tokens,
        chunk_overlap_tokens=settings.chunk_overlap_tokens,
    )

    app.state.settings = settings
    app.state.graph_store = graph_store
    app.state.ingestion_service = IngestionService(
        chunker=chunker,
        embedding_provider=embedding_provider,
        vector_store=vector_store,
        parent_store=parent_store,
        graph_extractor=graph_extractor,
        graph_store=graph_store,
    )
    app.state.rag_pipeline = build_rag_pipeline(
        settings, embedding_provider, vector_store, parent_store, graph_store
    )
    app.state.ingest_jobs: dict[str, IngestResponse] = {}

    yield

    graph_store.close()


def create_app() -> FastAPI:
    app = FastAPI(title="GraphRAG API", version="0.1.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router, prefix="/api/v1", tags=["health"])
    app.include_router(ingest.router, prefix="/api/v1", tags=["ingest"])
    app.include_router(chat.router, prefix="/api/v1", tags=["chat"])
    app.include_router(graph.router, prefix="/api/v1", tags=["graph"])
    return app


app = create_app()
