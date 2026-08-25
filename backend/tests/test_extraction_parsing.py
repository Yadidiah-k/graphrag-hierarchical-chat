"""Unit tests for GraphExtractor._parse -- pure JSON parsing/validation
logic with no LLM call, so these run without docker or an API key.

`_parse` is a staticmethod that turns the raw string an LLM is expected to
return into a validated ExtractionResult, defensively defaulting on
anything malformed rather than raising.
"""

from __future__ import annotations

import json

from app.graph.extraction import GraphExtractor

PARENT_ID = "doc-1:p0:aaaaaaaa"
DOCUMENT_ID = "doc-1"
CHILD_IDS = ["doc-1:p0:aaaaaaaa:c0:bbbbbbbb", "doc-1:p0:aaaaaaaa:c1:cccccccc"]


def _parse(raw: str):
    return GraphExtractor._parse(raw, PARENT_ID, DOCUMENT_ID, CHILD_IDS)


class TestWellFormedJson:
    def test_nodes_and_relationships_parsed(self) -> None:
        raw = json.dumps(
            {
                "nodes": [
                    {"name": "Acme Corp", "type": "Organization"},
                    {"name": "Startup Inc", "type": "Organization"},
                ],
                "relationships": [
                    {
                        "source": "Acme Corp",
                        "target": "Startup Inc",
                        "relation_type": "acquired",
                        "evidence": "Acme Corp acquired Startup Inc in March 2024 for $50 million",
                    }
                ],
                "section_title": "Executive Summary",
                "content_type": "prose",
            }
        )
        result = _parse(raw)

        assert len(result.nodes) == 2
        assert len(result.relationships) == 1
        assert result.section_title == "Executive Summary"
        assert result.content_type == "prose"

    def test_node_id_is_globally_scoped_by_normalized_name_not_document(self) -> None:
        """Node ids are no longer document-namespaced (superseded behavior --
        see the cross-document entity resolution design doc): a provisional
        id is `{normalized_name}:{random_suffix}`, with no document_id in it
        at all, so EntityResolver can later rewrite it onto an existing
        cross-document match by normalized_name alone."""
        raw = json.dumps({"nodes": [{"name": "Acme Corp", "type": "Organization"}], "relationships": []})
        result = _parse(raw)
        node_id = result.nodes[0].node_id

        assert node_id.startswith("acme_corp:")
        suffix = node_id.split(":", 1)[1]
        assert len(suffix) == 8
        assert DOCUMENT_ID not in node_id
        assert result.nodes[0].normalized_name == "acme_corp"

    def test_normalized_name_collapses_internal_whitespace(self) -> None:
        raw = json.dumps({"nodes": [{"name": "Acme   Corp Global", "type": "Organization"}], "relationships": []})
        result = _parse(raw)
        assert result.nodes[0].normalized_name == "acme_corp_global"

    def test_relation_type_uppercased_and_spaces_become_underscores(self) -> None:
        raw = json.dumps(
            {
                "nodes": [
                    {"name": "Acme Corp", "type": "Organization"},
                    {"name": "Startup Inc", "type": "Organization"},
                ],
                "relationships": [
                    {
                        "source": "Acme Corp",
                        "target": "Startup Inc",
                        "relation_type": "acquired company",
                        "evidence": "evidence text",
                    }
                ],
            }
        )
        result = _parse(raw)
        assert result.relationships[0].relation_type == "ACQUIRED_COMPANY"

    def test_source_child_ids_and_parent_ids_carried_through(self) -> None:
        raw = json.dumps({"nodes": [{"name": "Acme Corp", "type": "Organization"}], "relationships": []})
        result = _parse(raw)
        assert result.nodes[0].source_child_ids == CHILD_IDS
        assert result.nodes[0].source_parent_ids == [PARENT_ID]

    def test_entity_with_empty_name_is_skipped(self) -> None:
        raw = json.dumps(
            {"nodes": [{"name": "", "type": "Organization"}, {"name": "Acme Corp", "type": "Organization"}]}
        )
        result = _parse(raw)
        assert len(result.nodes) == 1
        assert result.nodes[0].name == "Acme Corp"

    def test_node_missing_type_defaults_to_entity(self) -> None:
        raw = json.dumps({"nodes": [{"name": "Acme Corp"}]})
        result = _parse(raw)
        assert result.nodes[0].type == "Entity"


class TestMalformedJson:
    def test_invalid_json_returns_empty_result_with_defaults(self) -> None:
        result = _parse("not valid json {{{")
        assert result.nodes == []
        assert result.relationships == []
        assert result.section_title is None
        assert result.content_type == "prose"

    def test_empty_string_returns_empty_result(self) -> None:
        result = _parse("")
        assert result.nodes == []
        assert result.relationships == []


class TestSectionTitleAndContentType:
    def test_missing_section_title_defaults_to_none(self) -> None:
        raw = json.dumps({"nodes": [], "relationships": [], "content_type": "prose"})
        result = _parse(raw)
        assert result.section_title is None

    def test_explicit_null_section_title_stays_none(self) -> None:
        raw = json.dumps({"nodes": [], "relationships": [], "section_title": None})
        result = _parse(raw)
        assert result.section_title is None

    def test_empty_string_section_title_becomes_none(self) -> None:
        raw = json.dumps({"nodes": [], "relationships": [], "section_title": ""})
        result = _parse(raw)
        assert result.section_title is None

    def test_missing_content_type_defaults_to_prose(self) -> None:
        raw = json.dumps({"nodes": [], "relationships": []})
        result = _parse(raw)
        assert result.content_type == "prose"

    def test_invalid_content_type_literal_defaults_to_prose(self) -> None:
        raw = json.dumps({"nodes": [], "relationships": [], "content_type": "spreadsheet"})
        result = _parse(raw)
        assert result.content_type == "prose"

    def test_mixed_case_content_type_normalized(self) -> None:
        raw = json.dumps({"nodes": [], "relationships": [], "content_type": "Table"})
        result = _parse(raw)
        assert result.content_type == "table"


class TestRelationshipDropping:
    """Existing (pre-existing, not changed by this test) behavior: a
    relationship referencing an entity name not present in `nodes` is
    silently dropped rather than raising or keeping a dangling reference."""

    def test_relationship_referencing_unknown_source_is_dropped(self) -> None:
        raw = json.dumps(
            {
                "nodes": [{"name": "Startup Inc", "type": "Organization"}],
                "relationships": [
                    {
                        "source": "Acme Corp",  # not in nodes
                        "target": "Startup Inc",
                        "relation_type": "ACQUIRED",
                        "evidence": "evidence",
                    }
                ],
            }
        )
        result = _parse(raw)
        assert result.relationships == []

    def test_relationship_referencing_unknown_target_is_dropped(self) -> None:
        raw = json.dumps(
            {
                "nodes": [{"name": "Acme Corp", "type": "Organization"}],
                "relationships": [
                    {
                        "source": "Acme Corp",
                        "target": "Startup Inc",  # not in nodes
                        "relation_type": "ACQUIRED",
                        "evidence": "evidence",
                    }
                ],
            }
        )
        result = _parse(raw)
        assert result.relationships == []

    def test_relationship_with_valid_entities_but_missing_relation_type_is_dropped(self) -> None:
        raw = json.dumps(
            {
                "nodes": [
                    {"name": "Acme Corp", "type": "Organization"},
                    {"name": "Startup Inc", "type": "Organization"},
                ],
                "relationships": [
                    {"source": "Acme Corp", "target": "Startup Inc", "relation_type": "", "evidence": "evidence"}
                ],
            }
        )
        result = _parse(raw)
        assert result.relationships == []

    def test_mix_of_valid_and_dangling_relationships_keeps_only_valid(self) -> None:
        raw = json.dumps(
            {
                "nodes": [
                    {"name": "Acme Corp", "type": "Organization"},
                    {"name": "Startup Inc", "type": "Organization"},
                ],
                "relationships": [
                    {
                        "source": "Acme Corp",
                        "target": "Startup Inc",
                        "relation_type": "ACQUIRED",
                        "evidence": "evidence",
                    },
                    {
                        "source": "Acme Corp",
                        "target": "Ghost Company",  # not in nodes
                        "relation_type": "PARTNERED_WITH",
                        "evidence": "evidence",
                    },
                ],
            }
        )
        result = _parse(raw)
        assert len(result.relationships) == 1
        assert result.relationships[0].relation_type == "ACQUIRED"
