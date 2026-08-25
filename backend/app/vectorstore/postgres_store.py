"""Vector store boundary backed by Postgres + pgvector.

Stores child chunks (small, precise) with a pointer back to their parent
chunk id, document id, and text, so a vector hit on a child chunk can be
expanded to full parent context at retrieval time. Shares the same
SQLAlchemy engine as ParentChunkStore and QueryLogStore -- one Postgres
instance now backs everything except the Neo4j graph.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.models import ChildChunkORM
from app.db.session import get_sessionmaker


@dataclass
class VectorHit:
    child_id: str
    parent_id: str
    document_id: str
    text: str
    score: float


class PostgresVectorStore:
    def __init__(
        self, settings: Settings, dimension: int, session_factory: sessionmaker[Session]
    ) -> None:
        self._session_factory = session_factory
        self._dimension = dimension
        self._embedding_model = settings.embedding_model

    def upsert_children(
        self,
        child_ids: list[str],
        vectors: list[list[float]],
        payloads: list[dict],
    ) -> None:
        if not (len(child_ids) == len(vectors) == len(payloads)):
            raise ValueError("child_ids, vectors, and payloads must be the same length")
        if not child_ids:
            return

        rows = [
            {
                "child_id": child_id,
                "parent_id": payload["parent_id"],
                "document_id": payload["document_id"],
                "text": payload["text"],
                "start_char": payload.get("start_char", 0),
                "end_char": payload.get("end_char", 0),
                "chunk_order": payload.get("chunk_order", 0),
                "token_count": payload.get("token_count"),
                "embedding": vector,
                "embedding_model": self._embedding_model,
            }
            for child_id, vector, payload in zip(child_ids, vectors, payloads)
        ]

        stmt = pg_insert(ChildChunkORM).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[ChildChunkORM.child_id],
            set_={
                "parent_id": stmt.excluded.parent_id,
                "document_id": stmt.excluded.document_id,
                "text": stmt.excluded.text,
                "start_char": stmt.excluded.start_char,
                "end_char": stmt.excluded.end_char,
                "chunk_order": stmt.excluded.chunk_order,
                "token_count": stmt.excluded.token_count,
                "embedding": stmt.excluded.embedding,
                "embedding_model": stmt.excluded.embedding_model,
            },
        )
        with self._session_factory() as session:
            session.execute(stmt)
            session.commit()

    def get_children_by_ids(self, child_ids: list[str]) -> dict[str, dict]:
        if not child_ids:
            return {}
        with self._session_factory() as session:
            rows = (
                session.query(ChildChunkORM)
                .filter(ChildChunkORM.child_id.in_(child_ids))
                .all()
            )
            return {
                row.child_id: {
                    "child_id": row.child_id,
                    "parent_id": row.parent_id,
                    "document_id": row.document_id,
                    "text": row.text,
                }
                for row in rows
            }

    def search(
        self,
        query_vector: list[float],
        top_k: int = 8,
        document_id: str | None = None,
    ) -> list[VectorHit]:
        with self._session_factory() as session:
            distance = ChildChunkORM.embedding.cosine_distance(query_vector)
            query = session.query(ChildChunkORM, distance.label("distance"))
            if document_id:
                query = query.filter(ChildChunkORM.document_id == document_id)
            rows = query.order_by(distance).limit(top_k).all()
            return [
                VectorHit(
                    child_id=row.ChildChunkORM.child_id,
                    parent_id=row.ChildChunkORM.parent_id,
                    document_id=row.ChildChunkORM.document_id,
                    text=row.ChildChunkORM.text,
                    score=1.0 - row.distance,
                )
                for row in rows
            ]


def build_vector_store(settings: Settings, dimension: int) -> PostgresVectorStore:
    return PostgresVectorStore(settings, dimension, get_sessionmaker(settings))
