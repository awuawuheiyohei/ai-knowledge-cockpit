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


def _dedupe_consecutive_paragraphs(paragraphs: list[str]) -> list[str]:
    """
    Drop paragraphs that are exact duplicates of an earlier paragraph
    in the same input list. Used to defend against OCR engines (and
    some pymupdf quirks) that emit the same line twice in a row.

    Why this lives in the chunker rather than the OCR module
    ---------------------------------------------------------
    The dedup is a content-level decision, not a per-call one: even
    if we re-OCR the same PDF page with a different model, the
    same defense is appropriate. Keeping it here means every code
    path that calls `chunk_text()` (PDF via ocr, PDF via native text,
    Markdown) benefits.

    Why "consecutive"
    ----------------
    We only drop a paragraph if its *predecessor* is identical. This
    is the right call for the failure mode we observed (2026-08-07):
    M3 OCR of the OSG10 中英对照版 PDF emitted each term pair
    twice in a row (e.g. "中文\n中文\nEnglish"). Non-consecutive
    dups are left alone — they may be intentional repetition in the
    source (a checklist that mentions the same step twice, etc.)
    and dropping them could lose real content.
    """
    if not paragraphs:
        return paragraphs
    out: list[str] = [paragraphs[0]]
    for p in paragraphs[1:]:
        if p == out[-1]:
            continue
        out.append(p)
    return out


def _dedupe_repeated_line_blocks(text: str, min_block: int = 2) -> str:
    """
    Detect and drop repeated line-blocks in `text`.

    Failure mode this addresses
    ----------------------------
    M3 OCR on the OSG10 中英对照版 PDF (2026-08-07) emitted each
    term pair twice, with the duplication happening **across the
    paragraph break** — i.e.:

        tation\n
        PDF\n
        at\n
        media.defcon.org/.../\n
        tation\n     ← repeat starts here
        PDF\n
        at\n
        media.defcon.org/.../\n

    Notice the lines are separated by SINGLE newlines, not blank
    lines, so `_split_paragraphs` (which only splits on blank lines)
    treated the whole thing as one paragraph. The paragraph-level
    dedup couldn't see the duplication because the individual lines
    ("tation", "PDF", "at", "media...") are not equal to each other.

    What this does
    --------------
    Split the input on any newline, then slide a window looking for
    a run of N consecutive lines (N >= min_block) that is *exactly
    repeated* later in the text. When found, drop the second copy.

    Constraints (to be safe)
    ------------------------
    - Only matches runs of length >= min_block (default 2). A single
      accidentally-repeated line is left alone — too risky.
    - Requires the match to be at the same position in the line
      sequence (i.e. the repeated block is the EXACT same lines in
      the same order).
    - Returns the deduped text with single newlines preserved.
    """
    lines = text.split("\n")
    if len(lines) < 2 * min_block:
        return text

    n = len(lines)
    drop_mask = [False] * n
    i = 0
    while i < n - min_block:
        # Find the longest run starting at i that matches the same
        # number of lines starting somewhere later in the text.
        max_run = 0
        for j in range(i + min_block, n - min_block + 1):
            run = 0
            while (
                i + run < n
                and j + run < n
                and lines[i + run] == lines[j + run]
            ):
                run += 1
            if run >= min_block and run > max_run:
                max_run = run
                max_j = j
        if max_run >= min_block:
            for k in range(max_j, max_j + max_run):
                drop_mask[k] = True
            i = max_j + max_run  # skip past the dropped block
        else:
            i += 1
    return "\n".join(line for line, drop in zip(lines, drop_mask) if not drop)


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

    # Day 7 dedup (2026-08-07): drop consecutive identical paragraphs
    # so an OCR engine that emits the same line twice doesn't end up
    # doubling the chunk content. See _dedupe_consecutive_paragraphs.
    paras = _dedupe_consecutive_paragraphs(paras)

    # Also run a line-level dedup on the raw text — catches the
    # OSG10-中英对照版 failure mode where M3 OCR duplicated a run
    # of N lines (N >= 2) with single newlines (not blank lines),
    # so the paragraph-level dedup above couldn't see it.
    for i, p in enumerate(paras):
        deduped_p = _dedupe_repeated_line_blocks(p, min_block=2)
        if deduped_p != p:
            paras[i] = deduped_p

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