"""
search.py — Query the KB and render results.

Layered design:
  - search(query, top_k, doc_filter): raw hits list. Uses hybrid
    (BM25 + embedding) when the embedding model is loaded AND the
    embeddings are built; otherwise pure BM25.
  - hybrid_search(query, top_k, ...): explicit hybrid; falls back
    to BM25 if embeddings are unavailable.
  - render(hits, ...): pretty CLI formatting with source line per hit.
  - format_compact(...): single-line answer for chat/IM later.

No LLM involved (the embedding model is local). Each hit includes
the original chunk text + provenance.
"""
from __future__ import annotations

import config
import bm25
import storage
import embedding


def search(
    query: str,
    top_k: int = config.DEFAULT_TOP_K,
    filename: str | None = None,
) -> list[dict]:
    """
    Run a hybrid (BM25 + embedding) query if embeddings are ready,
    else fall back to pure BM25. Optionally restrict to a single
    source filename.

    Returns the same dict shape as `bm25.score_query`, plus optional
    `bm25_score` / `embed_score` keys when hybrid is in use.
    Empty list if no hits.
    """
    hits = _select_search()(query, top_k=top_k)
    if filename:
        hits = [h for h in hits if h["filename"] == filename]
    return hits


def _select_search():
    """
    Decide whether to use hybrid or pure BM25 for this process.

    The check is cheap (one COUNT query + a module-level bool), so we
    re-evaluate on every call. The module-level `_EMBEDDINGS_TRIED`
    flag prevents repeated model-load attempts on every query if the
    model genuinely isn't available.
    """
    global _EMBEDDINGS_READY

    if not config.USE_HYBRID_SEARCH_WHEN_READY:
        return _pure_bm25_search

    # If we already know embeddings are present + model is loaded,
    # skip the COUNT query and go straight to hybrid.
    if _EMBEDDINGS_READY:
        return hybrid_search

    if not embedding.is_available():
        # Model didn't load — fall back. Don't re-try every query.
        _EMBEDDINGS_READY = False
        return _pure_bm25_search

    n_emb = storage.count_embeddings(config.EMBEDDING_MODEL)
    n_chunks = storage.corpus_stats()["n_chunks"]
    if n_chunks == 0 or n_emb < n_chunks * 0.5:
        # Less than half the chunks are embedded — hybrid would be
        # misleading. Fall back. The user should run
        # `python app.py rebuild --with-embeddings`.
        _EMBEDDINGS_READY = False
        return _pure_bm25_search

    _EMBEDDINGS_READY = True
    return hybrid_search


_EMBEDDINGS_READY: bool | None = None  # None = not yet decided this process


def _pure_bm25_search(query: str, top_k: int) -> list[dict]:
    return bm25.score_query(query, top_k=top_k)


def hybrid_search(
    query: str,
    top_k: int = config.DEFAULT_TOP_K,
    bm25_weight: float = config.HYBRID_BM25_WEIGHT,
    embed_weight: float = config.HYBRID_EMBED_WEIGHT,
) -> list[dict]:
    """
    Hybrid BM25 + embedding search (Day 6).

    Combines BM25 ranking with cosine similarity between the query
    embedding and each candidate chunk's embedding. Final score is
    a weighted sum of the two, each normalized to [0, 1] by dividing
    by its own top-1 score (so neither dominates by absolute magnitude).

    Falls back to pure BM25 if:
      - the embedding model is not loaded (network / install issue)
      - the corpus has no embeddings yet
    """
    import numpy as np

    # Overfetch from BM25 so the embedding layer has a meaningful
    # candidate set to re-rank. 3x is enough headroom in practice —
    # any hit that didn't make BM25's top-3k is very unlikely to
    # beat a top-1k semantic match.
    overfetch = max(top_k * 3, 30)
    bm25_hits = bm25.score_query(query, top_k=overfetch)
    if not bm25_hits:
        return []

    if not embedding.is_available():
        return bm25_hits[:top_k]

    q_vec = embedding.embed_texts([query])
    if q_vec is None or len(q_vec) == 0:
        return bm25_hits[:top_k]

    chunk_ids, vectors = storage.get_all_embeddings(config.EMBEDDING_MODEL)
    if len(chunk_ids) == 0:
        return bm25_hits[:top_k]
    id_to_idx = {cid: i for i, cid in enumerate(chunk_ids)}

    # Compute cosine sim (we normalize embeddings at encode time, so
    # dot product == cosine similarity for our use case).
    bm25_scores = np.array([h["score"] for h in bm25_hits], dtype="float32")
    sims = np.zeros(len(bm25_hits), dtype="float32")
    for i, h in enumerate(bm25_hits):
        idx = id_to_idx.get(h["chunk_id"])
        if idx is not None:
            sims[i] = float(np.dot(vectors[idx], q_vec[0]))

    # Min-max normalize each component to [0, 1] so neither dominates
    # by absolute magnitude. Use a tiny epsilon to avoid div-by-zero.
    def _norm(arr):
        lo, hi = float(arr.min()), float(arr.max())
        if hi - lo < 1e-6:
            return np.zeros_like(arr)
        return (arr - lo) / (hi - lo)

    bm25_norm = _norm(bm25_scores)
    sim_norm = _norm(sims)

    final = bm25_weight * bm25_norm + embed_weight * sim_norm

    # Sort by final score desc; return top_k.
    order = np.argsort(-final)
    out = []
    for i in order[:top_k]:
        hit = dict(bm25_hits[i])  # copy so we don't mutate bm25's rows
        hit["score"] = float(final[i])
        hit["bm25_score"] = float(bm25_scores[i])
        hit["embed_score"] = float(sims[i])
        out.append(hit)
    return out


def _truncate(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return text[:n].rstrip() + "…"


def render(hits: list[dict], max_chars_per_chunk: int = 240, stream=None) -> None:
    """
    Pretty-print hits to a stream (stdout by default). Each hit shows:
      - rank + score
      - source: filename + page (or '-' for markdown)
      - chunk text (truncated)
    """
    import sys

    out = stream or sys.stdout

    if not hits:
        print("No hits.", file=out)
        return

    print(f"Top {len(hits)} hit(s):", file=out)
    for i, h in enumerate(hits, start=1):
        page = f"p.{h['page_num']}" if h.get("page_num") is not None else "md"
        source_line = f"  [{i}] {h['filename']} ({h['source_type']}, {page})  score={h['score']:.3f}"
        print(source_line, file=out)
        snippet = _truncate(h["chunk_text"].replace("\n", " "), max_chars_per_chunk)
        print(f"      {snippet}", file=out)
        print("", file=out)


def format_compact(hits: list[dict], max_chars: int = 600) -> str:
    """
    Single-block format suitable for an IM reply (later, when we wire it up).
    Pure formatting — no LLM, no paraphrase.
    """
    if not hits:
        return "(no matching content in the knowledge base)"
    lines: list[str] = []
    for i, h in enumerate(hits, start=1):
        page = f"p.{h['page_num']}" if h.get("page_num") is not None else "md"
        snippet = _truncate(h["chunk_text"].replace("\n", " "), max_chars)
        lines.append(f"[{i}] {h['filename']} ({page}) — {snippet}")
    return "\n".join(lines)