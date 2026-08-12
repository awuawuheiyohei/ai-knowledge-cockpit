"""
show_chunk.py — Side-by-side chunk ↔ source PDF page viewer.

What this is for
----------------
The KB has ~17K chunks. After OCR / re-chunking, you want to
spot-check a few: "is the text actually what the source page says,
or did the OCR mess it up?" This tool makes that check one
command.

Usage
-----
    .venv/bin/python tools/show_chunk.py                     # one random chunk
    .venv/bin/python tools/show_chunk.py --doc 域1.pdf        # random chunk from that doc
    .venv/bin/python tools/show_chunk.py --page 35 --doc foo.pdf  # specific page
    .venv/bin/python tools/show_chunk.py --chunk-id 47069     # exact chunk by id
    .venv/bin/python tools/show_chunk.py --worst 10            # 10 lowest-coverage chunks

Output
------
A side-by-side rendering:
  ┌─ source PDF page text (pymupdf native, ground truth) ─┐
  │ ...                                                    │
  └────────────────────────────────────────────────────────┘
  ┌─ KB chunk text (what the bot sees) ────────────────────┐
  │ ...                                                    │
  └────────────────────────────────────────────────────────┘
  match rate: 0.78   char diff: 12

This is a terminal tool, not a UI. For documents where pymupdf
returns no text (true scans), only the chunk side is shown, and
you'll need to open the PDF in a viewer separately.
"""
from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import paths
import storage
import fitz  # pymupdf
import re


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

def _resolve_chunk(chunk_id: int) -> dict:
    storage.init_db()
    conn = storage.get_conn()
    try:
        cur = conn.execute(
            """
            SELECT c.id, c.doc_id, c.page_num, c.chunk_text, c.via_ocr,
                   d.filename, d.relative_path
              FROM chunks c JOIN documents d ON c.doc_id = d.id
             WHERE c.id = ?
            """,
            (chunk_id,),
        )
        r = cur.fetchone()
        return dict(r) if r else {}
    finally:
        conn.close()


def _random_chunk(doc_filter: str | None = None) -> dict:
    storage.init_db()
    conn = storage.get_conn()
    try:
        if doc_filter:
            cur = conn.execute(
                """
                SELECT c.id FROM chunks c JOIN documents d ON c.doc_id = d.id
                 WHERE d.filename LIKE ?
                 ORDER BY RANDOM() LIMIT 1
                """,
                (f"%{doc_filter}%",),
            )
        else:
            cur = conn.execute("SELECT id FROM chunks ORDER BY RANDOM() LIMIT 1")
        r = cur.fetchone()
    finally:
        conn.close()
    return _resolve_chunk(r["id"]) if r else {}


def _chunk_at_page(doc_filter: str, page_num: int) -> dict:
    storage.init_db()
    conn = storage.get_conn()
    try:
        cur = conn.execute(
            """
            SELECT c.id FROM chunks c JOIN documents d ON c.doc_id = d.id
             WHERE d.filename LIKE ? AND c.page_num = ?
             ORDER BY c.chunk_index LIMIT 1
            """,
            (f"%{doc_filter}%", page_num),
        )
        r = cur.fetchone()
    finally:
        conn.close()
    return _resolve_chunk(r["id"]) if r else {}


def _worst_chunks(n: int) -> list[dict]:
    """
    Find chunks most likely to be OCR-broken. Heuristic: chunks that
    contain long runs of Latin characters where CJK should be
    (typical OCR mis-recognition artifact), or repeated short
    fragments suggesting a stuck loop.
    """
    storage.init_db()
    conn = storage.get_conn()
    try:
        # Pull a sample of 500 chunks and score them.
        cur = conn.execute(
            """
            SELECT c.id, c.chunk_text, d.filename, c.page_num
              FROM chunks c JOIN documents d ON c.doc_id = d.id
             WHERE length(c.chunk_text) > 100
             ORDER BY RANDOM() LIMIT 500
            """
        )
        rows = [dict(r) for r in cur]
    finally:
        conn.close()

    def _score(text: str) -> float:
        """Lower score = more suspect."""
        if not text:
            return 0.0
        # CJK Han ratio
        cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
        cjk_ratio = cjk / len(text)
        # Penalize very long runs of single-character Latin (e.g. 'a a a a')
        repeats = len(re.findall(r"(.)\1{5,}", text))
        # Penalize excessive whitespace
        ws = text.count("  ") + text.count("\n\n\n")
        return cjk_ratio - 0.1 * repeats - 0.01 * ws

    rows.sort(key=lambda r: _score(r["chunk_text"]))
    return [r for r in rows[:n]]


# ---------------------------------------------------------------------------
# Source-page text extraction
# ---------------------------------------------------------------------------

def _source_text(rel_path: str, page_num: int | None) -> str:
    if page_num is None or not rel_path:
        return "(no source page available — this chunk has no page_num)"
    pdf_path = paths.BASE / rel_path
    if not pdf_path.is_file():
        return f"(source PDF not found: {pdf_path})"
    try:
        doc = fitz.open(str(pdf_path))
    except Exception as e:
        return f"(failed to open PDF: {e})"
    try:
        if page_num < 1 or page_num > doc.page_count:
            return f"(page {page_num} out of range 1..{doc.page_count})"
        return doc[page_num - 1].get_text("text") or ""
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# Side-by-side rendering
# ---------------------------------------------------------------------------

def _char_diff(a: str, b: str) -> int:
    """Approximate: how many characters appear in one but not the other."""
    sa, sb = set(a), set(b)
    return len(sa.symmetric_difference(sb))


def _show_chunk(chunk: dict, width: int = 78) -> None:
    print("=" * width)
    print(f"chunk_id:   {chunk['id']}")
    print(f"document:   {chunk['filename']}")
    print(f"page:       {chunk.get('page_num')}")
    print(f"via_ocr:    {bool(chunk.get('via_ocr'))}")
    print(f"chunk len:  {len(chunk['chunk_text'])} chars")
    print("=" * width)
    src = _source_text(chunk.get("relative_path", ""), chunk.get("page_num"))
    if src and not src.startswith("("):
        match = _simple_match_rate(chunk["chunk_text"], src)
        print(f"  ↪ pymupdf text-layer match: {match:.3f}")
    print()

    print(f"┌─ source PDF page (pymupdf text mode, ground truth) {'─' * 4}┐")
    for line in src.splitlines() or ["(empty)"]:
        for chunk_str in _wrap(line, width - 4):
            print(f"│ {chunk_str:<{width - 4}s} │")
    print(f"└{'─' * (width - 2)}┘")
    print()

    print(f"┌─ KB chunk (what the bot sees) {'─' * 28}┐")
    for line in chunk["chunk_text"].splitlines() or ["(empty)"]:
        for chunk_str in _wrap(line, width - 4):
            print(f"│ {chunk_str:<{width - 4}s} │")
    print(f"└{'─' * (width - 2)}┘")
    print()

    if src and not src.startswith("("):
        diff = _char_diff(chunk["chunk_text"], src)
        print(f"char-set diff: {diff}  "
              f"(lower is better; high diff = OCR substituted/lost characters)")
    print()


def _wrap(line: str, width: int) -> list[str]:
    if len(line) <= width:
        return [line]
    out = []
    while line:
        out.append(line[:width])
        line = line[width:]
    return out


def _simple_match_rate(a: str, b: str) -> float:
    """Length-normalized multiset intersection of characters."""
    if not a or not b:
        return 0.0
    from collections import Counter
    ca, cb = Counter(a), Counter(b)
    common = sum((ca & cb).values())
    return common / max(len(a), len(b))


def _show_worst_summary(chunks: list[dict], width: int = 78) -> None:
    print("=" * width)
    print(f"Worst {len(chunks)} chunks (lowest CJK ratio, most repeats, etc.)")
    print("=" * width)
    for r in chunks:
        cjk = sum(1 for c in r["chunk_text"] if "\u4e00" <= c <= "\u9fff")
        cjk_ratio = cjk / max(1, len(r["chunk_text"]))
        rep = len(re.findall(r"(.)\1{5,}", r["chunk_text"]))
        print(
            f"  chunk_id={r['id']:>6}  p.{r['page_num']:<4}  "
            f"cjk={cjk_ratio:.2%}  repeats={rep:>3}  "
            f"{r['filename'][:40]}"
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--chunk-id", type=int, default=None,
                   help="exact chunk id to show")
    p.add_argument("--doc", default=None,
                   help="filter by filename (substring match)")
    p.add_argument("--page", type=int, default=None,
                   help="with --doc, pick the chunk on this page")
    p.add_argument("--worst", type=int, default=0,
                   help="list the N most-suspect chunks (no side-by-side)")
    args = p.parse_args()

    if args.worst:
        worst = _worst_chunks(args.worst)
        _show_worst_summary(worst)
        return 0

    if args.chunk_id is not None:
        chunk = _resolve_chunk(args.chunk_id)
    elif args.doc and args.page is not None:
        chunk = _chunk_at_page(args.doc, args.page)
    elif args.doc:
        chunk = _random_chunk(args.doc)
    else:
        chunk = _random_chunk()

    if not chunk:
        print("No matching chunk found.")
        return 1
    _show_chunk(chunk)
    return 0


if __name__ == "__main__":
    sys.exit(main())
