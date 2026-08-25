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

## Agentic RAG pipeline (query validation, grading, rationale) -- verified 2026-08-25
Implements `docs/superpowers/specs/2026-08-25-agentic-rag-pipeline-design.md`:
`app/rag/pipeline.py` is now a real branching/cyclic LangGraph --
`validate_query` (structured: gibberish/tone/verbosity/history-resolved
rewritten_query) short-circuits to `reject_response` on gibberish, otherwise
runs `retrieve_vector -> expand_parents -> link_entities -> traverse_graph
-> grade_context` (structured sufficiency check), which loops back to
`retrieve_vector` with widened `top_k`/`hop_depth` up to
`Settings.max_context_retries` before `generate_answer` (free text) ->
`generate_rationale` (structured, post-hoc explanation of which
chunks/relationships were actually used). `/chat` now accepts conversation
`history` and emits a new `rationale` SSE event after the token stream ends.

Per the spec, `answer()` (non-streaming) drives the fully compiled graph via
`.invoke()`, including the `add_conditional_edges` branches and the
`grade_context -> retrieve_vector` retry cycle. `answer_stream()` (what
`/chat` calls) keeps manually chaining node functions in Python up through
`grade_context`, with the retry as a plain loop -- same reasoning as the
existing code's pre-this-spec deviation from `.invoke()` for streaming, no
new pattern introduced. `generate_rationale` is deliberately not part of
`answer_stream`'s token generator: it needs the finished answer text, which
only exists after the caller has drained the stream, so it's a separate
public method that `chat.py` calls after collecting all tokens, before the
`query_logs` write.

Verified (smoke-tested, no real OpenAI key -- see limitation below):
- Every `app.*` module imports cleanly (`app.schemas.models`,
  `app.core.config`, `app.services.query_log`, `app.api.routes.chat`,
  `app.rag.pipeline`, `app.main`).
- `GraphRagPipeline.__init__` constructs the compiled graph without error
  with mocked stores; `pipeline._app.get_graph().draw_mermaid()` confirms
  the wiring: `validate_query -.-> reject_response` /
  `validate_query -.-> retrieve_vector` (gibberish branch) and
  `grade_context -.-> retrieve_vector` / `grade_context -.-> generate_answer`
  (the retry cycle) both render as genuine conditional edges, not a
  straight line.
- Direct unit tests of `GraphRagPipeline._parse_validation` /
  `_parse_grade` / `_parse_rationale` against synthetic LLM-shaped JSON --
  well-formed output, malformed JSON, missing fields, and an invalid
  `verbosity_preference` literal / out-of-range `relevance_score` all parse
  to sane fallback values without raising (same technique used previously
  to validate `graph/extraction.py`'s `_parse`).
- `ChatRequest`/`ChatMessage`/`ChatStreamEvent` (including a `rationale`
  event carrying a `Rationale` payload) round-trip through Pydantic
  correctly.
- `docker compose up --build` -> postgres, neo4j, api, ui all healthy.
- `GET /api/v1/health` -> 200 before and after the failed `/chat` call below
  (server didn't crash or wedge).
- `POST /api/v1/chat` with a fake `OPENAI_API_KEY` and a populated `history`
  array -> fails at `validate_query` (the first LLM call) with a real `401`
  from OpenAI, delivered as a single SSE `error` event, HTTP 200 on the
  streaming response, connection closes cleanly. Confirmed via `docker
  compose logs api` that no unhandled traceback reached the server process.
- Request validation on the new field: an invalid `history[].role` (e.g.
  `"system"`) correctly 422s before the pipeline is ever invoked; a request
  with no `history` field at all still works (defaults to `[]`).
- `query_logs` has 0 rows after the failed call -- confirmed via `psql`
  directly against the container, matching the spec's "log only completed
  turns" scope (gibberish/rejected and failed turns don't write a row).
  `\d query_logs` confirms the `rationale_text` column is still present and
  nullable.
- `docker compose down -v` -> clean teardown, no leftover containers/volumes.

### Limitation: nothing past validate_query is behaviorally verified
This bites harder than the Postgres migration's fake-key limitation: here,
the *first* pipeline step (`validate_query`) itself needs a real LLM call,
so with a fake key there is no partial-success signal at all past FastAPI's
own request validation. **Untested against a real model, pending a real
OpenAI key**: gibberish detection accuracy, tone/verbosity inference
quality, the rewritten_query reference-resolution behavior, context
grading/the widen-and-retry loop actually firing and improving results, and
rationale quality/accuracy (does `chunks_used`/`relationships_used`
actually match what the answer drew on). All of this is structurally wired
and unit-tested at the parsing layer, but "does it work" for the LLM-judgment
parts of this spec is an open question until it runs against real OpenAI.

### Deviation from the spec's file list
The spec's "Files touched" list is `rag/pipeline.py`, `schemas/models.py`,
`core/config.py`, `api/routes/chat.py`, `frontend/app.py`. It doesn't list
`app/services/query_log.py`, but `QueryLogStore.record()` had no
`rationale_text` parameter -- the column existed (added by the Postgres
migration in anticipation of this work) but nothing wrote to it, which is
exactly the gap this spec exists to close. Added an optional
`rationale_text: str | None = None` parameter and pass it through to the
`QueryLog` row; `chat.py` now calls it with `rationale.explanation`. Same
class of necessary, mechanical, in-scope deviation as the Postgres
migration's `pipeline.py` extension noted above -- flagging it explicitly
since it's outside the spec's listed file scope.

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
