"""FastAPI dependency providers.

Every dependency reads a singleton built once at app startup (see
app.main's lifespan) off `request.app.state`, rather than constructing
new Neo4j/Qdrant/OpenAI clients per request.
"""

from __future__ import annotations

from fastapi import Request

from app.graph.neo4j_client import Neo4jGraphStore
from app.rag.pipeline import GraphRagPipeline
from app.services.ingestion import IngestionService


def get_ingestion_service(request: Request) -> IngestionService:
    return request.app.state.ingestion_service


def get_rag_pipeline(request: Request) -> GraphRagPipeline:
    return request.app.state.rag_pipeline


def get_graph_store(request: Request) -> Neo4jGraphStore:
    return request.app.state.graph_store
