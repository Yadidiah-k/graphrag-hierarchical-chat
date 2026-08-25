# Postgres migration design

Status: approved, ready for implementation
Date: 2026-08-25

## Context

The backend currently uses three separate stores: Qdrant (child chunk
vectors), SQLite (parent chunks), Neo4j (graph). This spec replaces Qdrant +
SQLite with a single Postgres instance (pgvector for embeddings, JSONB for
flexible metadata), and adds a query audit log. Neo4j is untouched.

This is the first item from `IMPROVEMENTS.md` Priority 2. Two related asks
from the same discussion are explicitly **out of scope** here and deferred to
their own specs, because they touch different subsystems (RAG pipeline /
graph extraction, not storage) and the first one overlaps with the Priority 1
ReAct rewrite already planned:
- LLM-generated rationale shown in the chat UI (the `rationale_text` column
  below is added now and left NULL until that spec lands)
- Extraction confidence + justification per entity/relationship (lives in
  Neo4j, unrelated to this migration)

## Goals

- Replace Qdrant + SQLite with Postgres (pgvector + JSONB), one instance.
- Fix a real bug found while designing this: `IngestRequest.title` is
  accepted by the API but dropped before it ever reaches `IngestionService`
  -- never persisted anywhere. Add a `documents` table and thread `title`
  through properly.
- Add a `query_logs` audit table so every `/chat` call is recorded (query,
  what was retrieved, what was answered, latency) -- this is also the
  storage foundation the Priority 3 evals/LangSmith work will need.
- Preserve existing call-site interfaces (`ParentChunkStore`, the
  vector-store class, `IngestionService`, `GraphRagPipeline`) so
  `ingestion.py` and `rag/pipeline.py` need no logic changes -- only their
  constructors/dependencies change in `main.py`.

## Non-goals

- No Alembic / tracked migrations. `Base.metadata.create_all()` at startup,
  matching the existing lightweight pattern (SQLite used
  `CREATE TABLE IF NOT EXISTS`, Neo4j uses `ensure_constraints()`). State this
  as a trade-off in the README later.
- No change to `hierarchical_chunker.py`'s chunk_id/parent_id generation
  scheme -- IDs stay the TEXT strings they already are.
- No change to Neo4j, graph extraction, or the RAG pipeline's retrieval
  logic.

## Stack

- **ORM**: SQLAlchemy 2.0, declarative models, sync `Engine` +
  `sessionmaker` (matches every other client in this codebase being sync;
  FastAPI routes already offload blocking calls via `asyncio.to_thread`).
- **Driver**: `psycopg[binary]` (psycopg 3).
- **Vectors**: `pgvector` Python package's `pgvector.sqlalchemy.Vector` type.
- **Server**: `pgvector/pgvector:pg16` Docker image (Postgres 16 + pgvector
  extension prebuilt -- no manual extension install step needed).

## Schema

```sql
-- documents: one row per ingested document. Fixes the dropped-title bug.
documents
  document_id   TEXT PRIMARY KEY
  title         TEXT NOT NULL
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()

-- parent_chunks: large context blocks (~1000 tokens)
parent_chunks
  parent_id     TEXT PRIMARY KEY
  document_id   TEXT NOT NULL REFERENCES documents(document_id)
  text          TEXT NOT NULL
  start_char    INTEGER NOT NULL
  end_char      INTEGER NOT NULL
  chunk_order   INTEGER NOT NULL
  token_count   INTEGER
  metadata      JSONB NOT NULL DEFAULT '{}'
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
  INDEX on document_id
  GIN INDEX on metadata

-- child_chunks: small precise blocks (~200 tokens), embedded
child_chunks
  child_id        TEXT PRIMARY KEY
  parent_id       TEXT NOT NULL REFERENCES parent_chunks(parent_id)
  document_id     TEXT NOT NULL REFERENCES documents(document_id)
  text            TEXT NOT NULL
  start_char      INTEGER NOT NULL
  end_char        INTEGER NOT NULL
  chunk_order     INTEGER NOT NULL
  token_count     INTEGER
  embedding       VECTOR(1536) NOT NULL   -- dimension = Settings.embedding_dimension
  embedding_model TEXT NOT NULL
  metadata        JSONB NOT NULL DEFAULT '{}'
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
  INDEX on document_id
  HNSW INDEX on embedding (vector_cosine_ops)
  GIN INDEX on metadata

-- query_logs: audit trail, one row per completed /chat call
query_logs
  id                  TEXT PRIMARY KEY        -- python-side uuid4, matches this codebase's TEXT-id convention
  query_text          TEXT NOT NULL
  document_id_filter  TEXT
  top_k               INTEGER NOT NULL
  citations           JSONB NOT NULL           -- list of {parent_id, document_id, score, text}
  linked_node_ids     JSONB NOT NULL           -- list of str
  graph_triples       JSONB NOT NULL           -- list of {source, relation, target, evidence} -- snapshot, not a live Neo4j reference
  answer_text         TEXT NOT NULL
  rationale_text      TEXT                     -- nullable, populated by a future spec
  latency_ms          INTEGER NOT NULL
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
```

`query_logs` is written only after a **successful** generation (mirrors how
ingest failures already surface via `IngestJobStatus.failed` plus normal app
logs -- logging failed chat turns is a reasonable future enhancement, not
in scope here).

## Interface contract (unchanged call sites)

- `ParentChunkStore`: keeps `save_many(parents)` / `get_many(parent_ids)`.
  Internals move from `sqlite3` to a SQLAlchemy `Session`.
- Vector store class: keeps `upsert_children(child_ids, vectors, payloads)` /
  `search(query_vector, top_k, document_id)` / `get_children_by_ids(child_ids)`.
  File renamed `app/vectorstore/qdrant_store.py` -> `app/vectorstore/postgres_store.py`,
  class renamed `QdrantVectorStore` -> `PostgresVectorStore`, factory function
  `build_vector_store(settings, dimension)` keeps its name/signature.
- `IngestionService.ingest(...)` gains a `title: str` parameter (currently
  silently dropped at the API layer) and writes/upserts the `documents` row
  before chunking.
- New `app/services/query_log.py::QueryLogStore.record(...)`, called from
  `app/api/routes/chat.py` after generation completes, timed with
  `time.perf_counter()` around the whole request.

## Files touched

New:
- `backend/app/db/__init__.py`
- `backend/app/db/models.py` -- SQLAlchemy declarative models: `Document`,
  `ParentChunkORM`, `ChildChunkORM`, `QueryLog`
- `backend/app/db/session.py` -- engine, sessionmaker, `init_db(engine)`
  (creates `vector` extension + `Base.metadata.create_all()`)
- `backend/app/services/query_log.py`
- `backend/app/vectorstore/postgres_store.py` (replaces `qdrant_store.py`)

Modified:
- `backend/app/core/config.py` -- replace `qdrant_url`, `qdrant_collection`,
  `parent_store_db_path` with `database_url: str` (default
  `postgresql+psycopg://graphrag:graphrag@postgres:5432/graphrag`)
- `backend/app/services/parent_store.py` -- SQLAlchemy-backed, same public
  interface; `build_parent_store(settings)` now builds/reuses the shared
  engine+session
- `backend/app/services/ingestion.py` -- accept `title`, write `documents`
  row
- `backend/app/api/routes/ingest.py` -- pass `payload.title` through
  (currently dropped)
- `backend/app/api/routes/chat.py` -- write a `query_logs` row after
  generation
- `backend/app/main.py` -- lifespan builds the SQLAlchemy engine once,
  calls `init_db()`, wires `PostgresVectorStore` + `ParentChunkStore` +
  `QueryLogStore` off it; drop Qdrant-specific wiring
- `backend/requirements.txt` -- add `sqlalchemy`, `psycopg[binary]`,
  `pgvector`; remove `qdrant-client`
- `docker-compose.yml` -- replace `qdrant` service with `postgres`
  (`pgvector/pgvector:pg16`, `pg_isready` healthcheck, `postgres_data`
  volume instead of `qdrant_storage` + `parent_chunks_data`); update `api`
  environment to `DATABASE_URL`
- `.env.example` -- add `POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB`

Deleted:
- `backend/app/vectorstore/qdrant_store.py`

## Verification plan

Without a real OpenAI key (same constraint as the rest of this build so
far), full ingest-success and chat-success paths can't be run end to end.
Verify what can actually be proven:

1. Every `app.*` module imports cleanly (repeat of the existing smoke-test
   pattern).
2. Direct unit-level test of the new store classes against a real local
   Postgres (via docker), bypassing OpenAI: construct `Document`,
   `ParentChunkORM`, `ChildChunkORM` rows directly with dummy embeddings,
   round-trip through `ParentChunkStore`/`PostgresVectorStore`, confirm
   `search()` returns expected hits. Directly call `QueryLogStore.record()`
   with dummy data and confirm the row lands with correct JSONB content.
3. `docker compose up` -- all services healthy, api boots.
4. `GET /api/v1/health` -> 200.
5. `POST /api/v1/ingest` with the same fake key used before -> chunking
   succeeds, `documents` + `parent_chunks` rows land in Postgres (verify via
   direct SQL query), job fails at the embedding call exactly as before
   (proves the title/documents fix and the parent-chunk write path work
   against real Postgres, using the same partial-failure signal that
   validated the SQLite version).
6. `GET /api/v1/graph/subgraph` -> still 200 with empty nodes/edges (proves
   Neo4j path is untouched).
7. Note explicitly in `PROGRESS.md`: real ingest+chat success paths, and the
   `query_logs` write from an actual `/chat` call, remain untested pending a
   real OpenAI key.

## Commit plan

Same layered style as the rest of this repo's history -- roughly:
1. `db` models + session setup
2. Postgres-backed `ParentChunkStore`
3. `PostgresVectorStore` (replaces qdrant_store.py)
4. `query_log.py` + wiring into `chat.py`
5. `ingestion.py` + `ingest.py` title fix
6. `main.py` wiring + `requirements.txt`
7. `docker-compose.yml` + `.env.example`
8. `PROGRESS.md` update with verification results

No Claude co-author trailer on any commit (established convention for this
repo -- see earlier history).
