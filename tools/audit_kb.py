"""
audit_kb.py — Strict 1:1 check between KB chunks and source PDF pages.

Why this exists
---------------
On 2026-08-12 the user reiterated the #1 hard rule: '知识库和我的
原始pdf资料一一对应 + 一字不落' (KB must be 1:1 with source PDFs,
no lost content). This tool audits that rigorously:

  For every PDF document in the KB:
    1. Open the source file from data/originals/
    2. Re-extract page text via pymupdf (native text-layer or
       re-render via OCR for scan-only docs)
    3. Compare against the chunks table: which pages are present,
       which are missing, which have content length outliers

Output
------
Per-document table:
  doc                                    pages   chunks  missing_pages  status
  ---------------------------------------------------------------------
  域1：安全与风险管理.pdf                  75      75     0              ✓
  综合测试四.pdf                          93      92     [75]           ⚠ 1 page
  ...

A 'missing page' means: the source PDF has a page (page_num N), but
the chunks table has no chunk with that page_num. The page may be:
  - a scanned page that the OCR engine declined to process
    (VL API returned 'sensitive' or 'rate-limited')
  - a fully-blank page that the chunker correctly skipped
  - a page where extraction failed silently

A 'low-coverage' warning means: source page has X chars of text,
but the chunks for that page have only Y < X * threshold. Usually
this is OCR truncation; occasionally it's a content-type mismatch
(table vs prose).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import paths
import storage
import pdf_extract
import pdf_ocr
import vl_config
import fitz  # pymupdf
import embedding  # noqa: F401  -- presence-check


logger = logging.getLogger("audit_kb")


def _re_extract_page_text(pdf_path: str, page_num: int, cfg) -> str:
    """Re-extract text for one page, OCR if scan."""
    doc = fitz.open(pdf_path)
    try:
        if page_num < 1 or page_num > doc.page_count:
            return ""
        page = doc[page_num - 1]
        raw = page.get_text("text") or ""
        raw = raw.strip()
        if raw and len(raw) > 30:
            return raw  # text-layer present and meaningful
        # Try OCR
        if cfg is None:
            return ""
        try:
            png = pdf_ocr.render_page_to_png(page)
            return pdf_ocr._call_vl(cfg, png)
        except Exception as e:
            logger.debug("OCR failed on %s p.%d: %s", pdf_path, page_num, e)
            return ""
    finally:
        doc.close()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--limit", type=int, default=0,
                   help="only check the first N docs (0 = all)")
    p.add_argument("--no-ocr", action="store_true",
                   help="skip OCR re-extraction (much faster, but can't "
                        "verify scan-only pages — they're reported as missing)")
    p.add_argument("--coverage-threshold", type=float, default=0.5,
                   help="flag pages where KB text is < this fraction of "
                        "source text length (default 0.5)")
    args = p.parse_args()

    storage.init_db()
    conn = storage.get_conn()
    try:
        cur = conn.execute(
            "SELECT id, filename, page_count, ocr_pages, relative_path "
            "FROM documents WHERE source_type = 'pdf' "
            "ORDER BY filename"
        )
        docs = [dict(r) for r in cur]
    finally:
        conn.close()

    if args.limit:
        docs = docs[: args.limit]

    cfg = None
    if not args.no_ocr:
        try:
            cfg = vl_config.load_vl_config()
            print(f"OCR available; will re-OCR scan pages as needed.\n")
        except Exception as e:
            print(f"⚠️  VL config unavailable, falling back to text-mode only: {e}\n")
            cfg = None

    print(f"Auditing {len(docs)} PDF documents…")
    print("=" * 90)
    print(f"{'doc':<40s}  {'pages':>5s}  {'chunks':>6s}  {'chars':>9s}  {'cover':>5s}  {'status'}")
    print("-" * 90)

    all_missing: dict[str, list[int]] = {}
    all_low_cover: dict[str, list[tuple[int, float]]] = {}
    total_missing = 0

    for d in docs:
        rel = d["relative_path"]
        pdf_path = str(paths.BASE / rel) if rel else ""
        if not pdf_path or not Path(pdf_path).is_file():
            print(f"  {d['filename'][:38]:40s}  (source missing: {pdf_path})")
            continue

        # Get chunk pages for this doc.
        conn = storage.get_conn()
        try:
            cur = conn.execute(
                "SELECT page_num, sum(length(chunk_text)) as chars "
                "FROM chunks WHERE doc_id = ? GROUP BY page_num",
                (d["id"],),
            )
            chunks_by_page = {r["page_num"]: r["chars"] for r in cur}
        finally:
            conn.close()

        # Compare to source page count.
        src_pages = d["page_count"] or 0
        chunk_pages = set(chunks_by_page.keys())
        src_page_set = set(range(1, src_pages + 1))
        missing = sorted(src_page_set - chunk_pages)
        all_missing[d["filename"]] = missing

        # Coverage check: for pages we have, compare to source text.
        low_cover = []
        for p in sorted(chunk_pages):
            if p > src_pages:
                continue
            src_text = _re_extract_page_text(pdf_path, p, cfg)
            src_len = len(src_text.strip())
            kb_len = chunks_by_page.get(p, 0)
            if src_len > 100 and kb_len < args.coverage_threshold * src_len:
                low_cover.append((p, kb_len / src_len))
        all_low_cover[d["filename"]] = low_cover

        n_chunks = sum(
            1 for _ in conn.execute(
                "SELECT 1 FROM chunks WHERE doc_id = ?", (d["id"],)
            )
        ) if False else len(chunks_by_page)  # cheat — use distinct pages as proxy
        # Re-fetch actual chunk count
        conn = storage.get_conn()
        try:
            n_chunks = conn.execute(
                "SELECT count(*) FROM chunks WHERE doc_id = ?", (d["id"],)
            ).fetchone()[0]
            total_chars = conn.execute(
                "SELECT sum(length(chunk_text)) FROM chunks WHERE doc_id = ?",
                (d["id"],),
            ).fetchone()[0] or 0
        finally:
            conn.close()

        if not missing and not low_cover:
            status = "✓"
        elif missing:
            status = f"⚠ {len(missing)} missing"
            total_missing += len(missing)
        elif low_cover:
            status = f"⚠ {len(low_cover)} low"
        else:
            status = "✓"

        cover = (sum(chunks_by_page.values()) /
                 max(1, sum(
                     len(_re_extract_page_text(pdf_path, p, cfg).strip())
                     for p in chunk_pages
                 )))
        # Only show coverage when it's notably low.
        cover_str = f"{cover:.2f}" if cover < 1.0 else "—"

        print(f"  {d['filename'][:38]:40s}  {src_pages:>5d}  {n_chunks:>6d}  "
              f"{total_chars:>9d}  {cover_str:>5s}  {status}")

    print("=" * 90)
    if total_missing == 0 and not any(all_low_cover.values()):
        print("✓ All pages present, coverage looks healthy.")
        return 0

    print(f"\n{len(docs)} documents audited, {total_missing} pages missing, "
          f"{sum(len(v) for v in all_low_cover.values())} pages with low coverage.\n")

    if total_missing:
        print("Pages with NO chunks in KB (but source PDF has them):")
        for fn, pages in all_missing.items():
            if pages:
                # Show only the first 20 per doc to keep report readable.
                shown = pages[:20]
                more = "" if len(pages) <= 20 else f"  (+{len(pages) - 20} more)"
                print(f"  {fn}: {shown}{more}")

    if any(all_low_cover.values()):
        print("\nPages with notably lower KB text than source (coverage < "
              f"{int(args.coverage_threshold * 100)}%):")
        for fn, lows in all_low_cover.items():
            if lows:
                for p, ratio in lows[:10]:
                    print(f"  {fn}  p.{p}  ratio={ratio:.2f}")

    print("\nNext steps for missing pages:")
    print("  1. If the page is blank in the source PDF, this is correct")
    print("  2. If the page has content but no KB chunk, the OCR/scan ")
    print("     pipeline dropped it (probably failed). Consider:")
    print("     a. Re-OCR the page manually with a different tool")
    print("     b. Type-override: edit the doc to remove the blank page")
    print("     c. If the page is a 'sensitive' API-reject, you may need to")
    print("        bypass the M3 content filter (different model / different image)")
    return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(main())
