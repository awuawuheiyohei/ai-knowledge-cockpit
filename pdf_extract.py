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
                raw = page.get_text("text") or ""
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