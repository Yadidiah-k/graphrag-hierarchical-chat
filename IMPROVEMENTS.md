# Improvements checklist

Working notes from a review of the built system against the assignment.
Priority 1 (agentic pipeline), Priority 2's storage migration and chunker
enrichment, and a first real-model end-to-end verification are all now done.
This tracks what's left, ranked by impact vs. effort.

## Milestone: real-model verification, 2026-08-25

Every verification pass up to this point used a fake OpenAI key, so only
structural correctness was proven, never actual model behavior. That changed
today: `OPENAI_BASE_URL` support was added (any OpenAI-compatible endpoint,
not just OpenAI itself -- no other code changes needed) and pointed at
OpenRouter with real free-tier models. Full real run: correct chunking,
correct embeddings, correct entity/relationship extraction (evidence text
checked against the source document), correct section/content-type
classification, a correctly-answered chat query with an accurate rationale,
and correct gibberish rejection (retrieval genuinely skipped, not just a
rejected-looking answer). See `VERIFICATION.md` for the full reviewable
test-case writeup and `PROGRESS.md` for the narrative version. This resolves
the "not yet tested against a real model" caveat that applied to every item
below marked done -- noted per-item now instead of as a blanket caveat.

Bug found and fixed getting here: `docker-compose.yml`'s `api` service only
forwards env vars it explicitly lists -- adding `OPENAI_BASE_URL` to `.env`
alone did nothing until it was also added there.

## Current state (for context, updated 2026-08-25)

- Stores: Postgres (pgvector for child chunk vectors, JSONB for parent chunk
  + query_log metadata) + Neo4j (graph) -- Qdrant and SQLite are gone
- RAG pipeline: agentic LangGraph with real conditional/cyclic edges --
  `validate_query` (gibberish short-circuit, history-aware query rewrite) ->
  `retrieve_vector -> expand_parents -> link_entities -> traverse_graph` ->
  `grade_context` (bounded widen-and-retry loop back to `retrieve_vector` on
  insufficient context) -> `generate_answer` -> `generate_rationale` --
  real-model verified (gibberish rejection + a grounded answer + rationale)
- Chunker: parent chunks get LLM-classified `section_title`/`content_type`
  (folded into the existing extraction call), inherited by children,
  queryable as `/chat` filters -- real-model verified
- Entity linking: still a literal substring match of retrieved text against
  all node names -- unchanged, not addressed by any priority yet
- Graph nodes: still namespaced per-document -- cross-document entity
  resolution unchanged, see below
- `query_logs` audit table exists and is written on every completed `/chat`
  call with real content now confirmed (query, citations, rationale,
  latency) -- no eval harness or tracing consumes it yet (in progress)
- Tests, eval harness, LangSmith tracing, and README are in progress (see
  `docs/superpowers/specs/2026-08-25-tests-evals-tracing-docs-design.md`)

## Priority 1 -- biggest grading impact, fixes the "why LangGraph" gap

- [x] **Query triage node** before retrieval -- `validate_query` node
      (structured LLM call): `is_gibberish`, `tone`, `verbosity_preference`,
      plus a history-aware `rewritten_query` (query rewriting was folded in
      here, as discussed) and `suggested_reply` for the reject path.
      Implemented in `docs/superpowers/specs/2026-08-25-agentic-rag-pipeline-design.md`.
  - [x] Conditional edge: gibberish -> `reject_response` node, skips
        retrieval entirely (real `add_conditional_edges`, verified via the
        compiled graph's mermaid output showing genuine branching, not a
        straight line; **real-model verified** -- `"asdkjf qwoeiru zzz
        blorp"` correctly produced empty citations/triples and a friendly
        clarification response, no `query_logs` row written)
  - [x] Conditional edge: insufficient context -> handled via `grade_context`
        (below) rather than a separate pre-retrieval relevance check -- more
        reliable since it grades against what was *actually* retrieved
        instead of guessing relevance from the raw query
  - [x] Feed tone/verbosity into the generation prompt (`_style_instruction`)
  - [x] **Bonus, discussed alongside this**: conversation history support
        (`ChatRequest.history`) -- `/chat` was previously stateless
  - [x] **Bonus**: `grade_context` node + bounded widen-and-retry loop
        (`top_k` x1.5, `hop_depth` +1, capped at `max_context_retries`) when
        retrieved context is graded insufficient -- this is the "low
        confidence" handling called out above, implemented as its own node.
        Grading itself ran for real (correctly judged the small test
        document sufficient on the first pass), but the retry *branch*
        specifically wasn't exercised -- the test document never triggered
        it. Still open to verify with a query that's genuinely
        under-supported by the ingested content.
  - [x] **Bonus**: `generate_rationale` node -- explains which chunks/
        relationships were actually used, populates `query_logs.rationale_text`.
        **Real-model verified**: asked "Who acquired Startup Inc and how
        much did it cost?", got the correct answer plus a rationale that
        correctly cited the actual supporting chunk ids and the actual
        graph relationship used -- not a generic or fabricated explanation.
- [ ] **ReAct-style agentic retrieval**: expose `vector_search` and
      `graph_traverse` as tools the LLM can call in a loop, letting it decide
      whether to search again or stop -- **deliberately not chosen** in favor
      of the bounded widen-and-retry loop above (predictable cost/latency,
      explicitly discussed and traded off in the pipeline spec's Non-goals).
      Still open if you want the fuller agentic version.

## Priority 2 -- architecture quality, one clean unit of work

- [x] **Migrate Qdrant + SQLite -> Postgres (pgvector + JSONB)** --
      implemented in `docs/superpowers/specs/2026-08-25-postgres-migration-design.md`
  - [x] Single Postgres instance: `child_chunks` (pgvector, HNSW index),
        `parent_chunks` + `documents` (JSONB metadata column, GIN indexed),
        plus a new `query_logs` audit table that wasn't in the original scope
        but fell out naturally from having one real database now
  - [x] Dropped Qdrant + SQLite entirely from docker-compose and code
  - [ ] Document the trade-off in the README -- README doesn't exist yet
        (see "Still outstanding" below), so this is written up in the spec
        doc and `PROGRESS.md` for now, not yet in a README
- [x] **Chunker enrichment** (structure-aware, more metadata/filters) --
      implemented in `docs/superpowers/specs/2026-08-25-chunker-enrichment-design.md`.
      `hierarchical_chunker.py` itself stays unchanged (pure, dependency-free);
      classification folded into the existing per-parent extraction call.
  - [x] Section/heading detection -- **real-model verified**: the test
        document's Executive Summary section was correctly classified as
        `section_title="Executive Summary"`
  - [x] Content-type tagging -- **real-model verified**: correctly
        classified as `content_type="prose"`
  - [x] Stored in the Postgres JSONB `metadata` column (per-parent,
        inherited by children -- classifying every child individually was a
        deliberate cost trade-off, not done)
  - [x] Retrieval filter: `section_title_filter`/`content_type_filter` on
        `ChatRequest`, threaded through to `search()`'s `WHERE` clause,
        frontend sidebar controls added. Wired and store-level tested; not
        yet exercised with a real filtered `/chat` call in manual
        verification (VERIFICATION.md's real run didn't set a filter)

## Priority 3 -- cheap, high signal

- [ ] **LangSmith tracing** -- *in progress*: env var plumbing
      (`LANGCHAIN_TRACING_V2`/`LANGCHAIN_API_KEY`/`LANGCHAIN_PROJECT`) into
      `.env.example` and `docker-compose.yml` is underway. LangChain/LangGraph
      auto-detect these directly, no application code changes needed. Cannot
      be verified against a real LangSmith account -- none available in this
      environment; will be documented as wired-but-unverified.
- [ ] **Eval harness** -- *in progress*: scoped down from "10-20 Q/A pairs +
      LLM-judge faithfulness" to a small (5-8 question) real, runnable script
      against the fixture document from VERIFICATION.md, using substring/
      keyword checks rather than an LLM-judge (more machinery than 5-8 fixed
      questions need). Retrieval hit-rate and per-stage latency breakdown
      were not carried into the scoped-down version -- worth adding back if
      there's time, not blocking.

## Priority 4 -- stretch goals

- [ ] **Hand-written few-shot + explicit CoT** in the graph extraction
      prompt (`app/graph/extraction.py`) -- gets most of the reliability
      win of DSPy without the extra dependency
- [ ] **DSPy** (`BootstrapFewShot` or similar) for prompt optimization --
      legit and resume-relevant, but it's a second orchestration framework
      with its own compile step that needs labeled examples to bootstrap
      against. Risk: overlaps with the ReAct rewrite above (two frameworks
      doing an adjacent job), highest effort/risk-of-half-finished item on
      this list. Only worth it if there's time left after Priority 1-3, or
      if having DSPy specifically on the resume matters more than the
      marginal quality gain.

## Cross-document entity resolution (flagged earlier, not yet prioritized)

- [ ] Graph node ids are currently namespaced per document, so "Acme Corp"
      in doc A and doc B become two separate nodes -- the knowledge graph
      never actually connects across documents, which undercuts the core
      GraphRAG pitch. Needs a real entity-resolution step (normalize name +
      type, maybe embedding-similarity dedup) before this is fixed.

## Still outstanding from the base build (see PROGRESS.md)

- [ ] `backend/tests/` -- *in progress*, scoped to pure-logic unit tests
      (chunker, extraction parsing, the pipeline's structured-output
      parsing) that need zero external services, per the same scoping
      decision made earlier in this project
- [ ] `README.md` (architecture diagram, setup, sample queries, trade-offs)
      -- *in progress*, being written from content already established in
      `PROGRESS.md`/this file/the prior spec docs, using the real verified
      example from `VERIFICATION.md` for sample queries
- [x] Real end-to-end ingest -> chat test with a valid (OpenRouter) key --
      **done, see the Milestone section above and `VERIFICATION.md`**.
      Proved actual answer quality and rationale accuracy, not just plumbing.
