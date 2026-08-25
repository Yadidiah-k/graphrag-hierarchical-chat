"""SQLAlchemy declarative models for the Postgres-backed stores.

Documents, parent chunks, and child chunks mirror the ingestion pipeline's
three-tier hierarchy (chunker output -> durable parent context -> embedded
child vectors); query_logs is a flat audit table written once per completed
/chat call. Every id stays a TEXT primary key to match the scheme
hierarchical_chunker.py already generates -- no surrogate integer keys.
"""

from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Index, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func

# Matches Settings.embedding_dimension's default. Not read from Settings at
# import time since the column type has to be fixed at table-definition time.
EMBEDDING_DIMENSION = 1536


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "documents"

    document_id: Mapped[str] = mapped_column(Text, primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


class ParentChunkORM(Base):
    __tablename__ = "parent_chunks"

    parent_id: Mapped[str] = mapped_column(Text, primary_key=True)
    document_id: Mapped[str] = mapped_column(
        Text, ForeignKey("documents.document_id"), nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    start_char: Mapped[int] = mapped_column(Integer, nullable=False)
    end_char: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_order: Mapped[int] = mapped_column(Integer, nullable=False)
    token_count: Mapped[int | None] = mapped_column(Integer)
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_parent_chunks_document_id", "document_id"),
        Index("ix_parent_chunks_metadata_gin", "metadata", postgresql_using="gin"),
    )


class ChildChunkORM(Base):
    __tablename__ = "child_chunks"

    child_id: Mapped[str] = mapped_column(Text, primary_key=True)
    parent_id: Mapped[str] = mapped_column(
        Text, ForeignKey("parent_chunks.parent_id"), nullable=False
    )
    document_id: Mapped[str] = mapped_column(
        Text, ForeignKey("documents.document_id"), nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    start_char: Mapped[int] = mapped_column(Integer, nullable=False)
    end_char: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_order: Mapped[int] = mapped_column(Integer, nullable=False)
    token_count: Mapped[int | None] = mapped_column(Integer)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSION), nullable=False)
    embedding_model: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_child_chunks_document_id", "document_id"),
        Index(
            "ix_child_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_child_chunks_metadata_gin", "metadata", postgresql_using="gin"),
    )


class QueryLog(Base):
    __tablename__ = "query_logs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    document_id_filter: Mapped[str | None] = mapped_column(Text)
    top_k: Mapped[int] = mapped_column(Integer, nullable=False)
    citations: Mapped[list] = mapped_column(JSONB, nullable=False)
    linked_node_ids: Mapped[list] = mapped_column(JSONB, nullable=False)
    graph_triples: Mapped[list] = mapped_column(JSONB, nullable=False)
    answer_text: Mapped[str] = mapped_column(Text, nullable=False)
    rationale_text: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
