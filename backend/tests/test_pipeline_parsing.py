"""Unit tests for GraphRagPipeline's three structured-output parsers
(_parse_validation, _parse_grade, _parse_rationale) added by the agentic
pipeline rewrite -- pure JSON parsing/validation logic with no LLM call and
no pipeline instance required (all three are staticmethods), so these run
without docker or an API key.

Same defensive-default style as GraphExtractor._parse: malformed or
missing fields fall back to sane defaults rather than raising.
"""

from __future__ import annotations

import json

from app.rag.pipeline import GraphRagPipeline


class TestParseValidation:
    def test_well_formed_json(self) -> None:
        raw = json.dumps(
            {
                "is_gibberish": False,
                "tone": "curious",
                "verbosity_preference": "brief",
                "rewritten_query": "Who acquired Startup Inc and how much did it cost?",
                "suggested_reply": None,
            }
        )
        result = GraphRagPipeline._parse_validation(raw, "who bought startup inc")
        assert result.is_gibberish is False
        assert result.tone == "curious"
        assert result.verbosity_preference == "brief"
        assert result.rewritten_query == "Who acquired Startup Inc and how much did it cost?"
        assert result.suggested_reply is None

    def test_gibberish_with_suggested_reply(self) -> None:
        raw = json.dumps(
            {
                "is_gibberish": True,
                "tone": "neutral",
                "verbosity_preference": "detailed",
                "rewritten_query": "asdkjf qwoeiru zzz blorp",
                "suggested_reply": "I couldn't quite follow that -- could you rephrase your question?",
            }
        )
        result = GraphRagPipeline._parse_validation(raw, "asdkjf qwoeiru zzz blorp")
        assert result.is_gibberish is True
        assert result.suggested_reply is not None

    def test_malformed_json_falls_back_to_original_query(self) -> None:
        result = GraphRagPipeline._parse_validation("not json {{{", "original query text")
        assert result.is_gibberish is False
        assert result.tone == "neutral"
        assert result.verbosity_preference == "detailed"
        assert result.rewritten_query == "original query text"
        assert result.suggested_reply is None

    def test_missing_fields_use_defaults(self) -> None:
        result = GraphRagPipeline._parse_validation("{}", "original query text")
        assert result.is_gibberish is False
        assert result.tone == "neutral"
        assert result.verbosity_preference == "detailed"
        assert result.rewritten_query == "original query text"

    def test_empty_rewritten_query_falls_back_to_original(self) -> None:
        raw = json.dumps({"rewritten_query": "   "})
        result = GraphRagPipeline._parse_validation(raw, "original query text")
        assert result.rewritten_query == "original query text"

    def test_invalid_verbosity_preference_defaults_to_detailed(self) -> None:
        raw = json.dumps({"verbosity_preference": "extremely long and rambling"})
        result = GraphRagPipeline._parse_validation(raw, "q")
        assert result.verbosity_preference == "detailed"

    def test_verbosity_preference_case_insensitive(self) -> None:
        raw = json.dumps({"verbosity_preference": "BRIEF"})
        result = GraphRagPipeline._parse_validation(raw, "q")
        assert result.verbosity_preference == "brief"

    def test_empty_tone_defaults_to_neutral(self) -> None:
        raw = json.dumps({"tone": ""})
        result = GraphRagPipeline._parse_validation(raw, "q")
        assert result.tone == "neutral"


class TestParseGrade:
    def test_well_formed_json(self) -> None:
        raw = json.dumps({"is_sufficient": True, "relevance_score": 0.9, "missing_info": None})
        result = GraphRagPipeline._parse_grade(raw)
        assert result.is_sufficient is True
        assert result.relevance_score == 0.9
        assert result.missing_info is None

    def test_insufficient_with_missing_info(self) -> None:
        raw = json.dumps(
            {"is_sufficient": False, "relevance_score": 0.2, "missing_info": "no mention of the purchase price"}
        )
        result = GraphRagPipeline._parse_grade(raw)
        assert result.is_sufficient is False
        assert result.missing_info == "no mention of the purchase price"

    def test_malformed_json_defaults_to_sufficient(self) -> None:
        result = GraphRagPipeline._parse_grade("not json {{{")
        assert result.is_sufficient is True
        assert result.relevance_score == 0.5
        assert result.missing_info is None

    def test_missing_fields_use_defaults(self) -> None:
        result = GraphRagPipeline._parse_grade("{}")
        assert result.is_sufficient is True
        assert result.relevance_score == 0.5
        assert result.missing_info is None

    def test_relevance_score_clamped_above_one(self) -> None:
        raw = json.dumps({"relevance_score": 5.0})
        result = GraphRagPipeline._parse_grade(raw)
        assert result.relevance_score == 1.0

    def test_relevance_score_clamped_below_zero(self) -> None:
        raw = json.dumps({"relevance_score": -3.0})
        result = GraphRagPipeline._parse_grade(raw)
        assert result.relevance_score == 0.0

    def test_non_numeric_relevance_score_defaults(self) -> None:
        raw = json.dumps({"relevance_score": "very relevant"})
        result = GraphRagPipeline._parse_grade(raw)
        assert result.relevance_score == 0.5

    def test_empty_missing_info_becomes_none(self) -> None:
        raw = json.dumps({"missing_info": ""})
        result = GraphRagPipeline._parse_grade(raw)
        assert result.missing_info is None


class TestParseRationale:
    def test_well_formed_json(self) -> None:
        raw = json.dumps(
            {
                "explanation": "The answer cites the acquisition parent chunk and the ACQUIRED relationship.",
                "chunks_used": ["doc-1:p0:aaaaaaaa"],
                "relationships_used": ["Acme Corp -> ACQUIRED -> Startup Inc"],
            }
        )
        result = GraphRagPipeline._parse_rationale(raw)
        assert "acquisition" in result.explanation
        assert result.chunks_used == ["doc-1:p0:aaaaaaaa"]
        assert result.relationships_used == ["Acme Corp -> ACQUIRED -> Startup Inc"]

    def test_malformed_json_returns_empty_defaults(self) -> None:
        result = GraphRagPipeline._parse_rationale("not json {{{")
        assert result.explanation == ""
        assert result.chunks_used == []
        assert result.relationships_used == []

    def test_missing_fields_use_defaults(self) -> None:
        result = GraphRagPipeline._parse_rationale("{}")
        assert result.explanation == ""
        assert result.chunks_used == []
        assert result.relationships_used == []

    def test_blank_entries_in_lists_are_filtered_out(self) -> None:
        raw = json.dumps({"explanation": "x", "chunks_used": ["  ", "real-id", ""], "relationships_used": [""]})
        result = GraphRagPipeline._parse_rationale(raw)
        assert result.chunks_used == ["real-id"]
        assert result.relationships_used == []

    def test_null_explanation_becomes_empty_string(self) -> None:
        raw = json.dumps({"explanation": None})
        result = GraphRagPipeline._parse_rationale(raw)
        assert result.explanation == ""
