# Improvements checklist

Working notes from a review of the built system against the assignment.
Current build is complete and verified end-to-end (see PROGRESS.md) but is a
literal, non-differentiated implementation of the spec. This tracks the gaps
and the discussed upgrades, ranked by impact vs. effort.

## Current state (for context)

- Stores: Qdrant (child chunk vectors) + SQLite (parent chunks) + Neo4j (graph)
- RAG pipeline: fixed LangGraph straight-line --
  `retrieve_vector -> expand_parents -> link_entities -> traverse_graph -> generate_answer`
  (no branching, no conditional edges, fixed `hop_depth`)
- Entity linking: literal substring match of retrieved text against all node names
- Graph nodes: namespaced per-document (`f"{document_id}:{entity_name}"`) -- no
  cross-document entity resolution
- Chunker: pure character-count recursive splitting, no structure awareness,
  no metadata beyond `{document_id, parent_id, order, start_char, end_char}`
- No evals, no tracing/observability, no tests yet

## Priority 1 -- biggest grading impact, fixes the "why LangGraph" gap

- [ ] **Query triage node** before retrieval: cheap LLM (or smaller/faster
      model) call classifying the incoming query --
      `is_gibberish`, `is_relevant` (or defer to post-retrieval confidence),
      `tone`/`sentiment`, `verbosity_preference` (wants a short answer vs. a
      thorough one).
  - [ ] Conditional edge: gibberish -> short-circuit with a clarification
        response, skip retrieval entirely (saves cost + latency)
  - [ ] Conditional edge: low vector-search confidence -> answer with an
        explicit "not found in the provided documents" flag instead of
        hallucinating
  - [ ] Feed tone/verbosity into the generation prompt to adjust response
        style/length
- [ ] **ReAct-style agentic retrieval**: expose `vector_search` and
      `graph_traverse` as tools the LLM can call in a loop (LangGraph's native
      pattern), letting it decide whether to search again, traverse deeper, or
      stop -- instead of a fixed `hop_depth`. This is the fuller version of
      "prefer agentic framework: LangGraph for multi-hop" from the assignment.

## Priority 2 -- architecture quality, one clean unit of work

- [ ] **Migrate Qdrant + SQLite -> Postgres (pgvector + JSONB)**
  - [ ] Single Postgres instance: child chunk vectors via pgvector, parent
        chunks + flexible metadata via JSONB, one schema instead of three
        stores
  - [ ] Drop Qdrant + SQLite from docker-compose, update `app/vectorstore/`
        and `app/services/parent_store.py` accordingly
  - [ ] Document the trade-off explicitly in the README (pgvector vs. a
        dedicated vector DB -- fine at this scale, would reconsider at very
        large corpora)
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
