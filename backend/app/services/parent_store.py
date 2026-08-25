"""Durable store for parent chunks.

Child chunks live in Qdrant for vector search. Parent chunks are the larger
context blocks that get passed to the LLM once a child match is found, so
they are stored separately, keyed by parent_id, and fetched cheaply at
retrieval time. SQLite is enough for this scope; production would use the
same Postgres instance as the rest of the app.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.schemas.models import ParentChunk

_DB_PATH = Path("/data/parent_chunks.db")


class ParentChunkStore:
    def __init__(self, db_path: Path = _DB_PATH) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS parent_chunks (
                parent_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                text TEXT NOT NULL,
                start_char INTEGER NOT NULL,
                end_char INTEGER NOT NULL,
                chunk_order INTEGER NOT NULL
            )
            """
        )
        self._conn.commit()

    def save_many(self, parents: list[ParentChunk]) -> None:
        self._conn.executemany(
            """
            INSERT OR REPLACE INTO parent_chunks
                (parent_id, document_id, text, start_char, end_char, chunk_order)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (p.parent_id, p.document_id, p.text, p.start_char, p.end_char, p.order)
                for p in parents
            ],
        )
        self._conn.commit()

    def get_many(self, parent_ids: list[str]) -> dict[str, ParentChunk]:
        if not parent_ids:
            return {}
        placeholders = ",".join("?" for _ in parent_ids)
        rows = self._conn.execute(
            f"""
            SELECT parent_id, document_id, text, start_char, end_char, chunk_order
            FROM parent_chunks WHERE parent_id IN ({placeholders})
            """,
            parent_ids,
        ).fetchall()
        return {
            row[0]: ParentChunk(
                parent_id=row[0],
                document_id=row[1],
                text=row[2],
                start_char=row[3],
                end_char=row[4],
                order=row[5],
            )
            for row in rows
        }


def build_parent_store() -> ParentChunkStore:
    return ParentChunkStore()
