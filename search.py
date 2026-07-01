"""
search.py — Query the KB and render results.

Layered design:
  - search(query, top_k, doc_filter): raw hits list.
  - render(hits, ...): pretty CLI formatting with source line per hit.
  - format_compact(...): single-line answer for chat/IM later.

No LLM involved. Each hit includes the original chunk text + provenance.
"""
from __future__ import annotations

import config
import bm25
import storage


def search(
    query: str,
    top_k: int = config.DEFAULT_TOP_K,
    filename: str | None = None,
) -> list[dict]:
    """
    Run a BM25 query. Optionally restrict to a single source filename.

    Returns the same dict shape as `bm25.score_query`.
    Empty list if no hits.
    """
    hits = bm25.score_query(query, top_k=top_k)
    if filename:
        hits = [h for h in hits if h["filename"] == filename]
    return hits


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