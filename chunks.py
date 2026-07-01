"""
chunks.py — Split text into retrieval-sized chunks.

Strategy: paragraph-boundary first, then character-boundary fallback.
Goal: each chunk should be a self-contained thought (~CHUNK_SIZE chars)
with a small overlap to the next so terms near a boundary don't get lost.

Pure stdlib. No LLM, no heuristics that need training data.
"""
from __future__ import annotations

import re

import config


_PARA_SPLIT = re.compile(r"\n\s*\n")


def _split_paragraphs(text: str) -> list[str]:
    """Split on blank lines, drop empties, trim each para."""
    raw = _PARA_SPLIT.split(text)
    return [p.strip() for p in raw if p.strip()]


def _split_hard(text: str, size: int, overlap: int) -> list[str]:
    """Last-resort character-based sliding window."""
    if len(text) <= size:
        return [text]
    step = max(1, size - overlap)
    out: list[str] = []
    i = 0
    while i < len(text):
        out.append(text[i : i + size])
        if i + size >= len(text):
            break
        i += step
    return out


def chunk_text(text: str) -> list[str]:
    """
    Split text into chunks. Each chunk is a string.

    Algorithm:
      1. Split into paragraphs on blank lines.
      2. Greedily accumulate paragraphs into a chunk until adding the next
         paragraph would exceed CHUNK_SIZE.
      3. When a single paragraph exceeds CHUNK_SIZE, fall back to the
         sliding-window splitter on that paragraph.
      4. Append an overlap slice from the previous chunk to the next, so
         cross-boundary terms stay findable.
    """
    text = (text or "").strip()
    if not text:
        return []

    size = config.CHUNK_SIZE
    overlap = config.CHUNK_OVERLAP

    paras = _split_paragraphs(text)
    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0

    def flush() -> None:
        nonlocal buf, buf_len
        if buf:
            chunks.append("\n\n".join(buf).strip())
            buf = []
            buf_len = 0

    for p in paras:
        if len(p) > size:
            # Flush whatever we had; hard-split the giant paragraph.
            flush()
            chunks.extend(_split_hard(p, size, overlap))
            continue

        add_len = len(p) + 2  # +2 for the "\n\n" joiner
        if buf and (buf_len + add_len) > size:
            flush()
        buf.append(p)
        buf_len += add_len

    flush()

    # Apply overlap: tail of each chunk is prepended to the next.
    if overlap > 0 and len(chunks) > 1:
        out: list[str] = []
        prev_tail = ""
        for i, c in enumerate(chunks):
            if prev_tail:
                # Avoid duplicating the whole chunk when overlap >= chunk size.
                head = prev_tail if len(prev_tail) < len(c) else ""
                if head:
                    out.append(head + "\n" + c)
                else:
                    out.append(c)
            else:
                out.append(c)
            prev_tail = c[-overlap:] if len(c) > overlap else c
        return out

    return chunks