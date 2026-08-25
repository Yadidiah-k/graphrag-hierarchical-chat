"""Durable store for parent chunks.

Child chunks live in Postgres (pgvector) for vector search. Parent chunks
are the larger context blocks that get passed to the LLM once a child match
is found, so they are stored separately, keyed by parent_id, and fetched
cheaply at retrieval time -- same Postgres instance as the rest of the app
now, previously a standalone SQLite file.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.models import Document, ParentChunkORM
from app.db.session import get_sessionmaker
from app.schemas.models import ParentChunk


class ParentChunkStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def save_document(self, document_id: str, title: str) -> None:
        stmt = pg_insert(Document).values(document_id=document_id, title=title)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Document.document_id], set_={"title": stmt.excluded.title}
        )
        with self._session_factory() as session:
            session.execute(stmt)
            session.commit()

    def save_many(self, parents: list[ParentChunk]) -> None:
        if not parents:
            return
        rows = [
            {
                "parent_id": p.parent_id,
                "document_id": p.document_id,
                "text": p.text,
                "start_char": p.start_char,
                "end_char": p.end_char,
                "chunk_order": p.order,
            }
            for p in parents
        ]
        stmt = pg_insert(ParentChunkORM).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[ParentChunkORM.parent_id],
            set_={
                "document_id": stmt.excluded.document_id,
                "text": stmt.excluded.text,
                "start_char": stmt.excluded.start_char,
                "end_char": stmt.excluded.end_char,
                "chunk_order": stmt.excluded.chunk_order,
            },
        )
        with self._session_factory() as session:
            session.execute(stmt)
            session.commit()

    def get_many(self, parent_ids: list[str]) -> dict[str, ParentChunk]:
        if not parent_ids:
            return {}
        with self._session_factory() as session:
            rows = session.scalars(
                select(ParentChunkORM).where(ParentChunkORM.parent_id.in_(parent_ids))
            ).all()
            return {
                row.parent_id: ParentChunk(
                    parent_id=row.parent_id,
                    document_id=row.document_id,
                    text=row.text,
                    start_char=row.start_char,
                    end_char=row.end_char,
                    order=row.chunk_order,
                )
                for row in rows
            }


def build_parent_store(settings: Settings) -> ParentChunkStore:
    return ParentChunkStore(get_sessionmaker(settings))
