"""
pdf_extract.py — Extract text from PDFs using pymupdf (fitz).

Behavior
--------
- For text PDFs: returns a list of (page_num, text) tuples, 1-indexed.
- For scanned/image-only pages: text will be near-empty. By default
  we leave those pages marked as scanned and let the ingest pipeline
  decide whether to OCR them (controlled by --ocr flag).
- If `ocr_callback` is provided, it is invoked for each scanned page;
  the returned text replaces the (empty) page text and the page is
  flagged with `via_ocr=True`.

No LLM/OCR is performed here by default. The callback is the seam.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

import config


@dataclass
class PageExtract:
    page_num: int       # 1-indexed
    text: str           # raw extracted text, may be near-empty
    is_scanned: bool    # True if text looks too short to be real content
    via_ocr: bool = False  # True if `text` came from an OCR callback


@dataclass
class ExtractResult:
    pages: list[PageExtract]
    page_count: int
    scan_page_nums: list[int]   # 1-indexed page numbers that look scanned
    ocr_page_nums: list[int] = field(default_factory=list)


def _get_fitz():
    try:
        import fitz  # type: ignore
        return fitz
    except ImportError as e:
        raise RuntimeError(
            "pymupdf is required for PDF extraction. "
            "Install with: pip install pymupdf"
        ) from e


def _strip_text(raw: str) -> str:
    if not raw:
        return ""
    lines = []
    for ln in raw.splitlines():
        s = " ".join(ln.split())
        if s:
            lines.append(s)
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Layout-aware text extraction (Day 3, 2026-08-06)
# ---------------------------------------------------------------------------
# Why this exists
# ----------------
# Several PDFs in the KB (notably OSG9 中文版) are laid out in TWO
# columns per page. pymupdf's `page.get_text("text")` returns blocks
# in some internal order that does NOT respect column boundaries — so
# the resulting text is a mix of left-then-right paragraphs, which
# shreds the reading order and confuses BM25.
#
# What this does
# --------------
# 1. Pull all non-empty blocks via `page.get_text("blocks")` — each
#    block has a bbox (x0, y0, x1, y1) we can use for layout decisions.
# 2. Detect whether the page is 2-column (heuristic: in a y-band of
#    20px, do we have substantial text on BOTH sides of the page
#    mid-line?  At least 40% of multi-block bands must show this).
# 3. If single-column: return pymupdf's plain text (already in order).
# 4. If 2-column: split blocks into left/right by x-center, sort each
#    by y, and concatenate.  Full-width blocks (figures, table cells)
#    go to the top in their original y order so they aren't lost.

# Tunable: minimum block text length to count as "substantial" for
# the 2-column detector.  Anything shorter is likely a TOC dot leader,
# a page number, or a single-character glyph, all of which can sit on
# either side without implying a column split.
_LAYOUT_MIN_SUBSTANTIAL_CHARS = 20
# Y-banding tolerance for the detector.  20px is tight enough that we
# don't merge blocks from different paragraphs but loose enough to
# group together a heading and the first line of its body.
_LAYOUT_Y_BAND_PX = 20
# Threshold on the fraction of multi-block bands that show 2-col
# structure.  Below this we treat the page as single-column (avoid
# false positives from pages with one stray figure or TOC fragment).
_LAYOUT_2COL_BAND_FRACTION = 0.40


def _classify_layout(blocks: list, page_width: float) -> str:
    """
    Decide whether a page's blocks are single-column, two-column, or
    a mix (e.g. some full-width figures amid 2-col text).

    Returns one of: "single", "two-col", "mixed".

    The heuristic is deliberately conservative — a wrong "two-col"
    decision on a single-column page would break reading order much
    worse than letting a single-column page slip through the 2-col
    path on a couple of stray bands.
    """
    from collections import defaultdict

    # Quick path: every block spans the full text area → unambiguously
    # single-column. This handles most body pages of single-column PDFs.
    full_width_count = sum(
        1 for b in blocks if b[0] < 60 and b[2] > page_width - 30
    )
    if full_width_count == len(blocks):
        return "single"

    mid = page_width / 2
    bands: dict[int, list] = defaultdict(list)
    for b in blocks:
        bands[int(b[1] // _LAYOUT_Y_BAND_PX) * _LAYOUT_Y_BAND_PX].append(b)

    multi_band_count = 0
    two_col_band_count = 0
    for band_blocks in bands.values():
        if len(band_blocks) < 2:
            continue
        multi_band_count += 1
        left = [
            b for b in band_blocks
            if (b[0] + b[2]) / 2 < mid
            and len(b[4].strip()) > _LAYOUT_MIN_SUBSTANTIAL_CHARS
        ]
        right = [
            b for b in band_blocks
            if (b[0] + b[2]) / 2 >= mid
            and len(b[4].strip()) > _LAYOUT_MIN_SUBSTANTIAL_CHARS
        ]
        if left and right:
            two_col_band_count += 1

    if multi_band_count == 0:
        return "single"
    ratio = two_col_band_count / multi_band_count
    if ratio >= _LAYOUT_2COL_BAND_FRACTION:
        return "two-col"
    if ratio >= 0.15:
        return "mixed"
    return "single"


def _extract_text_layout_aware(page) -> str:
    """
    Get page text in proper reading order, handling 1/2-column layouts.

    Returns a string with paragraphs separated by blank lines, ready
    for chunking. The "text" mode is the fallback used for genuinely
    single-column pages (it preserves any inline figure-caption
    interleaving the PDF author intended, which our band-based
    approach would flatten).
    """
    width = page.rect.width
    try:
        blocks = page.get_text("blocks")
    except Exception:
        # Defensive — some pages may fail in blocks mode; fall back.
        return page.get_text("text") or ""

    # Drop empty blocks (e.g. pure-whitespace "blocks" that pymupdf
    # sometimes emits at the top of pages).
    blocks = [b for b in blocks if b[4].strip()]
    if not blocks:
        return page.get_text("text") or ""

    layout = _classify_layout(blocks, width)

    if layout == "single":
        # Plain text is already in good order; cheap fast path.
        return page.get_text("text") or ""

    # For "two-col" and "mixed" layouts, fall back to pymupdf's
    # own "text" mode. Our earlier hand-rolled left/right/full-width
    # split (2026-08-06) was supposed to do a better job than
    # pymupdf, but in practice it *garbled* reading order on
    # borderline pages (OSG9 page 100/220/320) where a paragraph
    # wraps across the page mid-line — the wrapped lines landed
    # in the wrong column. pymupdf's text mode does its own
    # physical-block walk and handles those cases correctly.
    #
    # This means layout-aware extraction now provides a small
    # speedup (avoid the per-block bookkeeping) and a *correctness*
    # improvement (no more garbled outputs) but no semantic
    # restructuring — pymupdf alone is good enough for our
    # retrieval purposes. Day 3's other contribution — fixing
    # "looks-like-scanned" detection — is unaffected.
    return page.get_text("text") or ""


def _looks_like_scanned(text: str) -> bool:
    s = (text or "").strip()
    if len(s) < config.SCAN_PAGE_MIN_CHARS:
        return True
    has_meaningful_char = any(
        ("A" <= ch <= "Z") or ("a" <= ch <= "z")
        or ("\u4e00" <= ch <= "\u9fff") or ("\u3400" <= ch <= "\u4dbf")
        for ch in s
    )
    return not has_meaningful_char


# Callback type: given a fitz.Page and its 1-indexed page_num, return
# the recognized text. Implementations are responsible for tracking their
# own usage stats.
OcrCallback = Callable[["fitz.Page", int], str]


def extract_pdf(
    file_path: str,
    ocr_callback: Optional[OcrCallback] = None,
) -> ExtractResult:
    """
    Open a PDF and return per-page text. Optionally invoke OCR for scanned pages.

    Args:
        file_path:    path to the PDF file.
        ocr_callback: if given, called once per scanned page with the
                      pymupdf Page object and the 1-indexed page number.
                      Returned text replaces the empty extracted text.

    Raises:
        RuntimeError on file/parse errors.
    """
    fitz = _get_fitz()
    try:
        doc = fitz.open(file_path)
    except Exception as e:
        raise RuntimeError(f"Failed to open PDF: {file_path}: {e}") from e

    pages: list[PageExtract] = []
    scan_pages: list[int] = []
    ocr_pages: list[int] = []

    try:
        for i, page in enumerate(doc, start=1):
            try:
                # Day 3 (2026-08-06): use layout-aware extraction so
                # 2-column PDFs (OSG9, OSG10) come out in proper reading
                # order. Falls back to plain "text" mode for genuinely
                # single-column pages.
                raw = _extract_text_layout_aware(page) or ""
            except Exception:
                # Bad page — treat as scanned.
                raw = ""

            text = _strip_text(raw)
            scanned = _looks_like_scanned(text)

            if scanned and ocr_callback is not None:
                try:
                    ocr_text = ocr_callback(page, i)
                except Exception as e:
                    # OCR failure: keep page marked scanned, no text.
                    scan_pages.append(i)
                    pages.append(PageExtract(page_num=i, text="", is_scanned=True))
                    continue

                ocr_text_clean = _strip_text(ocr_text or "")
                if ocr_text_clean:
                    text = ocr_text_clean
                    scanned = False
                    ocr_pages.append(i)
                    pages.append(
                        PageExtract(page_num=i, text=text, is_scanned=False, via_ocr=True)
                    )
                    continue

                # OCR ran but produced nothing — fall through to scanned.
                scan_pages.append(i)
                pages.append(PageExtract(page_num=i, text="", is_scanned=True))
                continue

            if scanned:
                scan_pages.append(i)

            pages.append(PageExtract(page_num=i, text=text, is_scanned=scanned))
    finally:
        doc.close()

    return ExtractResult(
        pages=pages,
        page_count=len(pages),
        scan_page_nums=sorted(set(scan_pages)),
        ocr_page_nums=sorted(set(ocr_pages)),
    )