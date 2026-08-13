"""
reextract_test_docs.py — Pull clean per-page text from the test PDFs.

The KB chunks for these docs are chunked AND have cross-chunk line duplication,
which makes question parsing fragile. This re-extracts each page via the OCR
pipeline (or pymupdf text for non-scanned pages) and writes JSON to
data/reextracted/<doc>.json with one entry per page.

Usage: python tools/reextract_test_docs.py [--docs "综合测试一.pdf,综合测试二.pdf"]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import paths  # noqa: E402
import storage  # noqa: E402
import pdf_extract  # noqa: E402
import pdf_ocr  # noqa: E402
import vl_config  # noqa: E402


def reextract(filename: str, cfg) -> list[dict]:
    rel = next(
        (d["relative_path"] for d in storage.list_documents() if d["filename"] == filename),
        None,
    )
    if not rel:
        print(f"  skip {filename}: not in paths", file=sys.stderr)
        return []
    src = paths.BASE / rel
    if not src.is_file():
        print(f"  skip {filename}: missing at {src}", file=sys.stderr)
        return []

    def cb(page, page_num):
        return pdf_ocr.ocr_page(page, page_num, cfg, pdf_ocr.OcrUsage())

    result = pdf_extract.extract_pdf(str(src), ocr_callback=cb)
    pages = []
    for p in result.pages:
        pages.append({
            "page_num": p.page_num,
            "text": p.text,
            "is_scanned": p.is_scanned,
            "via_ocr": p.via_ocr,
        })
    return pages


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", default="综合测试一.pdf,综合测试二.pdf,综合测试三.pdf,综合测试四.pdf")
    ap.add_argument("--out", default="data/reextracted")
    args = ap.parse_args()

    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        cfg = vl_config.load_vl_config()
    except Exception as e:
        print(f"ERROR: VL config not available: {e}", file=sys.stderr)
        return 1

    docs = [d.strip() for d in args.docs.split(",") if d.strip()]
    for doc in docs:
        print(f"[{doc}] re-extracting...")
        pages = reextract(doc, cfg)
        out_path = out_dir / f"{Path(doc).stem}.json"
        out_path.write_text(json.dumps(pages, ensure_ascii=False, indent=2))
        n_chars = sum(len(p["text"]) for p in pages)
        n_ocr = sum(1 for p in pages if p.get("via_ocr"))
        print(f"  wrote {len(pages)} pages ({n_chars} chars, {n_ocr} via OCR) to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
