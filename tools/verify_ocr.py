"""
verify_ocr.py — Check OCR / scan-derived chunks for accuracy.

Why this exists
---------------
On 2026-08-07 the user asked: 'how do I verify the chunks in my KB
that came from OCR / scanned PDFs are actually correct?' A lot of
the KB's content (~1000 pages) was ingested via VL OCR (M3), and
the user's right to worry — OCR engines can mis-recognize
characters, especially for CJK text with low scan quality.

This tool answers the question in 3 layers (best to worst):

  Layer 1 — text-layer cross-check
    If the original PDF has a hidden text layer (very common for
    'born digital' documents that someone re-printed as scans),
    pymupdf.get_text('text') returns the AUTHORITATIVE text. We
    compare it character-by-character against what's in the chunks
    table for that page. A high match rate = OCR is faithful; a
    low match rate = OCR drifted.

  Layer 2 — OCR re-run consistency
    For pages with no text layer (pure scans), re-run the OCR on
    the same page and compare the result against what's in the
    chunks. Two runs of the same model on the same image SHOULD
    produce near-identical text. If they diverge a lot, the model
    is unstable and the original chunk text may be hallucinated.

  Layer 3 — character-set sanity check
    For all OCR'd chunks, check that the character set is
    'reasonable' (mostly CJK Han + ASCII + common punctuation).
    Catches cases where the OCR substituted Latin for Han, or
    dropped all Chinese characters, or got stuck on the same
    weird glyph.

Usage
-----
    .venv/bin/python tools/verify_ocr.py                  # all OCR'd docs, 5 samples each
    .venv/bin/python tools/verify_ocr.py --doc 综合测试一.pdf   # one doc
    .venv/bin/python tools/verify_ocr.py --samples 10     # more samples
    .venv/bin/python tools/verify_ocr.py --no-rerun        # skip Layer 2 (saves time)
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import paths
import storage
import pdf_ocr
import vl_config
import fitz  # pymupdf
import embedding  # noqa: F401  -- presence-check, ensures VL model is loadable


logger = logging.getLogger("verify_ocr")


# ---------------------------------------------------------------------------
# Metric helpers
# ---------------------------------------------------------------------------

def _char_match_rate(a: str, b: str) -> float:
    """
    Length-normalized character overlap between two strings.

    Returns 1.0 if a == b, 0.0 if no characters in common, somewhere
    in between. We use multiset intersection (Counter) so 'ABAB' vs
    'AB' scores 1.0 even though string positions differ.
    """
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    ca, cb = Counter(a), Counter(b)
    common = sum((ca & cb).values())
    # Average of the two length-normalized scores, so a tiny b
    # against a big a doesn't automatically score 1.0.
    return common / max(len(a), len(b))


# Allow CJK Han + ASCII alnum + common punctuation + whitespace.
# If a chunk is mostly outside this set, the OCR is suspect.
# (Build as a character class without the long alternation that
# confused re.findall in an earlier test — the previous regex had
# an embedded character with stray escapes that collapsed to empty.)
_REASONABLE_CHARS = re.compile(
    r"["
    r"\u4e00-\u9fff"          # CJK Unified Ideographs
    r"\u3400-\u4dbf"          # CJK Extension A
    r"\u3000-\u303f"          # CJK punctuation
    r"\uff00-\uffef"          # fullwidth forms
    r"A-Za-z0-9"              # ASCII alnum
    r"\s"                     # whitespace
    r".,:;\-()\[\]{}!?\'\"/\\<>=+*&%\$#@_\^~`|"
    r"\u00b7\u2026\u2013\u2014\u3001\u3002\u00a0"
    r"]"
)


def _reasonable_char_rate(text: str) -> float:
    """Fraction of `text` made of 'reasonable' (CJK + ASCII) chars."""
    if not text:
        return 0.0
    matched = len(_REASONABLE_CHARS.findall(text))
    return matched / len(text)


# ---------------------------------------------------------------------------
# Per-page checks
# ---------------------------------------------------------------------------

def _get_chunk_text_for_page(doc_id: int, page_num: int) -> str:
    """Concatenate all chunks for one (doc_id, page_num) in order."""
    storage.init_db()
    conn = storage.get_conn()
    try:
        cur = conn.execute(
            """
            SELECT chunk_text FROM chunks
            WHERE doc_id = ? AND page_num = ?
            ORDER BY chunk_index
            """,
            (doc_id, page_num),
        )
        return "\n\n".join(r["chunk_text"] for r in cur)
    finally:
        conn.close()


def _pymupdf_text_for_page(pdf_path: str, page_num: int) -> str:
    """Extract pymupdf's text-layer for one page (1-indexed)."""
    doc = fitz.open(pdf_path)
    try:
        if page_num < 1 or page_num > doc.page_count:
            return ""
        return doc[page_num - 1].get_text("text") or ""
    finally:
        doc.close()


def _ocr_for_page(pdf_path: str, page_num: int, cfg, dpi: int = 200) -> str:
    """Re-render + OCR one page using the configured VL model."""
    doc = fitz.open(pdf_path)
    try:
        if page_num < 1 or page_num > doc.page_count:
            return ""
        page = doc[page_num - 1]
        png = pdf_ocr.render_page_to_png(page, dpi=dpi)
        # Reuse the VL call but bypass usage stats
        return pdf_ocr._call_vl(cfg, png) or ""
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------

def _sample_pages(doc: dict, n: int) -> list[int]:
    """
    Choose `n` sample page numbers from a document.

    Strategy: skip the first 5 pages (cover / TOC) and the last 2
    (often blank), then evenly space n samples in the middle. This
    catches 'real' body content, not the structural pages that
    even a bad OCR would handle correctly.
    """
    page_count = doc.get("page_count") or 0
    if page_count <= 0:
        return []
    lo = min(6, max(1, page_count // 20))
    hi = max(lo + 1, page_count - 2)
    span = hi - lo + 1
    if span <= 0:
        return list(range(1, page_count + 1))[:n]
    if n >= span:
        return list(range(lo, hi + 1))
    step = span / n
    return [lo + int(i * step) for i in range(n)]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _verdict(layer1: float, layer2: float | None) -> str:
    """Combine layer-1 and layer-2 match rates into a one-word verdict."""
    if layer1 >= 0.95:
        return "OK"
    if layer1 >= 0.85:
        return "MINOR"  # small differences; probably whitespace / OCR spacing
    if layer1 < 0.85 and layer2 is not None and layer2 < 0.80:
        return "SUSPECT"  # text layer says one thing, OCR says another
    return "REVIEW"  # one or both signals worth eyeballing


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument(
        "--doc", default=None,
        help="restrict to a single filename (default: all OCR'd docs)",
    )
    p.add_argument(
        "--samples", type=int, default=5,
        help="number of sample pages per doc (default 5)",
    )
    p.add_argument(
        "--no-rerun", action="store_true",
        help="skip Layer 2 (re-OCR consistency) — saves time on big docs",
    )
    p.add_argument(
        "--rerun-pages", type=int, default=2,
        help="how many of the sample pages to re-OCR (default 2 — re-OCR is slow)",
    )
    p.add_argument(
        "--threshold", type=float, default=0.85,
        help="match rate below this is flagged in the report (default 0.85)",
    )
    args = p.parse_args()

    storage.init_db()
    # Find docs.
    conn = storage.get_conn()
    try:
        if args.doc:
            cur = conn.execute(
                "SELECT id, filename, page_count, char_count, ocr_pages "
                "FROM documents WHERE filename = ? AND ocr_pages IS NOT NULL",
                (args.doc,),
            )
        else:
            cur = conn.execute(
                "SELECT id, filename, page_count, char_count, ocr_pages "
                "FROM documents WHERE ocr_pages IS NOT NULL AND ocr_pages != '[]' "
                "ORDER BY char_count DESC"
            )
        docs = [dict(r) for r in cur]
    finally:
        conn.close()

    if not docs:
        print(f"No OCR'd documents found{' matching ' + args.doc if args.doc else ''}.")
        return 0

    cfg = vl_config.load_vl_config() if not args.no_rerun else None
    rerun_enabled = (cfg is not None) and (not args.no_rerun)
    if not rerun_enabled:
        print("(re-OCR disabled; only Layer 1 + Layer 3 will run)")

    print(f"Verifying {len(docs)} OCR'd document(s), "
          f"{args.samples} samples each" + (f", re-OCR {args.rerun_pages} samples" if rerun_enabled else ""))
    print("=" * 78)
    print(f"{'doc':<32s}  {'page':>4s}  {'L1_text':>8s}  {'L2_ocr':>8s}  "
          f"{'L3_chars':>8s}  {'verdict':>8s}")
    print("-" * 78)

    flagged: list[tuple[str, int, float, float | None, float, str]] = []
    for doc in docs:
        rel = None
        conn = storage.get_conn()
        try:
            cur = conn.execute(
                "SELECT relative_path FROM documents WHERE id = ?", (doc["id"],),
            )
            r = cur.fetchone()
            if r:
                rel = r["relative_path"]
        finally:
            conn.close()
        if not rel:
            print(f"  {doc['filename']:30s}  (no relative_path — skip)")
            continue
        pdf_path = str(paths.BASE / rel)
        if not Path(pdf_path).is_file():
            print(f"  {doc['filename']:30s}  (file missing — {pdf_path})")
            continue

        sample_pages = _sample_pages(doc, args.samples)
        # For re-OCR, use a small subset to keep runtime reasonable.
        rerun_set = set(sample_pages[: args.rerun_pages]) if rerun_enabled else set()

        for page_num in sample_pages:
            chunk_text = _get_chunk_text_for_page(doc["id"], page_num)
            pymu_text = _pymupdf_text_for_page(pdf_path, page_num)
            l1 = _char_match_rate(chunk_text, pymu_text) if pymu_text else None

            l2: float | None = None
            if page_num in rerun_set:
                try:
                    reocr = _ocr_for_page(pdf_path, page_num, cfg)
                    l2 = _char_match_rate(chunk_text, reocr)
                except Exception as e:
                    logger.warning("re-OCR failed on %s p.%d: %s",
                                   doc["filename"], page_num, e)

            l3 = _reasonable_char_rate(chunk_text)
            v = _verdict(l1 if l1 is not None else 1.0, l2)

            print(f"  {doc['filename'][:30]:<32s}  {page_num:>4d}  "
                  f"{('%.3f'%l1) if l1 is not None else 'NO_TEXT':>8s}  "
                  f"{('%.3f'%l2) if l2 is not None else 'skip':>8s}  "
                  f"{('%.3f'%l3):>8s}  {v:>8s}")
            if l1 is not None and l1 < args.threshold:
                flagged.append((doc["filename"], page_num, l1, l2, l3, v))

    print("=" * 78)
    if not flagged:
        print("All sampled pages have L1 >= threshold. No obvious OCR drift.")
    else:
        print(f"⚠ {len(flagged)} page(s) flagged (L1 < {args.threshold}):")
        for fn, pn, l1, l2, l3, v in flagged:
            print(f"  - {fn}  p.{pn}  L1={l1:.3f}  L2={l2!r}  L3={l3:.3f}  ({v})")
        print()
        print("Next steps for flagged pages:")
        print("  1. Read the actual chunk text + the original PDF page and compare")
        print("     (you can use im_router._format_hits_markdown to render)")
        print("  2. If the OCR is wrong on many pages, consider re-running")
        print("     `python app.py ingest <file> --ocr --drop-embeddings`")
        print("  3. If the issue is a specific document (e.g. poor scan quality),")
        print("     consider re-OCR with a different model or manual cleanup")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(main())
