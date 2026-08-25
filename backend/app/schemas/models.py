"""Shared Pydantic contracts for the GraphRAG API boundary."""

from __future__ import annotations

from enum import Enum
from typing import Literal

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
    metadata: dict = Field(default_factory=dict)


class ChildChunk(BaseModel):
    child_id: str
    parent_id: str
    document_id: str
    text: str
    start_char: int
    end_char: int
    order: int
    metadata: dict = Field(default_factory=dict)


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
    normalized_name: str = ""
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
    section_title: str | None = None
    content_type: Literal["prose", "table", "list", "other"] = "prose"


# ---------------------------------------------------------------------------
# Agentic RAG pipeline (query validation, context grading, rationale)
# ---------------------------------------------------------------------------


class QueryValidation(BaseModel):
    is_gibberish: bool
    tone: str
    verbosity_preference: Literal["brief", "detailed"]
    rewritten_query: str
    suggested_reply: str | None = None


class ContextGrade(BaseModel):
    is_sufficient: bool
    relevance_score: float
    missing_info: str | None = None


class Rationale(BaseModel):
    explanation: str
    chunks_used: list[str] = Field(default_factory=list)
    relationships_used: list[str] = Field(default_factory=list)


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


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    query: str
    document_id: str | None = None
    top_k: int | None = None
    history: list[ChatMessage] = Field(default_factory=list)
    section_title_filter: str | None = None
    content_type_filter: Literal["prose", "table", "list", "other"] | None = None


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
    triples = "triples"
    rationale = "rationale"
    done = "done"
    error = "error"


class ChatStreamEvent(BaseModel):
    type: ChatEventType
    data: str | list[CitationChunk] | list[CitationTriple] | Rationale | None = None


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
