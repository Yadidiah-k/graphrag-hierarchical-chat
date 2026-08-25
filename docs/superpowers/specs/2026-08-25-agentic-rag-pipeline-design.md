# Agentic RAG pipeline design (query validation, grading, rationale)

Status: approved, ready for implementation
Date: 2026-08-25

## Context

The current `GraphRagPipeline` (`backend/app/rag/pipeline.py`) is a fixed
LangGraph straight-line: `retrieve_vector -> expand_parents -> link_entities
-> traverse_graph -> generate_answer`. No branching, no conditional edges,
fixed `hop_depth`/`top_k`. This spec replaces it with a graph that actually
uses LangGraph's conditional-edge/cycle support, per discussion in
`IMPROVEMENTS.md` Priority 1.

This spec assumes the Postgres migration
(`2026-08-25-postgres-migration-design.md`) is already implemented and
committed -- this work touches `chat.py`, `main.py`, and `schemas/models.py`,
which that migration also touches, so it must land first to avoid stepping
on concurrent edits.

## Goals

- Query validation node: detect gibberish, infer tone and a verbosity
  preference, rewrite the query using conversation history.
- Grade retrieved context for sufficiency before generating; bounded retry
  with widened retrieval parameters if insufficient.
- Rationale node: after the answer streams, explain which chunks/
  relationships were actually used.
- Conversation history support in `/chat` (currently stateless).
- Structured (Pydantic-validated) output for the classification/grading
  nodes only. Final answer generation stays free-text and continues to
  stream token-by-token over SSE exactly as it does today -- this is a hard
  constraint, not a nice-to-have: the frontend's `st.write_stream` live-typing
  UX must keep working unchanged.

## Non-goals

- No ReAct-style tool-calling loop (LLM deciding which retrieval action to
  take). The bounded retry-with-widened-parameters approach is the chosen
  design -- more predictable cost/latency, discussed and explicitly
  preferred over ReAct for this round.
- No post-generation faithfulness/hallucination check.
- No moderation/safety node.
- No `query_logs` schema changes beyond populating the `rationale_text`
  column that the Postgres spec already added in anticipation of this work.
  Rejected/gibberish queries do not write a `query_logs` row (matches the
  existing "log only completed turns" scope).

## Graph design

```
validate_query (structured)
   │
   ├─ is_gibberish=true ──► reject_response ──► END
   │
   └─ valid, using rewritten_query
       ▼
retrieve_vector → expand_parents → link_entities → traverse_graph
       ▼
  grade_context (structured)
       │
       ├─ is_sufficient=false AND retry_count < max_context_retries
       │     → widen top_k (x retry_top_k_multiplier), hop_depth
       │       (+ retry_hop_depth_increment), retry_count += 1,
       │       loop back to retrieve_vector
       │
       └─ is_sufficient=true, or retries exhausted
             ▼
       generate_answer (free text, streamed)
             ▼
       generate_rationale (structured, called AFTER the stream is
                            consumed -- see "Streaming vs. compiled graph"
                            below, not part of the token-streaming path)
```

### Node specs

**`validate_query`** -- one LLM call, `response_format={"type": "json_object"}`
+ manual Pydantic parse (same pattern as `graph/extraction.py`, not the
newer strict `json_schema` mode -- stay consistent with the existing
codebase convention). Takes `query` + `history` (last few turns). Output:

```python
class QueryValidation(BaseModel):
    is_gibberish: bool
    tone: str
    verbosity_preference: Literal["brief", "detailed"]
    rewritten_query: str
    suggested_reply: str | None = None  # populated only if is_gibberish
```

`rewritten_query` resolves references using history (e.g. "what about their
2023 revenue?" -> "What was Acme Corp's 2023 revenue?") and is what actually
gets embedded/searched, not the raw `query`.

**`grade_context`** -- one LLM call, same structured-output pattern, given
the assembled parent text + graph triples:

```python
class ContextGrade(BaseModel):
    is_sufficient: bool
    relevance_score: float  # 0.0-1.0
    missing_info: str | None = None
```

**`generate_answer`** -- free text, streamed. Prompt incorporates
`tone`/`verbosity_preference` as a style instruction, conversation `history`
for continuity, and -- if retries were exhausted while still insufficient --
an explicit instruction to hedge rather than answer confidently from
incomplete context.

**`generate_rationale`** -- one LLM call, same structured-output pattern,
given the final answer text + citations + triples:

```python
class Rationale(BaseModel):
    explanation: str
    chunks_used: list[str] = []          # parent_ids actually referenced
    relationships_used: list[str] = []   # triple descriptions actually referenced
```

**`reject_response`** -- no LLM call. Just surfaces `suggested_reply` from
`validate_query`'s output.

### Streaming vs. compiled graph (important implementation detail)

The existing code already has two execution paths and this design keeps
that split rather than introducing a third pattern:
- `answer()` (non-streaming): uses the fully compiled LangGraph (`self._app`,
  built with `add_conditional_edges` for both branches above, including the
  retry cycle back to `retrieve_vector`) via `.invoke()`. This is the "real"
  LangGraph graph with genuine conditional/cyclic edges.
- `answer_stream()` (streaming, what `/chat` actually calls): continues the
  existing pattern of manually chaining node functions in Python (as it
  already does for `retrieve_vector`/`expand_parents`/`link_entities`/
  `traverse_graph`) up through `grade_context`, with the bounded retry
  implemented as a plain Python loop (not the graph engine's cycle
  machinery -- there's no streaming benefit to using the compiled graph
  here, and the current code already deviates from `.invoke()` for this
  entry point). It returns the token generator for `generate_answer` same
  as today. **`generate_rationale` is NOT part of this generator** -- it
  needs the full answer text as input, which only exists after the stream
  is consumed. Expose it as a separate method:

```python
def generate_rationale(
    self, query: str, answer: str, citations: list[CitationChunk], triples: list[CitationTriple]
) -> Rationale: ...
```

`chat.py` calls this after collecting all streamed tokens, then emits it as
a new `rationale` SSE event, then writes the `query_logs` row (with
`rationale_text` now populated).

Replace `answer_stream`'s current 4-tuple return with a small dataclass so
`chat.py` can branch on `is_gibberish` cleanly and has what it needs for the
rationale call and the reject path:

```python
@dataclass
class StreamResult:
    token_iter: Iterator[str]
    citations: list[CitationChunk]
    triples: list[CitationTriple]
    linked_node_ids: list[str]
    is_gibberish: bool
    rewritten_query: str
```

When `is_gibberish` is true, `token_iter` yields the single `suggested_reply`
string and `citations`/`triples`/`linked_node_ids` are empty lists.
`chat.py` skips `generate_rationale` and the `query_logs` write when
`is_gibberish` is true.

## Schema / interface changes

`backend/app/schemas/models.py`:
```python
class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str

class ChatRequest(BaseModel):
    query: str
    document_id: str | None = None
    top_k: int | None = None
    history: list[ChatMessage] = Field(default_factory=list)
```
Add `rationale` to `ChatEventType`. `QueryValidation`, `ContextGrade`,
`Rationale` also live in this file (mirrors where `ExtractionResult` etc.
already live).

`backend/app/core/config.py` -- add:
```python
max_context_retries: int = Field(default=1)
retry_top_k_multiplier: float = Field(default=1.5)
retry_hop_depth_increment: int = Field(default=1)
```

`frontend/app.py` -- send `st.session_state.messages` as `history` in the
chat request payload; render the new `rationale` SSE event's text near the
citations/graph view.

## Files touched

Modified: `backend/app/rag/pipeline.py` (rewrite), `backend/app/schemas/models.py`,
`backend/app/core/config.py`, `backend/app/api/routes/chat.py`,
`frontend/app.py`.

No new files, no files deleted.

## Verification plan

Same fake-`OPENAI_API_KEY` constraint as before, but it bites harder here:
`validate_query` itself needs a real LLM call, so with a fake key **nothing
past request-validation can be behaviorally verified** -- unlike the
Postgres migration, there's no partial-success signal to check against
real classification/grading behavior. Be explicit about this limitation
rather than overclaiming. What can actually be verified:

1. Every `app.*` module imports cleanly; the compiled graph
   (`GraphRagPipeline.__init__`) constructs without error (proves the
   `add_conditional_edges` wiring is valid LangGraph, independent of any
   LLM call).
2. Direct unit tests of `QueryValidation`/`ContextGrade`/`Rationale` Pydantic
   parsing against synthetic JSON strings shaped like what the LLM would
   return (including malformed/missing-field cases) -- bypasses the OpenAI
   call entirely, same technique already used to validate
   `graph/extraction.py`'s `_parse`.
3. `docker compose up` -- full stack still boots and stays healthy.
4. `POST /api/v1/chat` with the fake key -> fails gracefully at
   `validate_query` (the first LLM call), returns a proper `error` SSE
   event, does not crash the server or leave the connection hanging.
5. Explicitly note in `PROGRESS.md`: gibberish detection, tone/verbosity
   inference, context grading/retry behavior, and rationale quality are all
   **untested against a real model** pending a real OpenAI key.

## Commit plan

1. Schema changes (`ChatMessage`, `ChatRequest.history`, new structured
   models, `ChatEventType.rationale`) + config additions
2. `rag/pipeline.py` rewrite (compiled graph + `answer()`)
3. `rag/pipeline.py` streaming path (`answer_stream`, `StreamResult`,
   `generate_rationale`)
4. `chat.py` wiring (reject path, rationale event, history passthrough,
   query_logs rationale_text)
5. `frontend/app.py` (history send, rationale render)
6. `PROGRESS.md` update with verification results and the untested-behavior
   caveat

No Claude co-author trailer on any commit.
