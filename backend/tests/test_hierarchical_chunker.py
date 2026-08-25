"""Unit tests for the parent/child hierarchical chunker -- pure text
splitting with zero external dependencies, so these run without docker or
an API key."""

from __future__ import annotations

import pytest

from app.chunking.hierarchical_chunker import CHARS_PER_TOKEN, HierarchicalChunker

# A few paragraphs of realistic prose, long enough to force multiple parent
# chunks at the default ~1000-token parent size.
REALISTIC_TEXT = "\n\n".join(
    [
        (
            "Acme Corp is a mid-sized logistics company headquartered in Denver, "
            "Colorado. Founded in 1998, it has grown from a single regional "
            "trucking operation into a national freight and warehousing "
            "business. Acme Corp acquired Startup Inc in March 2024 for $50 "
            "million, a deal intended to bring Startup Inc's route-optimization "
            "software in-house rather than licensing it from a third party."
        ),
        (
            "Startup Inc was founded in 2019 by a small team of engineers who "
            "previously worked on mapping software. Its flagship product, a "
            "route-optimization engine, reduced fuel costs for its logistics "
            "customers by an average of twelve percent. Before the acquisition, "
            "Startup Inc's largest customer was Acme Corp itself, which had "
            "licensed the software since 2021."
        ),
        (
            "Industry analysts described the acquisition as a vertical "
            "integration play: rather than continuing to pay licensing fees, "
            "Acme Corp chose to own the technology outright and fold Startup "
            "Inc's engineering team into its own logistics division. The deal "
            "closed without regulatory objection, and most of Startup Inc's "
            "twenty-two employees joined Acme Corp directly."
        ),
        (
            "Financial terms of the acquisition were disclosed in Acme Corp's "
            "quarterly filing: the $50 million purchase price was paid in a mix "
            "of cash and stock, with an additional performance-based earnout of "
            "up to $10 million payable over two years if the route-optimization "
            "product hit specific customer-retention targets."
        ),
    ]
    * 6  # repeat to comfortably exceed one parent chunk's ~1000-token target
)


def _default_chunker() -> HierarchicalChunker:
    return HierarchicalChunker(parent_chunk_tokens=1000, child_chunk_tokens=200, chunk_overlap_tokens=50)


class TestChunkCountsAndSizing:
    def test_produces_multiple_parents_on_long_text(self) -> None:
        result = _default_chunker().chunk_document("doc-1", REALISTIC_TEXT)
        assert len(result.parents) > 1

    def test_produces_multiple_children_per_parent(self) -> None:
        result = _default_chunker().chunk_document("doc-1", REALISTIC_TEXT)
        assert len(result.children) > len(result.parents)

    def test_parent_chunks_approximate_target_token_size(self) -> None:
        result = _default_chunker().chunk_document("doc-1", REALISTIC_TEXT)
        max_parent_chars = 1000 * CHARS_PER_TOKEN
        # Recursive splitting can slightly overshoot the target at a
        # separator boundary; allow generous headroom rather than pinning
        # an exact byte count.
        for parent in result.parents[:-1]:
            assert len(parent.text) <= max_parent_chars * 1.5

    def test_child_chunks_approximate_target_token_size(self) -> None:
        result = _default_chunker().chunk_document("doc-1", REALISTIC_TEXT)
        max_child_chars = 200 * CHARS_PER_TOKEN
        for child in result.children[:-1]:
            assert len(child.text) <= max_child_chars * 1.5

    def test_short_text_still_produces_one_parent_and_child(self) -> None:
        result = _default_chunker().chunk_document("doc-1", "A short sentence about Acme Corp.")
        assert len(result.parents) == 1
        assert len(result.children) >= 1


class TestParentChildLinkage:
    def test_every_child_parent_id_references_a_real_parent(self) -> None:
        result = _default_chunker().chunk_document("doc-1", REALISTIC_TEXT)
        parent_ids = {p.parent_id for p in result.parents}
        for child in result.children:
            assert child.parent_id in parent_ids

    def test_every_child_document_id_matches(self) -> None:
        result = _default_chunker().chunk_document("doc-1", REALISTIC_TEXT)
        assert all(child.document_id == "doc-1" for child in result.children)
        assert all(parent.document_id == "doc-1" for parent in result.parents)

    def test_parent_and_child_ids_are_unique(self) -> None:
        result = _default_chunker().chunk_document("doc-1", REALISTIC_TEXT)
        parent_ids = [p.parent_id for p in result.parents]
        child_ids = [c.child_id for c in result.children]
        assert len(parent_ids) == len(set(parent_ids))
        assert len(child_ids) == len(set(child_ids))


class TestValidationErrors:
    def test_empty_document_id_raises(self) -> None:
        with pytest.raises(ValueError):
            _default_chunker().chunk_document("", "some text")

    def test_empty_text_raises(self) -> None:
        with pytest.raises(ValueError):
            _default_chunker().chunk_document("doc-1", "")

    def test_whitespace_only_text_raises(self) -> None:
        with pytest.raises(ValueError):
            _default_chunker().chunk_document("doc-1", "   \n\t  ")

    def test_parent_tokens_equal_to_child_tokens_raises(self) -> None:
        with pytest.raises(ValueError):
            HierarchicalChunker(parent_chunk_tokens=200, child_chunk_tokens=200)

    def test_parent_tokens_less_than_child_tokens_raises(self) -> None:
        with pytest.raises(ValueError):
            HierarchicalChunker(parent_chunk_tokens=100, child_chunk_tokens=200)

    def test_negative_chunk_overlap_raises(self) -> None:
        with pytest.raises(ValueError):
            HierarchicalChunker(parent_chunk_tokens=1000, child_chunk_tokens=200, chunk_overlap_tokens=-1)
