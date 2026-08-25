# Tests, evals, LangSmith tracing, README

Status: approved, ready for implementation
Date: 2026-08-25

## Context

Everything else in `IMPROVEMENTS.md`/`PROGRESS.md`'s "still outstanding"
list. Four pieces, bundled into one spec since none of them involve real
architectural decisions (unlike the three prior specs) -- they're
finishing/polish work using context already established across
`PROGRESS.md`, `IMPROVEMENTS.md`, and the three prior spec docs. Read all
of those before starting; most of what the README needs (trade-offs,
architecture) is already written down somewhere in this repo and needs
consolidating, not inventing.

**Critically**: `OPENAI_BASE_URL` support now exists and has been verified
for real against OpenRouter's free-tier models (see PROGRESS.md's "First
real-model verification" section, added today). `.env` in this repo
(gitignored, not committed) already has a working `OPENROUTER_API_KEY` /
`OPENAI_API_KEY` / `OPENAI_BASE_URL` / `LLM_MODEL=minimax/minimax-m2.7:free`
/ `EMBEDDING_MODEL=openai/text-embedding-3-small` configuration that
produces correct, real results. This means the eval harness and the
README's sample-queries section can and should be built against a real
running system, not speculative examples -- actually run them.

## Part 1: Tests (`backend/tests/`)

Scope deliberately limited to pure, dependency-free logic -- this was
already decided earlier in this project's history and should not be
re-litigated: unit tests only for code that runs with zero external
services (no Postgres/Neo4j/OpenAI required), using real pytest, not ad
hoc scripts.

- `test_hierarchical_chunker.py`: parent/child chunk counts and
  approximate size targets on realistic text, child `parent_id`
  back-references are all valid, `document_id`/empty-text/
  parent<=child-tokens raise `ValueError` as the current code already
  does.
- `test_extraction_parsing.py`: `GraphExtractor._parse` -- well-formed
  JSON, malformed JSON, missing/invalid `section_title`/`content_type`,
  relationships referencing entities not present in `nodes` get dropped
  (existing behavior -- write a test that pins it, don't change it).
- `test_pipeline_parsing.py`: the three `_parse_*` static methods added to
  `rag/pipeline.py` by the agentic-pipeline spec (`_parse_validation`,
  `_parse_grade`, `_parse_rationale`) -- same malformed/missing-field
  coverage style as the extraction test.

Add `pytest` to `backend/requirements.txt`. Tests must pass with
`pytest backend/tests/` using nothing but the existing `backend/.venv` (or
a fresh one from `requirements.txt`) -- no docker, no API keys. Actually
run them and paste real output in the verification report, not "should
pass."

## Part 2: Eval harness (`backend/evals/`)

A small, real, runnable eval script -- not a framework. Given a live stack
with a working `OPENAI_BASE_URL` (see Context above), this should actually
execute against the running API and report real numbers, since that's now
possible for the first time in this project.

`backend/evals/run_evals.py`:
- A hardcoded set of 5-8 question/expected-fact pairs against a single
  fixture document (reuse or adapt the Acme Corp / Startup Inc acquisition
  text already used for today's manual verification -- it's in this
  conversation's history and produces a known-correct graph; write it into
  the eval script as the fixture text rather than requiring a separate
  data file).
- For each question: POST `/api/v1/ingest` once for the fixture (skip if
  already ingested), then POST `/api/v1/chat`, parse the SSE stream,
  check whether the expected fact (a short substring/keyword list per
  question, e.g. `["Acme Corp", "$50 million"]`) appears in the final
  answer text. Simple substring matching is fine -- do not build an
  LLM-judge for this pass, that's more machinery than 5-8 fixed questions
  need.
- Report per-question pass/fail, total latency from the SSE stream, and a
  summary (N/8 passed, average latency) printed to stdout and written to
  `backend/evals/results.md` (or similar) as a timestamped run record.
- This requires a live docker-compose stack and a working API key to run
  -- it is a manual/documented step, not part of `pytest`. Say so clearly
  in both the script's docstring and the README.
- Actually run it against the live stack (docker compose up, real
  OpenRouter key from `.env`) and include real results in your
  verification report and in `backend/evals/results.md`.

## Part 3: LangSmith tracing

LangChain/LangGraph auto-detect `LANGCHAIN_TRACING_V2`, `LANGCHAIN_API_KEY`,
`LANGCHAIN_PROJECT` directly from the environment -- no application code
changes are needed for tracing itself to work once those are set. This
part is almost entirely documentation + plumbing the env vars through, not
new code:
- Add `LANGCHAIN_TRACING_V2`, `LANGCHAIN_API_KEY`, `LANGCHAIN_PROJECT` to
  `.env.example` (commented/blank, matching the style already used for
  optional vars there) with a short comment explaining they're picked up
  automatically by LangGraph.
- Add the same three to `docker-compose.yml`'s `api` service `environment`
  block (same `${VAR:-}` pattern as `OPENAI_BASE_URL` -- and note in your
  own verification: this project already shipped one bug where an env var
  was added to `.env` but not to `docker-compose.yml`'s explicit list,
  causing it to silently never reach the container. Don't repeat it --
  actually confirm via `docker exec ... env` that these land in the
  container, the way that bug was caught and fixed today.)
- Document in the README how to enable it (get a LangSmith API key, set
  the three vars, traces appear at smith.langchain.com).
- **Cannot be verified against a real LangSmith account** -- no LangSmith
  key is available in this pass. State this limitation explicitly rather
  than claiming it works; verify only that the env vars are correctly
  wired through to the container (which is checkable without a real key).

## Part 4: README.md

The assignment (`AI_Assignment (1).docx`, summarized in this project's
history) explicitly grades on: architecture diagram, setup instructions,
sample queries, and a discussion of trade-offs between retrieval accuracy
and latency. All of this content already exists somewhere in this repo
and needs consolidating, not inventing:

- **Architecture**: an ASCII or mermaid diagram of the four services
  (postgres/pgvector, neo4j+apoc, api, ui) and the two pipelines (ingest:
  chunk -> embed -> vector store -> extract+classify -> graph write ->
  metadata backfill; chat: validate -> retrieve -> expand -> link -> 
  traverse -> grade (bounded retry) -> generate -> rationale).
- **Setup**: `docker compose up`, `.env` from `.env.example`, including
  the OpenRouter option (this repo has now actually verified that path
  works, unlike a purely theoretical "should work with OpenAI").
- **Sample queries**: use the real verified example from
  `PROGRESS.md`'s "First real-model verification" section -- the Acme
  Corp/Startup Inc document, the "Who acquired Startup Inc and how much
  did it cost?" query and its real answer/rationale, and the gibberish
  rejection example. These are real, not invented -- don't paraphrase
  them into something slightly different.
- **Trade-offs**: pull together what's already scattered across
  `PROGRESS.md`/`IMPROVEMENTS.md`/the three prior spec docs' Non-goals
  sections: pgvector vs. a dedicated vector DB, bounded widen-and-retry
  vs. a full ReAct tool-calling loop, per-parent vs. per-child
  classification granularity, cheap substring-match entity linking vs.
  real NER, per-document graph node namespacing (no cross-document entity
  resolution yet), no Alembic (idempotent `create_all` instead). Cite the
  real latency number from today's verification (35.4s for a 4-LLM-call
  chat turn on a free-tier model) as a concrete data point for the
  accuracy/latency discussion, not a hypothetical one.
- Also mention what's still open per `IMPROVEMENTS.md`: LangSmith tracing
  (wired, unverified against a real account), the eval harness (small,
  manual, not CI-integrated), cross-document entity resolution, DSPy/
  few-shot as a stretch goal.

## Files touched

New: `backend/tests/test_hierarchical_chunker.py`,
`backend/tests/test_extraction_parsing.py`,
`backend/tests/test_pipeline_parsing.py`, `backend/evals/run_evals.py`,
`backend/evals/results.md`, `README.md` (root, currently just the
one-line GitHub auto-init placeholder).

Modified: `backend/requirements.txt` (add `pytest`), `.env.example`,
`docker-compose.yml` (LangSmith env vars).

## Verification plan

- `pytest backend/tests/` -- run for real, paste actual output (pass
  count, no docker/API key needed).
- `docker compose up`, run `backend/evals/run_evals.py` against the live
  stack with the real OpenRouter config already in `.env` -- paste real
  results, not a description of what it would show.
- `docker exec graphrag-api-1 env | grep LANGCHAIN` after adding the vars
  to `.env` (even blank/placeholder values) -- confirm they reach the
  container, the same way the `OPENAI_BASE_URL` bug was caught today.
- Read the finished README fully once written and confirm every concrete
  claim in it (the sample query/answer, the latency number, which
  services docker-compose starts) actually matches what's true in this
  repo right now -- don't let it drift into aspirational claims.
- `docker compose down -v` when done.

## Commit plan

1. `backend/tests/` + `pytest` in requirements.txt
2. `backend/evals/` (script + real results from an actual run)
3. LangSmith env var plumbing (`.env.example` + `docker-compose.yml`)
4. `README.md`
5. `PROGRESS.md` update
