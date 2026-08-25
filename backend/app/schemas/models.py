"""Shared Pydantic contracts for the GraphRAG API boundary."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


class ParentChunk(BaseModel):
    parent_id: str
    document_id: str
    text: str
    start_char: int
    end_char: int
    order: int


class ChildChunk(BaseModel):
    child_id: str
    parent_id: str
    document_id: str
    text: str
    start_char: int
    end_char: int
    order: int


class HierarchicalChunkResult(BaseModel):
    document_id: str
    parents: list[ParentChunk]
    children: list[ChildChunk]


# ---------------------------------------------------------------------------
# Graph extraction
# ---------------------------------------------------------------------------


class GraphNode(BaseModel):
    node_id: str
    name: str
    type: str
    source_child_ids: list[str] = Field(default_factory=list)
    source_parent_ids: list[str] = Field(default_factory=list)


class GraphRelationship(BaseModel):
    source_node_id: str
    target_node_id: str
    relation_type: str
    evidence: str
    source_child_ids: list[str] = Field(default_factory=list)


class ExtractionResult(BaseModel):
    nodes: list[GraphNode]
    relationships: list[GraphRelationship]


# ---------------------------------------------------------------------------
# Ingestion API
# ---------------------------------------------------------------------------


class IngestRequest(BaseModel):
    document_id: str
    title: str
    text: str


class IngestJobStatus(str, Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class IngestResponse(BaseModel):
    document_id: str
    status: IngestJobStatus
    parent_chunk_count: int | None = None
    child_chunk_count: int | None = None
    node_count: int | None = None
    relationship_count: int | None = None


# ---------------------------------------------------------------------------
# Chat API
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    query: str
    document_id: str | None = None
    top_k: int | None = None


class CitationChunk(BaseModel):
    parent_id: str
    document_id: str
    text: str
    score: float


class CitationTriple(BaseModel):
    source: str
    relation: str
    target: str
    evidence: str


class ChatEventType(str, Enum):
    token = "token"
    citations = "citations"
    done = "done"
    error = "error"


class ChatStreamEvent(BaseModel):
    type: ChatEventType
    data: str | list[CitationChunk] | list[CitationTriple] | None = None


# ---------------------------------------------------------------------------
# Graph subgraph API
# ---------------------------------------------------------------------------


class SubgraphNode(BaseModel):
    id: str
    label: str
    type: str


class SubgraphEdge(BaseModel):
    source: str
    target: str
    relation: str
    evidence: str


class SubgraphResponse(BaseModel):
    nodes: list[SubgraphNode]
    edges: list[SubgraphEdge]
