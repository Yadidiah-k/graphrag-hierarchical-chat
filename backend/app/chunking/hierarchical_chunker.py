"""Parent-child (small-to-big) hierarchical chunking.

Strategy:
    1. Split the document into large "parent" blocks (~1000 tokens) using a
       recursive character splitter that prefers paragraph/sentence boundaries.
    2. Split each parent block into smaller "child" blocks (~200 tokens) the
       same way.
    3. Every child chunk stores a reference to its parent_id, so retrieval can
       match on the precise child chunk and expand to the full parent context
       before it is sent to the LLM.
"""

from __future__ import annotations

import uuid

from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.schemas.models import ChildChunk, HierarchicalChunkResult, ParentChunk

# Rough heuristic: ~4 characters per token for English text. Good enough for
# chunk-sizing purposes; it does not need to be exact token accounting.
CHARS_PER_TOKEN = 4


def _tokens_to_chars(tokens: int) -> int:
    return tokens * CHARS_PER_TOKEN


class HierarchicalChunker:
    def __init__(
        self,
        parent_chunk_tokens: int = 1000,
        child_chunk_tokens: int = 200,
        chunk_overlap_tokens: int = 50,
    ) -> None:
        if parent_chunk_tokens <= child_chunk_tokens:
            raise ValueError("parent_chunk_tokens must be larger than child_chunk_tokens")
        if chunk_overlap_tokens < 0:
            raise ValueError("chunk_overlap_tokens must be >= 0")

        self.parent_splitter = RecursiveCharacterTextSplitter(
            chunk_size=_tokens_to_chars(parent_chunk_tokens),
            chunk_overlap=_tokens_to_chars(chunk_overlap_tokens),
            separators=["\n\n", "\n", ". ", " ", ""],
        )
        self.child_splitter = RecursiveCharacterTextSplitter(
            chunk_size=_tokens_to_chars(child_chunk_tokens),
            chunk_overlap=_tokens_to_chars(min(chunk_overlap_tokens, child_chunk_tokens // 2)),
            separators=["\n\n", "\n", ". ", " ", ""],
        )

    def chunk_document(self, document_id: str, text: str) -> HierarchicalChunkResult:
        if not document_id:
            raise ValueError("document_id is required")
        if not text or not text.strip():
            raise ValueError("text must not be empty")

        parents: list[ParentChunk] = []
        children: list[ChildChunk] = []

        parent_texts = self.parent_splitter.split_text(text)

        cursor = 0
        for parent_order, parent_text in enumerate(parent_texts):
            start_char = text.find(parent_text, cursor)
            if start_char == -1:
                start_char = cursor
            end_char = start_char + len(parent_text)
            cursor = max(cursor, end_char - _tokens_to_chars(50))  # allow overlap search

            parent_id = f"{document_id}:p{parent_order}:{uuid.uuid4().hex[:8]}"
            parents.append(
                ParentChunk(
                    parent_id=parent_id,
                    document_id=document_id,
                    text=parent_text,
                    start_char=start_char,
                    end_char=end_char,
                    order=parent_order,
                )
            )

            child_texts = self.child_splitter.split_text(parent_text)
            child_cursor = 0
            for child_order, child_text in enumerate(child_texts):
                local_start = parent_text.find(child_text, child_cursor)
                if local_start == -1:
                    local_start = child_cursor
                local_end = local_start + len(child_text)
                child_cursor = max(child_cursor, local_end - _tokens_to_chars(25))

                children.append(
                    ChildChunk(
                        child_id=f"{parent_id}:c{child_order}:{uuid.uuid4().hex[:8]}",
                        parent_id=parent_id,
                        document_id=document_id,
                        text=child_text,
                        start_char=start_char + local_start,
                        end_char=start_char + local_end,
                        order=child_order,
                    )
                )

        return HierarchicalChunkResult(document_id=document_id, parents=parents, children=children)
