"""SQLAlchemy engine/session lifecycle, shared by every Postgres-backed store.

One engine and one sessionmaker are built lazily on first use and reused for
the life of the process (mirroring the single-client-per-process pattern
already used for the Neo4j driver), so ParentChunkStore, PostgresVectorStore,
and QueryLogStore all talk to the same connection pool without main.py
having to thread an engine through every factory call.
"""

from __future__ import annotations

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.models import Base

_engine: Engine | None = None
_sessionmaker: sessionmaker[Session] | None = None


def build_engine(settings: Settings) -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(settings.database_url)
    return _engine


def get_sessionmaker(settings: Settings) -> sessionmaker[Session]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = sessionmaker(bind=build_engine(settings))
    return _sessionmaker


def init_db(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        Base.metadata.create_all(conn)
