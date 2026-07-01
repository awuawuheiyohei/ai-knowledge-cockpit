"""
pdf_ocr.py — OCR fallback for scanned PDF pages.

Pipeline
--------
1. pymupdf renders the scanned page to a PNG (DPI ~200, configurable).
2. The PNG is base64-encoded and sent to a vision-language model
   (default: MiniMax-M3 via the Anthropic-compatible endpoint).
3. The model's text output replaces the (empty) extracted text.

Hard rules
----------
- LLM output is used *only* as raw text replacement for the page.
  No summarization, no paraphrasing — the OCR prompt explicitly forbids it.
- If the VL call fails or returns nothing useful, the page stays as a
  scan warning; we don't retry blindly.
- Each successful OCR call increments a usage counter so `status`
  can show how many pages were OCR'd and how many token-equivalents.
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field

import fitz  # pymupdf

import vl_config


logger = logging.getLogger("pdf_ocr")


@dataclass
class OcrUsage:
    """Aggregate stats for a single OCR run."""
    pages_ocrd: int = 0
    pages_failed: int = 0
    input_chars: int = 0      # rough proxy for image bytes
    output_chars: int = 0
    failed_page_nums: list[int] = field(default_factory=list)


class OcrError(Exception):
    """Raised on a non-recoverable OCR failure for one page."""


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_page_to_png(page: "fitz.Page", dpi: int = 200) -> bytes:
    """
    Render a pymupdf page to PNG bytes.

    DPI 200 is the sweet spot for printed text — higher hurts token cost
    without meaningful OCR quality gains.
    """
    # pymupdf wants a Matrix(scale, scale). scale = dpi / 72.
    scale = dpi / 72.0
    matrix = fitz.Matrix(scale, scale)
    pix = page.get_pixmap(matrix=matrix, alpha=False)
    return pix.tobytes("png")


# ---------------------------------------------------------------------------
# VL call
# ---------------------------------------------------------------------------

def _build_client(cfg: vl_config.VlConfig):
    """Lazy-import anthropic so missing dep doesn't break non-OCR paths."""
    try:
        import anthropic
    except ImportError as e:
        raise OcrError(
            "anthropic SDK not installed. Run: pip install anthropic"
        ) from e

    return anthropic.Anthropic(
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        timeout=cfg.timeout_s,
        max_retries=0,  # we handle retries manually
    )


def _call_vl(
    cfg: vl_config.VlConfig,
    png_bytes: bytes,
) -> str:
    """Make a single VL OCR call. Returns recognized text."""
    client = _build_client(cfg)
    img_b64 = base64.standard_b64encode(png_bytes).decode("ascii")

    response = client.messages.create(
        model=cfg.model,
        max_tokens=cfg.max_tokens,
        system=cfg.prompt,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": img_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": "请输出这张图片中的原文。",
                    },
                ],
            }
        ],
    )

    parts: list[str] = []
    for block in response.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)

    text = "".join(parts).strip()

    # If the model explicitly says the image is empty, return empty.
    # We use a tiny stop-set instead of exact match — VL models phrase
    # this in many ways ("空", "无文字", "No text", etc.).
    empty_markers = ("空", "无文字", "无内容", "no text", "empty")
    if text.lower() in empty_markers:
        return ""

    return text


# ---------------------------------------------------------------------------
# High-level: OCR one page with usage tracking
# ---------------------------------------------------------------------------

def ocr_page(
    page: "fitz.Page",
    page_num: int,
    cfg: vl_config.VlConfig,
    usage: OcrUsage,
) -> str:
    """
    OCR a single scanned page. Updates `usage` with stats.

    Returns recognized text. Raises OcrError on hard failure.
    """
    try:
        png = render_page_to_png(page, dpi=cfg.dpi)
    except Exception as e:
        usage.pages_failed += 1
        usage.failed_page_nums.append(page_num)
        raise OcrError(f"page {page_num}: render failed: {e}") from e

    usage.input_chars += len(png)

    try:
        text = _call_vl(cfg, png)
    except Exception as e:
        usage.pages_failed += 1
        usage.failed_page_nums.append(page_num)
        logger.warning("OCR failed on page %d: %s", page_num, e)
        raise OcrError(f"page {page_num}: VL call failed: {e}") from e

    usage.output_chars += len(text)
    if text:
        usage.pages_ocrd += 1
    else:
        # Empty after OCR — treat as failed for warnings, but don't raise.
        usage.pages_failed += 1
        usage.failed_page_nums.append(page_num)
        logger.info("OCR returned empty for page %d", page_num)

    return text