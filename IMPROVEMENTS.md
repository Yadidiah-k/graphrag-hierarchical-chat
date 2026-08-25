# Improvements checklist

Working notes from a review of the built system against the assignment.
Priority 1's query triage/grading and Priority 2's storage migration are now
implemented and verified (structurally -- see the real-model caveat in each
section below). This tracks what's left, ranked by impact vs. effort.

## Current state (for context, updated 2026-08-25)

- Stores: Postgres (pgvector for child chunk vectors, JSONB for parent chunk
  + query_log metadata) + Neo4j (graph) -- Qdrant and SQLite are gone
- RAG pipeline: agentic LangGraph with real conditional/cyclic edges --
  `validate_query` (gibberish short-circuit, history-aware query rewrite) ->
  `retrieve_vector -> expand_parents -> link_entities -> traverse_graph` ->
  `grade_context` (bounded widen-and-retry loop back to `retrieve_vector` on
  insufficient context) -> `generate_answer` -> `generate_rationale`
- Entity linking: still a literal substring match of retrieved text against
  all node names -- unchanged, not addressed by either priority yet
- Graph nodes: still namespaced per-document -- cross-document entity
  resolution unchanged, see below
- Chunker: still pure character-count recursive splitting, no structure
  awareness, no metadata beyond `{document_id, parent_id, order, start_char,
  end_char}` -- Priority 2's chunker-enrichment sub-item not started
- `query_logs` audit table exists and is written on every completed `/chat`
  call, but no eval harness or tracing consumes it yet
- Still no tests, no README
- **Caveat that applies to everything below marked done**: all verification
  so far used a fake OpenAI key. Structural correctness (graph wiring, schema,
  parsing of malformed LLM output, docker boot) is real-verified; actual model
  behavior (does it correctly detect gibberish, does grading actually improve
  answers, is the rationale accurate) is not yet tested against a real model.

## Priority 1 -- biggest grading impact, fixes the "why LangGraph" gap

- [x] **Query triage node** before retrieval -- `validate_query` node
      (structured LLM call): `is_gibberish`, `tone`, `verbosity_preference`,
      plus a history-aware `rewritten_query` (query rewriting was folded in
      here, as discussed) and `suggested_reply` for the reject path.
      Implemented in `docs/superpowers/specs/2026-08-25-agentic-rag-pipeline-design.md`.
  - [x] Conditional edge: gibberish -> `reject_response` node, skips
        retrieval entirely (real `add_conditional_edges`, verified via the
        compiled graph's mermaid output showing genuine branching, not a
        straight line)
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
        confidence" handling called out above, implemented as its own node
  - [x] **Bonus**: `generate_rationale` node -- explains which chunks/
        relationships were actually used, populates `query_logs.rationale_text`
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
- [ ] **Chunker enrichment** (structure-aware, more metadata/filters)
  - [ ] Section/heading detection during chunking, carry `section_title`
        forward as chunk metadata
  - [ ] Content-type tagging (table vs. list vs. prose) where detectable
  - [ ] Store this metadata in the new Postgres JSONB column
  - [ ] Use it as a retrieval filter (filter by section/type, not just
        `document_id`)

## Priority 3 -- cheap, high signal

- [ ] **LangSmith tracing**: near-free given LangChain/LangGraph is already
      in use -- add `LANGCHAIN_TRACING_V2`, `LANGCHAIN_API_KEY`,
      `LANGCHAIN_PROJECT` to Settings + `.env.example`, document in README
- [ ] **Eval harness**: small hand-built set (10-20 Q/A pairs against a
      sample document)
  - [ ] Retrieval hit-rate (did the right parent chunk get retrieved?)
  - [ ] Answer faithfulness via LLM-judge (is the answer grounded in the
        retrieved context?)
  - [ ] Latency per stage (retrieval / graph traversal / generation)

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

- [ ] `backend/tests/`
- [ ] `README.md` (architecture diagram, setup, sample queries, trade-offs)
- [ ] Real end-to-end ingest -> chat test with a valid OpenAI key (only
      tested with a fake key so far, which proved the plumbing but not
      answer quality)
