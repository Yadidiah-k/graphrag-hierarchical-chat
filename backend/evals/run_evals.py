"""Small, real eval harness for the /chat endpoint -- not a framework.

This is a manual, documented step, NOT part of `pytest backend/tests/`: it
requires a live docker-compose stack (`docker compose up`) and a working
`OPENAI_API_KEY` (real OpenAI, or an OpenAI-compatible endpoint via
`OPENAI_BASE_URL`, e.g. OpenRouter -- see `.env.example`). Run it with:

    python backend/evals/run_evals.py [--base-url http://localhost:8000]

It ingests one fixture document (the same Acme Corp / Startup Inc
acquisition report used for this project's manual real-model verification,
see VERIFICATION.md), skipping ingestion if that document_id already
succeeded on a prior run, then asks a fixed set of questions with known
expected facts and checks whether each fact appears (case-insensitive
substring match) in the final answer text pulled off the SSE stream. This
is deliberately simple substring matching, not an LLM-judge -- overkill for
8 fixed questions with unambiguous expected facts.

Results are printed to stdout and appended to results.md as a timestamped
run record.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

FIXTURE_DOCUMENT_ID = "eval-fixture-acme-startup"
FIXTURE_TITLE = "Acme Corp Acquisition Report"
FIXTURE_TEXT = (
    "Executive Summary\n\n"
    "Acme Corp acquired Startup Inc in March 2024 for $50 million to expand "
    "its AI platform capabilities. The acquisition was led by Acme Corp CEO "
    "Jane Smith, who has led the company since 2019. Startup Inc was founded "
    "by John Doe in 2019 and specialized in natural language processing "
    "technology.\n\n"
    "Financial Overview\n\n"
    "Acme Corp reported revenue of $200 million in fiscal year 2023, a 15% "
    "increase from the prior year. The company employs approximately 1,200 "
    "people across offices in San Francisco, New York, and London. Following "
    "the acquisition, Startup Inc's 45 employees joined Acme Corp's "
    "engineering division.\n\n"
    "Strategic Rationale\n\n"
    "The acquisition allows Acme Corp to integrate advanced NLP capabilities "
    "into its existing product suite. Analysts expect the combined entity to "
    "compete more effectively against larger rivals in the enterprise AI "
    "market."
)

# (question, expected facts -- every string in the list must appear
# case-insensitively as a substring of the final answer for the question to
# pass)
QUESTIONS: list[tuple[str, list[str]]] = [
    ("Who acquired Startup Inc and how much did it cost?", ["Acme Corp", "$50 million"]),
    ("Who is the CEO of Acme Corp?", ["Jane Smith"]),
    ("Who founded Startup Inc, and in what year?", ["John Doe", "2019"]),
    ("What was Acme Corp's revenue in fiscal year 2023?", ["$200 million"]),
    ("Roughly how many people does Acme Corp employ?", ["1,200"]),
    ("Which cities does Acme Corp have offices in?", ["San Francisco", "New York", "London"]),
    ("How many Startup Inc employees joined Acme Corp after the acquisition?", ["45"]),
    ("What technology did Startup Inc specialize in?", ["natural language processing"]),
]

RESULTS_PATH = Path(__file__).parent / "results.md"


def _normalize_whitespace(text: str) -> str:
    """Collapse all Unicode whitespace runs (including the narrow no-break
    space, U+202F, that this project's free-tier model was observed to emit
    between words, e.g. "Acme\\u202fCorp") to a single ASCII space before
    substring matching -- otherwise a factually-correct answer fails the
    eval purely on typographic spacing, not content."""
    return re.sub(r"\s+", " ", text)


@dataclass
class QuestionResult:
    question: str
    expected_facts: list[str]
    answer_text: str
    passed: bool
    missing_facts: list[str]
    latency_s: float
    error: str | None = None


@dataclass
class RunSummary:
    results: list[QuestionResult] = field(default_factory=list)

    @property
    def passed_count(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def total_count(self) -> int:
        return len(self.results)

    @property
    def average_latency_s(self) -> float:
        if not self.results:
            return 0.0
        return sum(r.latency_s for r in self.results) / len(self.results)


def _ensure_fixture_ingested(client: httpx.Client, base_url: str) -> None:
    status_resp = client.get(f"{base_url}/api/v1/ingest/{FIXTURE_DOCUMENT_ID}")
    if status_resp.status_code == 200 and status_resp.json().get("status") == "succeeded":
        print(f"Fixture '{FIXTURE_DOCUMENT_ID}' already ingested, skipping.")
        return

    print(f"Ingesting fixture document '{FIXTURE_DOCUMENT_ID}'...")
    ingest_resp = client.post(
        f"{base_url}/api/v1/ingest",
        json={"document_id": FIXTURE_DOCUMENT_ID, "title": FIXTURE_TITLE, "text": FIXTURE_TEXT},
    )
    ingest_resp.raise_for_status()

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        poll = client.get(f"{base_url}/api/v1/ingest/{FIXTURE_DOCUMENT_ID}")
        poll.raise_for_status()
        status = poll.json().get("status")
        if status == "succeeded":
            print(f"Ingest succeeded: {poll.json()}")
            return
        if status == "failed":
            raise RuntimeError(f"Fixture ingestion failed: {poll.json()}")
        time.sleep(2)
    raise TimeoutError("Fixture ingestion did not complete within 120s")


def _run_chat_question(client: httpx.Client, base_url: str, question: str, expected_facts: list[str]) -> QuestionResult:
    started = time.monotonic()
    answer_parts: list[str] = []
    error: str | None = None

    try:
        with client.stream(
            "POST",
            f"{base_url}/api/v1/chat",
            json={"query": question, "document_id": FIXTURE_DOCUMENT_ID},
            timeout=120,
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[len("data: "):])
                if event["type"] == "token" and event["data"]:
                    answer_parts.append(event["data"])
                elif event["type"] == "error":
                    error = event["data"]
    except httpx.HTTPError as exc:
        error = str(exc)

    latency_s = time.monotonic() - started
    answer_text = "".join(answer_parts)

    if error:
        return QuestionResult(
            question=question,
            expected_facts=expected_facts,
            answer_text=answer_text,
            passed=False,
            missing_facts=expected_facts,
            latency_s=latency_s,
            error=error,
        )

    normalized_answer = _normalize_whitespace(answer_text).lower()
    missing = [fact for fact in expected_facts if _normalize_whitespace(fact).lower() not in normalized_answer]
    return QuestionResult(
        question=question,
        expected_facts=expected_facts,
        answer_text=answer_text,
        passed=not missing,
        missing_facts=missing,
        latency_s=latency_s,
    )


def run(base_url: str) -> RunSummary:
    summary = RunSummary()
    with httpx.Client() as client:
        health = client.get(f"{base_url}/api/v1/health")
        health.raise_for_status()

        _ensure_fixture_ingested(client, base_url)

        for question, expected_facts in QUESTIONS:
            print(f"\nAsking: {question}")
            result = _run_chat_question(client, base_url, question, expected_facts)
            summary.results.append(result)
            status = "PASS" if result.passed else "FAIL"
            print(f"  [{status}] {result.latency_s:.1f}s -- {result.answer_text[:200]!r}")
            if not result.passed:
                print(f"  missing facts: {result.missing_facts}" + (f", error: {result.error}" if result.error else ""))

    return summary


def _write_results_record(summary: RunSummary, base_url: str) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [f"\n## Run: {timestamp}\n", f"Base URL: `{base_url}`\n", f"Fixture: `{FIXTURE_DOCUMENT_ID}`\n"]
    lines.append("| # | Question | Result | Latency (s) | Missing facts |")
    lines.append("|---|----------|--------|--------------|----------------|")
    for i, r in enumerate(summary.results, start=1):
        status = "PASS" if r.passed else "FAIL"
        missing = ", ".join(r.missing_facts) if r.missing_facts else "-"
        lines.append(f"| {i} | {r.question} | {status} | {r.latency_s:.1f} | {missing} |")
    lines.append(
        f"\n**Summary: {summary.passed_count}/{summary.total_count} passed, "
        f"average latency {summary.average_latency_s:.1f}s**\n"
    )

    with RESULTS_PATH.open("a") as f:
        f.write("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000", help="Base URL of the running API")
    args = parser.parse_args()

    summary = run(args.base_url)

    print("\n" + "=" * 60)
    print(f"Summary: {summary.passed_count}/{summary.total_count} passed")
    print(f"Average latency: {summary.average_latency_s:.1f}s")
    print("=" * 60)

    _write_results_record(summary, args.base_url)
    print(f"\nResults appended to {RESULTS_PATH}")

    return 0 if summary.passed_count == summary.total_count else 1


if __name__ == "__main__":
    sys.exit(main())
