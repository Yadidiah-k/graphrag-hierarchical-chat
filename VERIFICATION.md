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

## Summary (API-level tests)

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

---

# Frontend: real browser testing

Everything above was verified via `curl`/raw HTTP against the API
directly. That's necessary but not sufficient — a curl script doesn't
reproduce the frontend's own client-side logic (how it consumes the SSE
stream, what it does with each event, how widgets commit state). This
section covers driving the **actual Streamlit UI** in a real browser
(Playwright + headless system Chrome — no extra browser download needed,
the system already had `google-chrome`), clicking buttons and reading
rendered output, not calling the API directly.

Two real, reproducible bugs were found this way that no API-level test
had caught — see each test case below for exactly how.

## Test case 4: Frontend — full chat flow (ingest, citations, rationale, graph)

**Input**: pasted the Acme Corp/Startup Inc text into the sidebar's
"...or paste text" box, filled Document ID/Title, clicked **Ingest**;
then asked "Who acquired Startup Inc and how much did it cost?" via the
chat input.

**Checks performed**:
- Ingest form: Streamlit's `text_area` requires **Ctrl+Enter** (or a
  blur) to commit a pending edit — confirmed via screenshot showing a red
  border + "Ctrl+Enter to apply" hint; the Ingest button stays disabled
  until that happens. Real, minor UX rough edge (not a bug) — easy to
  trip over since nothing else on the page explains it.
- Ingest result rendered: "Ingested: 1 parent chunks, 1 child chunks, 3
  entities, 3 relationships."
- Chat answer rendered correctly via `st.write_stream`: "Acme Corp
  acquired Startup Inc for $50 million."
- Citations rendered as 3 expandable cards; expanding one showed the full
  correct parent-chunk text.
- **Bug found**: "Why this answer" (the rationale section) never
  rendered, for this or any query — `frontend/app.py`'s SSE consumer
  `break`d its read loop on the `"done"` event, but the backend sends
  `"rationale"` *after* `"done"` (it needs the finished answer text
  first). The frontend silently discarded every rationale the backend
  ever sent. No prior verification pass caught this because every one of
  them read the raw SSE stream directly (`curl`/`requests` without the
  frontend's early-exit logic) — only driving the actual frontend code
  path surfaces this class of bug.
  **Fixed**: removed the `break` (commit `7f734c5`). Reloaded in the
  browser afterward and confirmed "Why this answer" now renders with
  accurate content, correctly citing the actual supporting chunk ids and
  the actual graph relationship used.
- Pyvis graph rendered as a real interactive node/edge visualization
  inside its iframe (not an empty container) — confirmed both
  programmatically (`iframe` element present) and visually (circles,
  labeled edges, entity-id labels all visible).

**Status: PASS (after fix)**

## Test case 5: Frontend — gibberish rejection

**Input**: chat query `"asdkjf qwoeiru zzz blorp"`.

**Checks performed**: rendered response was the friendly clarification
message; the page explicitly showed "No citations retrieved for this
answer." / "No rationale available for this answer." / "Nothing to
show." for the graph section — a clean, legible empty state, not a blank
or broken-looking section.

**Status: PASS**

## Test case 6: Frontend — Graph Explorer tab

**Input**: switched to the "Graph Explorer" tab, entered "Acme Corp",
clicked **Explore**.

**Checks performed**: returned "3 nodes, 5 edges" and rendered the same
Pyvis visualization component, working independently of any chat turn.

**Status: PASS**

## Test case 7: Frontend — file upload + multi-turn conversation history

**Input**: uploaded a `.txt` file (Meridian Logistics/RouteWise Analytics
acquisition text) via the sidebar's file uploader — not the paste-text
box, a different code path. Then asked two chat turns in sequence: "Who
acquired RouteWise Analytics and how much did it cost?" (turn 1), then
"Who led that acquisition?" (turn 2, deliberately using a pronoun with no
antecedent unless conversation history is actually used).

**Checks performed**:
- File upload ingest succeeded: "Ingested: 1 parent chunks, 1 child
  chunks, 3 entities, 6 relationships."
- Turn 1 answered correctly: "Meridian Logistics acquired RouteWise
  Analytics for $22 million."
- Turn 2 correctly resolved "that acquisition" via `validate_query`'s
  history-aware query rewriting: "The acquisition was led by Priya Nair,
  Meridian Logistics' CEO (who finalized the deal and has headed the
  company since 2021)." — the first real-model confirmation that
  multi-turn conversation history and query rewriting work end-to-end
  through the actual UI, not just in isolated unit tests of the parsing
  logic.

**Status: PASS**

## Test case 8: Frontend — `document_id` filter

**Input**: set the sidebar's "Filter chat by document_id" field to a
nonexistent document id, then re-asked a question already answered in
test case 7's turn 1.

**Checks performed**: response correctly showed "No citations retrieved
for this answer." — the filter genuinely restricted vector search to zero
results (not silently ignored). The model still produced a correct
answer, and the rationale **honestly disclosed why**: "The answer was
generated from internal knowledge without referencing any retrieved
citations or graph relationships, as none were provided." This is
correct, disclosed behavior, not a bug — conversation history is a
legitimate generation input by design; the rationale mechanism correctly
flagged that this particular answer wasn't grounded in retrieval, rather
than claiming citations that didn't exist.

**Status: PASS**

## Test case 9: Frontend — `content_type` filter

**Input**: with the `document_id` filter reset to the correct document
(entirely prose, no tables), set "Content type filter" to `prose`, asked
a question — then repeated with the filter set to `table`.

**Checks performed**:
- `content_type=prose`: citations returned, question answered correctly
  ("RouteWise Analytics specializes in route-optimization software.").
- `content_type=table`: zero citations returned, correctly ("the
  available information does not provide details..." — the document has
  no table content, so nothing should match). Proves the filter
  genuinely restricts retrieval by content type.
- **Second bug found**, organically, while this test happened to hit
  OpenRouter's exhausted daily quota mid-run: the `table`-filtered
  answer text had a raw exception repr appended directly to it —
  `Error: Error code: 429 - {'error': {'message': 'Rate limit
  exceeded...` — the full Python dict, dumped into the chat bubble right
  after an already-correct, already-complete answer. Root cause:
  `chat.py`'s single outer `try/except` treated a failure in
  `generate_rationale` (called after the answer already streamed
  successfully) the same as any other failure, yielding a chat `"error"`
  event that the frontend renders inline. **Fixed** (commit `cbe7a9b`):
  `generate_rationale` + the `query_logs` write now have their own inner
  `try/except` — on failure, log and silently skip rather than surface a
  user-facing error, since the answer itself already succeeded by that
  point. `pytest backend/tests/` still 70/70 after the fix.

**Status: PASS (content_type filtering itself); bug found and fixed
(error handling)**

## Frontend summary

| # | Test case | Result |
|---|-----------|--------|
| 4 | Full chat flow: ingest, citations, rationale, graph render | PASS (after fixing the rationale-discard bug) |
| 5 | Gibberish rejection, clean empty state | PASS |
| 6 | Graph Explorer tab | PASS |
| 7 | File upload ingest + multi-turn history/query rewriting | PASS |
| 8 | `document_id` filter (correctly empty + honest rationale) | PASS |
| 9 | `content_type` filter (prose match, table correctly no-match) | PASS (after fixing the rationale-error-leak bug) |

Two real bugs found and fixed, both invisible to every prior
API-level/curl-based verification pass because they lived specifically in
the frontend's own SSE-consumption and error-rendering logic, not the
API's behavior.

**Not yet covered**: the bounded retry-on-insufficient-context loop
(`grade_context` grading something insufficient and actually retrying)
still hasn't fired against a real model — a query designed to trigger it
was queued but OpenRouter's daily quota exhausted before it ran (see
`PROGRESS.md`). Also not covered: LangSmith tracing (no account
available), and multi-candidate entity disambiguation (explicitly out of
scope, see `README.md`).
