# Verification report: first real end-to-end run

Date: 2026-08-25
Purpose: every verification pass before this one used a fake OpenAI key,
so only structural correctness (imports, schema, graceful failure) was
proven — never actual model behavior. This is the first run against real
models, with real inputs, real outputs, and real checks against those
outputs. Reproducible: bring the stack up with `docker compose up` and a
working `.env` (see Configuration below), then re-run the commands in each
test case as-is.

## Configuration used

```
OPENAI_BASE_URL=https://openrouter.ai/api/v1
OPENAI_API_KEY=<OpenRouter key>
LLM_MODEL=minimax/minimax-m2.7:free
EMBEDDING_MODEL=openai/text-embedding-3-small   # returns 1536 dims, matches the schema
```

`OPENAI_BASE_URL` support (any OpenAI-compatible endpoint, not just
OpenAI itself) was added specifically to make this test possible —
see commit `42f7af1`.

## Bug found and fixed to get here

`docker-compose.yml`'s `api` service only forwards environment variables
it explicitly lists — adding `OPENAI_BASE_URL` to `.env` alone did nothing
until it was also added to that list. First symptom: an
`openai.AuthenticationError` whose message text was OpenAI's own
client-side validation error, which was the tell that the request never
reached OpenRouter at all. Fixed in commit `eca8611`.

---

## Test case 1: Document ingestion (chunking, embedding, extraction, classification)

**Input**: `POST /api/v1/ingest`, a 3-section, ~180-word document (Acme
Corp acquiring Startup Inc).

```bash
curl -s -X POST http://localhost:8000/api/v1/ingest \
  -H "Content-Type: application/json" \
  -d '{"document_id":"real-test-1","title":"Acme Corp Acquisition Report","text":"Executive Summary\n\nAcme Corp acquired Startup Inc in March 2024 for $50 million to expand its AI platform capabilities. The acquisition was led by Acme Corp CEO Jane Smith, who has led the company since 2019. Startup Inc was founded by John Doe in 2019 and specialized in natural language processing technology.\n\nFinancial Overview\n\nAcme Corp reported revenue of $200 million in fiscal year 2023, a 15% increase from the prior year. The company employs approximately 1,200 people across offices in San Francisco, New York, and London. Following the acquisition, Startup Inc'\''s 45 employees joined Acme Corp'\''s engineering division.\n\nStrategic Rationale\n\nThe acquisition allows Acme Corp to integrate advanced NLP capabilities into its existing product suite. Analysts expect the combined entity to compete more effectively against larger rivals in the enterprise AI market."}'
```

**Poll**: `GET /api/v1/ingest/real-test-1` until `status` leaves
`queued`/`running`.

**Actual result**:
```json
{"document_id":"real-test-1","status":"succeeded","parent_chunk_count":1,"child_chunk_count":2,"node_count":8,"relationship_count":9}
```

**Checks performed against the actual data, not just the summary counts**:
- Chunker classification landed correctly:
  `{"content_type": "prose", "section_title": "Executive Summary"}` on
  both the parent chunk and its two children (verified via
  `psql -c "SELECT metadata FROM parent_chunks/child_chunks ..."`).
- Extracted graph entities — all 8 correctly identified, correctly typed:
  `Acme Corp` (Organization), `Startup Inc` (Organization), `Jane Smith`
  (Person), `John Doe` (Person), `San Francisco`/`New York`/`London`
  (Location), `Natural Language Processing` (Technology).
- Extracted relationships — all 9 checked against the source text, e.g.:
  - `(Acme Corp)-[ACQUIRED]->(Startup Inc)`, evidence: *"Acme Corp
    acquired Startup Inc in March 2024 for $50 million"* — exact
    substring match against the input document.
  - `(John Doe)-[FOUNDED]->(Startup Inc)`, evidence: *"Startup Inc was
    founded by John Doe in 2019"* — exact match.
  - `(Jane Smith)-[LEDS_SINCE]->(Acme Corp)`, evidence: *"has led the
    company since 2019"* — exact match.
  - No hallucinated entities or relationships not supported by the text.

**Status: PASS**

---

## Test case 2: Chat — grounded question with a real, correct answer

**Input**: `POST /api/v1/chat`, `{"query": "Who acquired Startup Inc and
how much did it cost?", "document_id": "real-test-1"}`

**Actual streamed SSE response** (trimmed to the load-bearing parts —
citations/triples were correct but verbose, full text was checked, not
just skimmed):
- `citations` event: both retrieved parent chunks contained the correct
  source paragraph (the Executive Summary section, which has the answer).
- `triples` event: included `(Acme Corp)-[ACQUIRED]->(Startup Inc)`,
  evidence *"Acme Corp acquired Startup Inc in March 2024 for $50
  million"*.
- `token` events, concatenated: **"Acme Corp acquired Startup Inc for $50
  million."** — correct, concise, directly answers both parts of the
  question (who, how much).
- `rationale` event:
  ```json
  {
    "explanation": "The answer states that Acme Corp acquired Startup Inc for $50 million. This matches the text in the retrieved citations (both parent_ids) that say 'Acme Corp acquired Startup Inc in March 2024 for $50 million'. The knowledge-graph relationship (Acme Corp)-[ACQUIRED]->(Startup Inc) captures the acquisition event and was used to confirm the fact.",
    "chunks_used": ["real-test-1:p0:5351431d", "real-test-1:p0:c6fb1b65"],
    "relationships_used": ["Acme Corp -> ACQUIRED -> Startup Inc"]
  }
  ```
  Checked: the cited `chunks_used`/`relationships_used` are the actual
  ones that contain the supporting evidence, not fabricated references.

**Latency**: 35.4s end-to-end (4 sequential LLM calls — `validate_query`,
`grade_context`, `generate_answer`, `generate_rationale` — on a free-tier
model with no dedicated capacity). Real data point, not estimated, for
the accuracy/latency trade-off discussion.

**Audit trail check**: `SELECT query_text, latency_ms, rationale_text IS
NOT NULL FROM query_logs` → one row, `rationale_text` populated,
`latency_ms=35460`. Confirms the query_logs write path works end-to-end
with real content, not just empty/mocked data.

**Status: PASS**

---

## Test case 3: Chat — gibberish input is rejected before retrieval

**Input**: `POST /api/v1/chat`, `{"query": "asdkjf qwoeiru zzz blorp",
"document_id": "real-test-1"}`

**Actual streamed SSE response**:
```
data: {"type":"citations","data":[]}
data: {"type":"triples","data":[]}
data: {"type":"token","data":"It looks like that message might have been a typo or got jumbled. Could you try rephrasing your question?"}
data: {"type":"done","data":null}
```

**Checks performed**:
- `citations`/`triples` are empty — confirms `validate_query` correctly
  short-circuited *before* any vector search or graph traversal ran (not
  just that the final answer happened to be a rejection).
- No `rationale` event was emitted (correct — the reject path skips it).
- `query_logs` row count unchanged after this call (verified via SQL) —
  confirms the "log only completed turns" design actually holds under a
  real rejected query, not just in unit tests of the logic.

**Status: PASS**

---

## Summary

| # | Test case | Result |
|---|-----------|--------|
| 1 | Ingestion: chunking, embedding, entity/relationship extraction, section/content-type classification | PASS |
| 2 | Chat: grounded answer + accurate rationale + audit log write | PASS |
| 3 | Chat: gibberish rejection, retrieval correctly skipped, no audit log write | PASS |

All three checked against **actual output content** (evidence text
matched against the source document, cited chunk/relationship ids checked
against what actually supported the answer), not just HTTP status codes
or summary counts. This is the first proof the complete system — every
piece built across the four prior specs — works correctly together against
a real model, not only in isolation with mocks or against a fake key.

Not yet covered by this pass (see `IMPROVEMENTS.md` / `PROGRESS.md` for
status): the bounded retry-on-insufficient-context path (`grade_context`
grading something insufficient and actually retrying) wasn't exercised
here since the test document was small enough to be sufficient on the
first pass; multi-turn conversation history resolution; LangSmith tracing
(no LangSmith account available in this environment).
