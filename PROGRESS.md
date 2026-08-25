# Progress (in-progress build, not final)

## Real-model re-verification with a secondary OpenRouter key -- 2026-08-25

Both real-model gaps left open by the previous two entries below (few-shot/
CoT extraction quality, entity resolution's LLM confirmation step) were
blocked by the primary OpenRouter key's exhausted daily quota. A second
key was made available; used sparingly (10 LLM calls total across four
short documents) rather than a full re-run, specifically targeting only
what was actually unverified.

**Entity resolution -- both directions of the LLM confirmation now proven
against a real model, not a mock:**
- Ingested `real-key2-doc-a` ("Silverline Capital... completed its
  acquisition of Bramblewood Foods... Managing Partner Derek Chen") then,
  separately, `real-key2-doc-b` ("Silverline Capital announced a fifteen
  million dollar investment..."). Direct Cypher query afterward:
  ```
  MATCH (e:Entity) WHERE e.normalized_name = 'silverline_capital'
  RETURN e.node_id, e.name, e.source_parent_ids
  ```
  → **exactly one node**, `source_parent_ids` containing parent chunk ids
  from *both* documents. Since these are genuinely different documents,
  this could not have been the same-document auto-confirm path -- the LLM
  confirmation call had to actually run and correctly judge them the same
  real-world entity. `docker logs` showed no errors during the run.
- Ingested `real-key2-doc-c` ("TransGlobal Shipping named Marcus Reed as
  its new CFO...") and `real-key2-doc-d` ("The city council appointed
  Marcus Reed to lead the downtown revitalization committee... a local
  architect...") -- two different people who happen to share a name, by
  design. Same query for `marcus_reed` afterward → **two separate nodes**,
  each with provenance from only its own document. This is the actual
  motivation for choosing LLM confirmation over blind exact-match merging
  (option 1 alone would have wrongly fused these) -- now confirmed working
  against a real model, not just asserted in the design doc.

**Few-shot/CoT restraint -- confirmed incidentally, not just by design**:
`real-key2-doc-b`'s text mentions "a renewable energy startup" without
naming it. The extractor correctly returned 0 relationships for that
document rather than inventing a node for the unnamed startup -- exactly
the restraint behavior the weak-signal few-shot example was written to
teach, now observed for real rather than only unit-tested against
synthetic JSON.

Stack torn down (`docker compose down -v`) after these four ingests --
kept the real-model verification budget small and targeted rather than
re-running the full original test suite against the new key.

## Cross-document entity resolution -- 2026-08-25

Implements `docs/superpowers/specs/2026-08-25-cross-document-entity-resolution-design.md`.
`IMPROVEMENTS.md`'s last open item: graph node ids were namespaced per
document (`f"{document_id}:{name}"`), so "Acme Corp" in two different
ingested documents became two disconnected Neo4j nodes.

`GraphExtractor._parse` (`app/graph/extraction.py`) now generates a
provisional, globally-scoped id (`{normalized_name}:{uuid4().hex[:8]}`, no
`document_id` in it) and a new `GraphNode.normalized_name` field
(`app/schemas/models.py`). `document_id` is still passed into
`extract`/`_parse` even though unused for id generation now -- kept rather
than removed, since it avoids signature churn and the same `document_id`
is already needed one line later in `ingestion.py`'s loop for the resolver
call (the spec left this as an explicit implementer's-call point).

New `app/graph/entity_resolution.py`: `EntityResolver.resolve()`, called
once per parent chunk between `extract()` and `write_extraction()`
(wired in `app/services/ingestion.py`, constructed in `app/main.py`).
Batch-looks-up normalized names via a new `Neo4jGraphStore` method,
`find_candidates_by_normalized_name` (backed by a new
`entity_normalized_name` index). For each candidate: if the existing
entity's `source_parent_ids` already contains an id from *this* ingestion's
`document_id`, auto-confirms without an LLM call (a same-document repeat
mention -- the old scheme already merged this for free, and re-confirming
it would newly cost an LLM call per repeat mention within a single
document, which it didn't before). Otherwise it's a genuinely
cross-document candidate: one batched LLM call per parent chunk (all
ambiguous candidates together), giving the model the new mention's
`parent_text` plus a new `get_relationship_summary()` method's view of
what's already known about each existing candidate. Structured output,
same `response_format=json_object` + manual-parse pattern as everywhere
else in this codebase; a malformed/missing `same_as_existing` defaults to
`False` -- a missed merge is a smaller problem than a wrongful one
(mirrors `_parse_grade`'s style in `app/rag/pipeline.py`). Confirmed
matches get their provisional `node_id` rewritten in place to the existing
node's id (nodes are not dropped -- they still need to reach `MERGE` so
their provenance accumulates), and any relationship referencing a renamed
id is rewritten too.

**Verified**:
```
cd backend && .venv/bin/python -m pytest tests/ -v
```
**68 passed** (up from 53) -- includes the rewritten
`test_extraction_parsing.py::test_node_id_is_namespaced_by_document_and_lowercased`
(renamed `test_node_id_is_globally_scoped_by_normalized_name_not_document`,
now asserting the new scheme instead of pinning the old one) and a new
`tests/test_entity_resolution.py` (`EntityResolver._parse_decisions`
malformed/missing-field coverage, plus `resolve()` logic against a fake
graph store covering no-candidate, same-document auto-confirm, and both
LLM-confirmed cross-document outcomes -- all pure logic, no LLM call, no
docker).

Store-level verification against **real Neo4j** (docker, `neo4j` service
only -- `EntityResolver` never touches Postgres): constructed
`ExtractionResult`s with the same entity name under different
`document_id`s, monkeypatched `EntityResolver._confirm_via_llm` to avoid a
real OpenAI/OpenRouter call, and confirmed via direct Cypher queries
against the running container:
- Cross-document match, LLM confirms same -> **one** merged node, with
  `source_parent_ids` containing both documents' parent ids, and the
  pre-existing `IS_CEO_OF` relationship still correctly pointing at the
  merged node.
- Cross-document match, LLM denies same (two different "John Smith"s) ->
  **two** separate nodes, each keeping its own provenance.
- Same-document repeat mention (two parent chunks of one document both
  mentioning "Meridian Logistics") -> auto-confirmed, **one** merged node,
  `EntityResolver._confirm_via_llm` monkeypatched to raise if called at all
  (it wasn't).

All three cases passed. `docker compose down -v` afterward to tear down
the container brought up for this.

**Not verified this pass**: a real end-to-end two-document ingest through
the live API (verification plan item 3), which needs a real LLM call for
the confirmation step. OpenRouter's account-wide daily free-tier quota
(exhausted earlier today, see the few-shot/CoT entry below) had not yet
reset when this ran: reset time recorded as `X-RateLimit-Reset` =
2026-08-26 00:00 UTC; this ran at 2026-08-25 16:07 UTC, about 8 hours
before reset. Rather than burn a call confirming what was already known to
be exhausted, this was skipped and store-level verification (which needs
no LLM call) was relied on instead -- the same honest gap-flagging pattern
the few-shot/CoT pass used when it hit the same wall. A re-run after the
quota resets should confirm the real LLM confirmation call actually fires
and produces a connected cross-document graph, which is still unverified
against real model behavior (only its parsing/fallback logic and the
graph-write mechanics around it are).

Also updated `README.md` (trade-offs section: replaced the old
"per-document namespacing" gap with the new exact-match + LLM-confirmation
description and the fuzzy-matching non-goal; "What's still open" section
updated accordingly) and `IMPROVEMENTS.md` (checklist item marked done,
same caveats noted) to keep the written record in sync with what's
actually implemented and what's still genuinely open.

## Few-shot + CoT graph extraction -- 2026-08-25

Implements `docs/superpowers/specs/2026-08-25-fewshot-cot-extraction-design.md`.
`EXTRACTION_PROMPT` (`app/graph/extraction.py`) now asks for a `reasoning`
field first (chain-of-thought, embedded in the same JSON call, not a second
one) and includes two hand-written, in-domain few-shot examples: one rich
extraction, one deliberately weak-signal example demonstrating restraint
(no entities/relationships forced when the text doesn't support them). No
code changes to `_parse` -- it already ignores unrecognized JSON keys, so
`reasoning` needs no new handling; confirmed by re-running the existing
suite unchanged.

**Verified**: `pytest backend/tests/test_extraction_parsing.py` -- 18
passed, unchanged, confirming the `_parse` compatibility claim rather than
just asserting it.

**Not verified this pass**: actual output-quality improvement from the new
prompt against a real model. Brought up the live stack and attempted two
targeted ingests (a rich-signal test and a weak-signal restraint test) --
both got through chunking and embedding successfully (parent/child chunks
landed in Postgres, confirmed via SQL), but both failed at the extraction
LLM call with `openai.RateLimitError: 429`
(`limit_source: openrouter_free_tier_daily`, `X-RateLimit-Remaining: 0`) --
the same account-wide daily quota already exhausted by today's earlier
eval harness runs. Quota resets at `X-RateLimit-Reset` = 2026-08-26 00:00
UTC. This is an honest gap, not glossed over: the prompt change is
structurally sound (existing tests pass, pipeline mechanics work end to
end up to the LLM call) but its actual effect on extraction quality --
does the weak-signal example actually prevent hallucinated entities in
practice, does reasoning improve relationship accuracy -- is unverified
until the quota resets and this can be re-run.

## Tests, evals, LangSmith tracing, README -- 2026-08-25
Implements `docs/superpowers/specs/2026-08-25-tests-evals-tracing-docs-design.md`.
The last four pieces from `IMPROVEMENTS.md`'s "still outstanding" list, none
of which involved a real architectural decision.

**Tests** (`backend/tests/`, new `pytest==8.3.4` dependency): 53 unit tests
across three files, all pure/dependency-free (no docker, no API key):
`test_hierarchical_chunker.py` (parent/child counts and approximate sizing
on realistic repeated-paragraph text, valid `parent_id` back-references,
all five existing `ValueError` cases including the previously-untested
`chunk_overlap_tokens < 0` check), `test_extraction_parsing.py`
(`GraphExtractor._parse` -- well-formed JSON, malformed JSON,
missing/invalid `section_title`/`content_type`, and the existing
dangling-relationship-drop behavior, pinned rather than changed),
`test_pipeline_parsing.py` (the three static parsers added by the agentic
pipeline spec -- `_parse_validation`/`_parse_grade`/`_parse_rationale` --
same malformed/missing-field coverage style). Run:
```
cd backend && .venv/bin/python -m pytest tests/ -v
```
Result: **53 passed in 1.02s**.

**Eval harness** (`backend/evals/run_evals.py`, `backend/evals/results.md`):
a small hardcoded script (not a framework, not part of `pytest`, not
CI-integrated) -- 8 question/expected-fact pairs against the same Acme
Corp / Startup Inc acquisition document used for today's earlier real-model
verification (see below), run against the actual live docker-compose stack
with the real OpenRouter key from `.env`.

Two real runs happened, both worth recording honestly rather than only
keeping the clean one:
- **Run 1** (before a whitespace-normalization fix): 3/8 passed by strict
  substring match, but the failures were a harness bug, not wrong answers
  -- the model (`minimax/minimax-m2.7:free`) emits Unicode narrow no-break
  spaces (`U+202F`) between some words/numbers (e.g. `"Acme Corp"`,
  `"$50 million"`), which defeated plain `in` substring matching on
  otherwise-correct answers. Fixed by normalizing all whitespace (regex
  `\s+` -> single space) on both sides of the comparison before
  re-running.
- **Run 2** (after the fix): questions 1-2 passed cleanly (34.6s, 35.5s).
  Question 3 onward hit `openai.RateLimitError: 429`
  (`limit_source: openrouter_free_tier_daily`, `X-RateLimit-Remaining: 0`)
  -- OpenRouter's account-wide daily free-tier request cap, exhausted
  partway through the run. Confirmed this is account-wide, not
  model-specific: swapped `LLM_MODEL` to
  `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` (the other model
  this project previously verified as working) and immediately re-attempted
  fixture ingestion -- identical `free-models-per-day` error at the
  extraction call. `LLM_MODEL` was reverted back to
  `minimax/minimax-m2.7:free` afterward since the swap didn't help.
  Quota resets daily (`X-RateLimit-Reset` = 2026-08-26 00:00 UTC); a
  re-run after that reset should complete the full 8/8 this run was on
  track for -- questions 3, 4, 6, 7, 8 all had the correct fact present
  verbatim in run 1's raw answer text, just defeated by the same spacing
  bug run 1 hit, which was already fixed by the time run 2 executed them.

Full per-question tables for both runs are in `backend/evals/results.md`,
including a hand-added note on the rate-limit run (the script itself only
records pass/fail/latency, not the HTTP error text, so the note was added
by hand for anyone reading the file later).

**LangSmith tracing**: `LANGCHAIN_TRACING_V2`/`LANGCHAIN_API_KEY`/
`LANGCHAIN_PROJECT` added to `.env.example` and `docker-compose.yml`'s
`api` service `environment` block (same `${VAR:-}` pattern as
`OPENAI_BASE_URL`). No application code changes -- LangChain/LangGraph
auto-detect these from the environment. Verified via
`docker exec graphrag-api-1 env | grep LANGCHAIN` (all three land in the
container, even as blank placeholders) -- the same check that caught the
`OPENAI_BASE_URL` env-var bug earlier today, applied proactively this time
so it isn't repeated. **Not verified against a real LangSmith account** --
no LangSmith API key was available in this pass; stated explicitly rather
than claimed.

**README.md**: architecture (mermaid diagrams of the four services and both
pipelines), setup (`docker compose up`, `.env.example`, the verified
OpenRouter path), sample queries (the real ingest/chat/gibberish-rejection
example from `VERIFICATION.md`, copied verbatim rather than paraphrased),
trade-offs (pgvector vs. dedicated vector DB, bounded retry vs. ReAct,
per-parent vs. per-child classification, substring-match entity linking vs.
NER, per-document graph namespacing, no Alembic), and a "what's still open"
section (cross-document entity resolution, LangSmith unverified, eval
harness scope, ReAct, DSPy). Every concrete claim in it (service names,
ports, container name, the sample query/answer/rationale text, the 35.4s
latency figure, node/relationship counts) was checked against this repo's
actual current state, not written from memory.

### Deviation: `.env` also got the LangSmith placeholder vars, not just `.env.example`
The spec's file list only mentions `.env.example` (a committed template),
but `.env` itself (gitignored, already on disk with the working OpenRouter
config) needed the same three vars added too, or the
`docker exec ... env | grep LANGCHAIN` verification step would have
nothing to check against `docker-compose.yml`'s `${VAR:-}` substitution
pattern would still produce empty-string env vars in the container either
way, so this wasn't strictly required for the plumbing to work -- but
verifying it while blank matches exactly how the `OPENAI_BASE_URL` bug was
originally caught, so it was done for real rather than assumed.

## First real-model verification -- 2026-08-25

Every prior verification pass in this file used a fake OpenAI key, so only
structural correctness (imports, schema, graceful failure) was proven, never
actual model behavior. That changed today: `OPENAI_BASE_URL` support was
added (OpenAI's SDK against any OpenAI-compatible endpoint, no other code
changes needed) and pointed at OpenRouter with real free-tier models
(`minimax/minimax-m2.7:free` for chat, `openai/text-embedding-3-small` for
embeddings, which happens to return exactly 1536 dimensions -- matches the
existing schema, no migration needed).

**Bug found and fixed getting here**: `docker-compose.yml`'s `api` service
only passes through env vars explicitly listed in its `environment:` block
-- adding `OPENAI_BASE_URL` to `.env` alone did nothing until it was also
added there. First attempt's error (`openai.AuthenticationError`, with
OpenAI's own client-side error text) was the tell: the request never left
for OpenRouter at all.

**Real end-to-end result** (`docker compose up`, real ingest, real chat,
against a 3-paragraph test document):
- Ingest: 1 parent chunk, 2 child chunks, 8 entities, 9 relationships --
  all real, all correct (verified the extracted relationships' evidence
  text against the source document, e.g. `(Acme Corp)-[ACQUIRED]->
  (Startup Inc)` with evidence "Acme Corp acquired Startup Inc in March
  2024 for $50 million", an exact match).
- Chunker enrichment classified correctly: `section_title="Executive
  Summary"`, `content_type="prose"`.
- Chat query "Who acquired Startup Inc and how much did it cost?" ->
  correct answer ("Acme Corp acquired Startup Inc for $50 million."),
  correct rationale citing the specific parent chunks and the specific
  graph relationship actually used, `query_logs` row written with
  `rationale_text` populated, latency 35.4s (4 sequential LLM calls on a
  free-tier model -- real data point for the README's cost/latency
  trade-off discussion once that's written).
- Gibberish input ("asdkjf qwoeiru zzz blorp") -> correctly short-circuited
  before any retrieval (empty citations/triples), friendly clarification
  response, no `query_logs` row written (matches "log only completed
  turns").

This is the first genuine proof the whole system -- chunking, embedding,
graph extraction, chunker classification, query validation, retrieval,
context grading, generation, and rationale -- works correctly together,
not just in isolation with mocks.

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

## Chunker enrichment: section/content-type classification + retrieval filters -- verified 2026-08-25
Implements `docs/superpowers/specs/2026-08-25-chunker-enrichment-design.md`:
`GraphExtractor.extract()`'s existing per-parent LLM call now also returns
`section_title`/`content_type` (`prose`/`table`/`list`/`other`), folded into
the same JSON response rather than a second call. `ingestion.py` keeps its
existing order (`save_document -> chunk -> save_many -> embed+upsert ->
loop parents: extract -> write_graph`) unchanged and backfills the
classification via `ParentChunkStore.update_metadata` /
`PostgresVectorStore.update_children_metadata` (`UPDATE ... WHERE`, not part
of the initial `INSERT`) right after each parent's `extract()` call inside
the existing loop -- so an extraction failure still leaves chunks and
vectors durably saved, same partial-success semantics as before this spec.
`PostgresVectorStore.search()` gained optional `section_title`/
`content_type` JSONB-`astext` filters, threaded through `RagState` ->
`answer()`/`answer_stream()` -> `/chat`'s `ChatRequest.section_title_filter`
/`content_type_filter` -> the Streamlit sidebar (content-type selectbox +
section-title text input, chat tab only).

Verified (bypassing OpenAI where required, same fake-key constraint as every
prior pass):
- Every `app.*` module imports cleanly (`app.schemas.models`,
  `app.graph.extraction`, `app.services.parent_store`,
  `app.vectorstore.postgres_store`, `app.services.ingestion`,
  `app.rag.pipeline`, `app.api.routes.chat`, `app.db.models`, `app.main`).
- Direct unit tests of `GraphExtractor._parse` against synthetic JSON: 7
  cases (well-formed table/list/prose, missing fields entirely, an invalid
  `content_type` literal, an explicit `null` section_title, an empty-string
  section_title, mixed-case `content_type`, and totally malformed JSON) --
  all parse to the correct value or the documented defaults
  (`section_title=None`, `content_type="prose"`) without raising.
- Direct store-level test against a real local Postgres
  (`pgvector/pgvector:pg16`, docker), bypassing OpenAI entirely: saved 2
  parents + 4 children, called `update_metadata`/`update_children_metadata`
  with synthetic classification dicts, confirmed the exact JSONB landed via
  raw `psycopg` SQL (not the ORM), then called `search()` with
  `content_type`/`section_title` filters set individually, combined, mismatched
  (0 results), and omitted entirely (all 4 rows come back) -- every case
  returned exactly the expected child_id set, and `document_id` filtering
  still composes correctly alongside the new filters.
- `docker compose up -d --build` -> postgres, neo4j, api, ui all healthy.
- `POST /api/v1/ingest` with a fake key -> still fails at the *embedding*
  call (`ingestion.py:71`, `openai.AuthenticationError: 401`), same failure
  point as every prior pass -- confirmed via `docker compose logs api`
  (single traceback, at `embed_texts`, nothing past it). Confirmed via
  direct SQL that the one `parent_chunks` row that did land has
  `metadata = '{}'` (the column default) and `child_chunks` has 0 rows for
  that document -- proves the backfill code is correctly never reached on
  this path, exactly as the spec predicted.
- `GET /api/v1/graph/subgraph?query=...` -> 200, `{"nodes":[],"edges":[]}`,
  unaffected by this change.
- `GET /api/v1/health` -> 200 before and after the failed `/ingest` and
  `/chat` calls.
- `POST /api/v1/chat` with `section_title_filter`/`content_type_filter` set
  -> request validates (no 422), fails at `validate_query`'s LLM call with
  the same real 401, delivered as a single SSE `error` event -- proves the
  new fields are wired through `ChatRequest` -> `answer_stream()` without
  breaking the existing request path. A request with an invalid
  `content_type_filter` literal (e.g. `"spreadsheet"`) correctly 422s before
  the pipeline is ever invoked.
- `docker compose down -v` -> clean teardown, no leftover containers or
  volumes.

### Limitation: classification quality and the real ingest -> chat round trip are untested
Same class of gap as every prior pass's fake-key limitation, worth stating
plainly rather than letting the store-level pass stand in for it:
classification accuracy/quality (does the LLM actually pick a sensible
`section_title`, does `content_type` match the passage) is untested against
a real model, and the metadata backfill + filtered search above are only
verified at the store level with synthetic metadata -- not through a real
`/ingest` -> `/chat` round trip, which is blocked by the same fake-key
limitation as every prior pass. Pending a real OpenAI key.

## Not started yet
- Cross-document entity resolution (see "Known design trade-offs" below)
- LangSmith tracing verified against a real account (env vars wired and
  confirmed reaching the container; no LangSmith API key available in this
  pass)
- CI integration for the eval harness (currently a manual/documented step
  against a live stack, not run automatically)
- DSPy / hand-written few-shot for the extraction prompt (stretch goal)

## Known design trade-offs to call out in the README
- Graph node ids are namespaced per document
  (`f"{document_id}:{entity_name}"` in `extraction.py`), so there is no
  cross-document entity resolution yet -- each document gets its own
  subgraph rather than one merged knowledge graph. Acceptable for this
  scope; would need a real entity-resolution step to fix.
- Entity linking at query time (`find_node_ids_by_name_fragment`) is a
  cheap substring match over all node names, not real NER/linking --
  called out in the code as a take-home shortcut.
- `section_title`/`content_type` classification happens once per parent
  chunk (~1000 tokens) and is inherited by all of that parent's children,
  rather than classifying every child individually. Children outnumber
  parents roughly 5:1 in the chunker's default sizing, so per-child
  classification would be ~5x the LLM calls at ingestion time for marginal
  precision gain -- a deliberate cost/latency trade-off, not an oversight.
