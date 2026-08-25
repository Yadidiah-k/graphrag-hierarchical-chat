"""Streamlit chat UI for the GraphRAG backend.

Two tabs:
    Chat            -- ingest documents, ask questions, stream answers with
                        expandable citation cards and a graph view of the
                        relationship triples actually used for that answer.
    Graph Explorer   -- free-form lookup against GET /api/v1/graph/subgraph.
"""

from __future__ import annotations

import json
import os
import time

import requests
import streamlit as st
from pyvis.network import Network

API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000")

st.set_page_config(page_title="GraphRAG Chat", layout="wide")


def render_graph(nodes: list[dict], edges: list[dict], height: str = "480px") -> None:
    if not edges and not nodes:
        st.caption("Nothing to show.")
        return

    net = Network(height=height, width="100%", directed=True, bgcolor="#0e1117", font_color="#e6e6e6")
    seen: set[str] = set()

    for edge in edges:
        for node_id, label in ((edge["source"], edge.get("source_label", edge["source"])), (edge["target"], edge.get("target_label", edge["target"]))):
            if node_id not in seen:
                net.add_node(node_id, label=label)
                seen.add(node_id)
        net.add_edge(edge["source"], edge["target"], label=edge["relation"], title=edge.get("evidence", ""))

    for node in nodes:
        if node["id"] not in seen:
            net.add_node(node["id"], label=node["label"], title=node.get("type", ""))
            seen.add(node["id"])

    net.repulsion(node_distance=200, spring_length=150)
    # Multiple relationships between the same pair of nodes (common --
    # e.g. a person both leading and being CEO-since a company) otherwise
    # draw as coincident straight lines with fully overlapping labels;
    # dynamic smoothing curves them apart so each label is legible.
    net.set_edge_smooth("dynamic")
    html = net.generate_html(notebook=False)
    st.components.v1.html(html, height=int(height.replace("px", "")) + 20, scrolling=True)


def triples_to_edges(triples: list[dict]) -> list[dict]:
    return [
        {
            "source": t["source"],
            "target": t["target"],
            "relation": t["relation"],
            "evidence": t.get("evidence", ""),
            "source_label": t["source"],
            "target_label": t["target"],
        }
        for t in triples
    ]


def stream_chat(
    query: str,
    document_id: str | None,
    top_k: int,
    history: list[dict],
    section_title_filter: str | None = None,
    content_type_filter: str | None = None,
):
    """Returns (token_generator, citations_holder, triples_holder, rationale_holder).

    citations_holder / triples_holder / rationale_holder are dicts populated
    as a side effect of consuming the generator -- the backend emits
    citations/triples before the first token and rationale after the last
    one, so by the time st.write_stream finishes, citations/triples already
    hold their final values and rationale fills in right after.
    """
    citations_holder: dict = {}
    triples_holder: dict = {}
    rationale_holder: dict = {}

    def token_gen():
        payload = {
            "query": query,
            "document_id": document_id,
            "top_k": top_k,
            "history": history,
            "section_title_filter": section_title_filter,
            "content_type_filter": content_type_filter,
        }
        # 240s, not 120s: the agentic pipeline is up to 4 sequential LLM
        # calls (validate -> grade -> generate -> rationale) plus a possible
        # bounded retry (extra retrieve+grade round), which can approach
        # 120s on a free-tier model on its own -- observed a real timeout at
        # 120s during browser testing on a slow response.
        with requests.post(f"{API_BASE_URL}/api/v1/chat", json=payload, stream=True, timeout=240) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                event = json.loads(line[len("data:") :].strip())
                event_type = event.get("type")
                if event_type == "token":
                    yield event.get("data") or ""
                elif event_type == "citations":
                    citations_holder["value"] = event.get("data") or []
                elif event_type == "triples":
                    triples_holder["value"] = event.get("data") or []
                elif event_type == "rationale":
                    rationale_holder["value"] = event.get("data")
                elif event_type == "error":
                    yield f"\n\n**Error:** {event.get('data')}"
                # No break on "done" -- the backend sends "rationale" after
                # it (chat.py needs the full answer text first), so this
                # loop keeps reading until the server actually closes the
                # connection. Breaking early here silently discarded every
                # rationale event; found via real browser testing, since
                # a raw SSE dump (e.g. curl) doesn't reproduce this
                # early-exit behavior.

    return token_gen(), citations_holder, triples_holder, rationale_holder


def render_citations(citations: list[dict]) -> None:
    if not citations:
        st.caption("No citations retrieved for this answer.")
        return
    st.markdown("**Citations (parent chunks)**")
    for c in citations:
        with st.expander(f"{c['document_id']} · {c['parent_id']} · score={c['score']:.3f}"):
            st.write(c["text"])


def render_rationale(rationale: dict | None) -> None:
    if not rationale:
        st.caption("No rationale available for this answer.")
        return
    st.markdown("**Why this answer**")
    st.write(rationale.get("explanation", ""))
    chunks_used = rationale.get("chunks_used") or []
    relationships_used = rationale.get("relationships_used") or []
    if chunks_used:
        st.caption(f"Chunks used: {', '.join(chunks_used)}")
    if relationships_used:
        st.caption(f"Relationships used: {', '.join(relationships_used)}")


def ingest_tab_sidebar() -> None:
    st.sidebar.header("Ingest a document")
    document_id = st.sidebar.text_input("Document ID", value="doc-1")
    title = st.sidebar.text_input("Title", value="Untitled")
    uploaded = st.sidebar.file_uploader("Upload a .txt file", type=["txt"])
    text = uploaded.read().decode("utf-8") if uploaded is not None else st.sidebar.text_area("...or paste text", height=160)

    if st.sidebar.button("Ingest", type="primary", disabled=not text.strip()):
        with st.sidebar:
            with st.spinner("Ingesting..."):
                resp = requests.post(
                    f"{API_BASE_URL}/api/v1/ingest",
                    json={"document_id": document_id, "title": title, "text": text},
                    timeout=30,
                )
                resp.raise_for_status()

                status = "queued"
                result = resp.json()
                deadline = time.time() + 120
                while status in ("queued", "running") and time.time() < deadline:
                    time.sleep(1.5)
                    poll = requests.get(f"{API_BASE_URL}/api/v1/ingest/{document_id}", timeout=10)
                    poll.raise_for_status()
                    result = poll.json()
                    status = result["status"]

            if status == "succeeded":
                st.success(
                    f"Ingested: {result['parent_chunk_count']} parent chunks, "
                    f"{result['child_chunk_count']} child chunks, "
                    f"{result['node_count']} entities, {result['relationship_count']} relationships."
                )
            elif status == "failed":
                st.error("Ingestion failed -- check the API logs.")
            else:
                st.warning(f"Still {status} after 2 minutes -- check back later.")


def chat_tab() -> None:
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "last_citations" not in st.session_state:
        st.session_state.last_citations = []
    if "last_triples" not in st.session_state:
        st.session_state.last_triples = []
    if "last_rationale" not in st.session_state:
        st.session_state.last_rationale = None

    document_filter = st.sidebar.text_input("Filter chat by document_id (optional)")
    top_k = st.sidebar.slider("top_k (vector search)", 1, 20, 8)
    content_type_filter = st.sidebar.selectbox(
        "Content type filter", options=[None, "prose", "table", "list", "other"],
        format_func=lambda v: v or "(any)",
    )
    section_title_filter = st.sidebar.text_input("Section title filter (optional)")

    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    query = st.chat_input("Ask a question about your ingested documents")
    if query:
        history = [{"role": m["role"], "content": m["content"]} for m in st.session_state.messages]
        st.session_state.messages.append({"role": "user", "content": query})
        with st.chat_message("user"):
            st.markdown(query)

        with st.chat_message("assistant"):
            token_gen, citations_holder, triples_holder, rationale_holder = stream_chat(
                query,
                document_filter or None,
                top_k,
                history,
                section_title_filter or None,
                content_type_filter,
            )
            answer = st.write_stream(token_gen)
            st.session_state.messages.append({"role": "assistant", "content": answer})
            st.session_state.last_citations = citations_holder.get("value", [])
            st.session_state.last_triples = triples_holder.get("value", [])
            st.session_state.last_rationale = rationale_holder.get("value")

    if st.session_state.messages:
        render_citations(st.session_state.last_citations)
        render_rationale(st.session_state.last_rationale)
        st.markdown("**Graph relationships used for the last answer**")
        render_graph([], triples_to_edges(st.session_state.last_triples))


def graph_explorer_tab() -> None:
    st.markdown("Look up any entity's neighborhood directly against the graph store.")
    col1, col2 = st.columns([3, 1])
    with col1:
        query = st.text_input("Entity name / free text to link", key="explorer_query")
    with col2:
        hop_depth = st.number_input("Hop depth", min_value=1, max_value=5, value=2)

    if st.button("Explore") and query:
        resp = requests.get(
            f"{API_BASE_URL}/api/v1/graph/subgraph",
            params={"query": query, "hop_depth": hop_depth},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        st.caption(f"{len(data['nodes'])} nodes, {len(data['edges'])} edges")
        render_graph(data["nodes"], data["edges"])


st.title("GraphRAG Chat")
ingest_tab_sidebar()

tab_chat, tab_graph = st.tabs(["Chat", "Graph Explorer"])
with tab_chat:
    chat_tab()
with tab_graph:
    graph_explorer_tab()
