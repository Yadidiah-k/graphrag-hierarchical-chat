# Progress (in-progress build, not final)

## Done
- `requirements.txt` - pinned backend dependencies (fastapi, langgraph, neo4j,
  sqlalchemy, psycopg, pgvector, ...)
- `app/core/config.py` - typed settings
- `app/schemas/models.py` - all API/domain Pydantic contracts
- `app/chunking/hierarchical_chunker.py` - parent/child chunker
- `app/services/embeddings.py` - embedding provider interface (OpenAI-backed)
- `app/db/models.py` - SQLAlchemy declarative models: `Document`,
  `ParentChunkORM`, `ChildChunkORM`, `QueryLog`
- `app/db/session.py` - shared engine/sessionmaker + `init_db()` (creates the
  `vector` extension and all tables)
- `app/services/parent_store.py` - Postgres-backed parent chunk storage
  (same instance as everything else now, previously a standalone SQLite file)
- `app/vectorstore/postgres_store.py` - pgvector-backed vector store for child
  chunks (replaces `qdrant_store.py`)
- `app/services/query_log.py` - `QueryLogStore`, audit log for completed
  `/chat` calls
- `app/graph/extraction.py` - LLM structured entity/relationship extraction
- `app/graph/neo4j_client.py` - Neo4j writer + N-hop traversal
- `app/services/ingestion.py` - orchestrates the full ingest pipeline; now
  writes the `documents` row (title was previously accepted by the API and
  silently dropped -- see Bugs below)
- `app/rag/pipeline.py` - LangGraph retrieval pipeline (vector search -> parent
  expansion -> entity linking -> graph traversal -> generation, streaming +
  non-streaming entry points)
- `app/api/routes/health.py` - GET /api/v1/health
- `app/api/routes/ingest.py` - POST /api/v1/ingest (202 + background job),
  GET /api/v1/ingest/{document_id} (status polling); now threads `title`
  through to the ingestion service
- `app/api/routes/chat.py` - POST /api/v1/chat, SSE stream of citations / graph
  triples / tokens / done; records a `query_logs` row after a successful
  generation, timed with `time.perf_counter()`
- `app/api/routes/graph.py` - GET /api/v1/graph/subgraph
- `app/main.py` - FastAPI app factory + lifespan wiring all clients onto
  app.state (builds the shared SQLAlchemy engine, calls `init_db()`)
- `backend/Dockerfile`
- `docker-compose.yml` (api, neo4j+apoc, postgres/pgvector) - `docker compose
  up` brings up the full backend stack
- `.env.example` - documents required env vars

## Postgres migration (replaces Qdrant + SQLite) -- verified 2026-08-25
Implements `docs/superpowers/specs/2026-08-25-postgres-migration-design.md`:
one Postgres instance (pgvector + JSONB) now backs child-chunk vectors,
parent chunks, documents, and a new `query_logs` audit table. Neo4j
untouched.

Verified against a real local Postgres (`pgvector/pgvector:pg16`, docker),
bypassing OpenAI:
- `init_db()` creates the `vector` extension and all four tables with the
  exact indexes from the spec (btree on `document_id`, GIN on `metadata`,
  HNSW on `embedding` with `vector_cosine_ops`) -- confirmed via `\d` on each
  table.
- `ParentChunkStore.save_document` / `save_many` / `get_many` round-trip
  correctly, including upsert-on-conflict (re-saving the same parent_id
  doesn't error).
- `PostgresVectorStore.upsert_children` / `search` / `get_children_by_ids`
  round-trip correctly: nearest-neighbor search returns the closer vector
  first, `document_id` filtering works, re-upsert is idempotent.
- `QueryLogStore.record` writes a row with correctly-shaped JSONB
  (`citations`, `linked_node_ids`, `graph_triples`), confirmed by querying
  the row back with raw SQL (`psycopg`), not just via the ORM.

Verified against the live docker-compose stack (real Postgres + Neo4j, fake
OpenAI key):
- `docker compose up` -> postgres, neo4j, api all healthy; api boots cleanly
  and `init_db()` runs without error against the real container.
- `GET /api/v1/health` -> 200.
- `GET /api/v1/graph/subgraph?query=...` -> 200 with empty nodes/edges --
  proves the Neo4j path is untouched by this migration.
- `POST /api/v1/ingest` with `title` set -> chunking succeeds, a `documents`
  row lands with the correct title (fixes the dropped-title bug), all 3
  `parent_chunks` rows land with correct char offsets, job then goes
  `queued -> failed` with a real `401 AuthenticationError` from OpenAI at the
  embedding call (fake key) -- `child_chunks` correctly has 0 rows at that
  point. Verified by direct SQL query against the running container, not
  just the API response.
- `POST /api/v1/chat` -> fails gracefully via an SSE `error` event with the
  same real `401` from OpenAI; server stays healthy afterward; confirmed
  `query_logs` has 0 rows after the failed call (it's written only after a
  successful generation, per spec).
- `docker compose down -v` -> clean teardown, no leftover containers or
  volumes.
- NOT yet tested: a real ingest -> chat round trip with a valid OpenAI key,
  which is the only way to verify a `query_logs` row gets written from an
  actual `/chat` call with real citations/triples/answer content.

### Deviation from the spec's file list
The spec's "Files touched" list doesn't include `app/rag/pipeline.py`, but
two changes there turned out to be unavoidable:
- `rag/pipeline.py` imported `QdrantVectorStore` from `app.vectorstore.
  qdrant_store` for type hints. Since that module is deleted by this spec,
  leaving the import would break `app.rag.pipeline` (and therefore
  `app.main`) on import. Fixed by pointing the import/type hints at
  `PostgresVectorStore` -- a mechanical rename, no logic change.
- `query_logs.linked_node_ids` is NOT NULL and meant to hold real data (the
  entity ids linked during retrieval), but `GraphRagPipeline.answer_stream()`
  computes `linked_node_ids` internally and previously didn't return it --
  only `(token_iter, citations, triples)`. Logging an empty list for every
  row would make that column permanently useless, defeating the audit log's
  stated purpose. Extended the return tuple to
  `(token_iter, citations, triples, linked_node_ids)` and updated `chat.py`
  to pass it through. This does not change any retrieval/graph logic -- it
  only surfaces state the pipeline already computes. Flagging this
  explicitly since it's outside the spec's listed file scope.

### Bugs found and fixed while implementing this migration
- SQLAlchemy reserves the `metadata` attribute name on declarative models
  (it's `Base.metadata`). The `parent_chunks.metadata` / `child_chunks.
  metadata` JSONB columns are mapped through a `metadata_` Python attribute
  name (`mapped_column("metadata", JSONB, ...)`) to avoid colliding with it.

## Verified against the live docker-compose stack (real Neo4j + Qdrant, fake OpenAI key) [superseded by Postgres migration above]
- `docker compose up` -> all three services healthy, api boots cleanly.
- `GET /api/v1/health` -> 200.
- `GET /api/v1/graph/subgraph?query=...` -> real Neo4j+APOC N-hop traversal,
  200 with empty nodes/edges (no data ingested yet).
- `POST /api/v1/ingest` -> real chunking runs, background job correctly goes
  `queued -> failed` with an actual `401` from OpenAI (fake test key) --
  proves the async job flow and error handling, not just wiring.
- NOT yet tested: a real ingest -> chat round trip with a valid OpenAI key
  (needed to verify embeddings, LLM extraction, and generation actually
  produce sane output).

## Bugs found and fixed by actually running the stack
- `ParentChunkStore` had `/data/parent_chunks.db` hardcoded, which only
  exists inside the container -- broke on bare-host smoke tests. Now reads
  `Settings.parent_store_db_path`.
- Qdrant's official image has no `wget`/`curl`, so a `wget`-based
  healthcheck always failed silently -- would have wedged `api` behind a
  dependency that could never report healthy. Switched to a bash
  `/dev/tcp` check.
- `qdrant-client` 1.19.0 warned about incompatibility with
  `qdrant/qdrant:v1.12.4`; bumped the server image to `v1.19.0` to match.

## Verified (smoke-tested, no live services required) [SQLite/Qdrant era, superseded above]
- Every `app.*` module imports cleanly.
- Chunker + SQLite parent store round-trip correctly on real text (parent
  chunks ~1000 tokens, child chunks ~200 tokens, matches config).
- Full app boots via `TestClient` with only the two live network calls
  mocked (Qdrant collection check, Neo4j constraint creation): health check
  returns 200, ingest input validation and 404s behave correctly, all 5
  routes resolve in the OpenAPI schema.
- NOT yet tested: an actual ingest -> chat round trip against real
  OpenAI/Qdrant/Neo4j (needs API key + docker-compose).

## Not started yet
- `frontend/` - chat UI + citations + graph visualizer, plus a `ui` service
  added to docker-compose once it exists
- `backend/tests/`
- `README.md` - architecture, setup, sample queries, trade-offs

## Known design trade-offs to call out in the README
- Graph node ids are namespaced per document
  (`f"{document_id}:{entity_name}"` in `extraction.py`), so there is no
  cross-document entity resolution yet -- each document gets its own
  subgraph rather than one merged knowledge graph. Acceptable for this
  scope; would need a real entity-resolution step to fix.
- Entity linking at query time (`find_node_ids_by_name_fragment`) is a
  cheap substring match over all node names, not real NER/linking --
  called out in the code as a take-home shortcut.
