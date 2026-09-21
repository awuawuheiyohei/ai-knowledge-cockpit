"""
image_extract.py — Extract text from a standalone image via the VL model.

Used by the image-input flow (场景 2) of IM bots (DingTalk / Feishu):
user sends a screenshot of a question, we OCR it, then run BM25 search
+ (optionally) LLM synthesis.

Reuses the same VL config and Anthropic-compatible client as
`pdf_ocr.py`. The only difference is the input: a file on disk (not
a rendered PDF page) and the user prompt is tuned to "question +
options" rather than "verbatim OCR".

Hard rules (mirror pdf_ocr.py)
------------------------------
- The VL output is used *only* as raw extracted text.
- No summarization, no paraphrasing — the prompt forbids it.
- On failure, returns "" and logs; the caller decides what to do
  (typically: fall back to "could not read image").
"""
from __future__ import annotations

import base64
import logging
import mimetypes
import re
from dataclasses import dataclass, field

import vl_config


logger = logging.getLogger("image_extract")


# Image formats MiniMax-M3 / Anthropic SDK accepts as base64.
_SUPPORTED_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


# Server-side [TRUNCATED] fallback — see _maybe_flag_truncation() below.
_TRUNCATED_RE = re.compile(r"\[truncated\]", re.IGNORECASE)
# Strong option marker: "A. text" / "A) text" — letter followed by
# period or paren + whitespace. Reliable, low false-positive.
_STRONG_OPTION_RE = re.compile(r"(?:^|\s)([A-Ea-e])[\.\)]")
# Weak option marker: catches cut-mid-option cases like "...0.002 A"
# (option A's text "0.002" and the next option letter are concatenated
# without a separator). Applied only when STRONG finds nothing —
# otherwise a sentence like "Just a fragment of text without options."
# would falsely match the "a" / "e" articles.
#
# Restricted to end-of-string so we only catch a single trailing
# A-E letter that clearly looks like a cropped option start — NOT
# every "a" / "e" article that appears mid-sentence.
_WEAK_OPTION_RE = re.compile(r"(?:^|\s)([A-Ea-e])\s*$")


@dataclass
class ExtractUsage:
    """Stats for one image_extract call."""
    bytes_in: int = 0
    chars_out: int = 0
    failed: bool = False
    error: str | None = None


class ImageExtractError(Exception):
    """Raised on a non-recoverable failure for one image."""


# ---------------------------------------------------------------------------
# VL call
# ---------------------------------------------------------------------------

def _build_client(cfg: vl_config.VlConfig):
    """Lazy-import anthropic so missing dep doesn't break non-image paths."""
    try:
        import anthropic
    except ImportError as e:
        raise ImageExtractError(
            "anthropic SDK not installed. Run: pip install anthropic"
        ) from e
    return anthropic.Anthropic(
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        timeout=cfg.timeout_s,
        max_retries=0,
    )


def _media_type_for(path) -> str:
    """Best-effort MIME type detection; default to image/jpeg."""
    mt, _ = mimetypes.guess_type(str(path))
    if mt and mt.startswith("image/"):
        return mt
    suffix = str(path).lower().rsplit(".", 1)[-1]
    return {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png",
        "gif": "image/gif",
        "webp": "image/webp",
    }.get(suffix, "image/jpeg")


def _call_vl(cfg: vl_config.VlConfig, image_bytes: bytes, media_type: str) -> str:
    """Single VL call → recognized text. Returns the raw text."""
    client = _build_client(cfg)
    img_b64 = base64.standard_b64encode(image_bytes).decode("ascii")

    response = client.messages.create(
        model=cfg.model,
        max_tokens=cfg.max_tokens,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": img_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "请只提取图片中的 CISSP/考试题目正文(题干 + 全部选项 + 必要的图表说明)。\n"
                            "**忽略**:UI 元素(按钮、菜单、导航栏、广告、登录框、设置图标、分享按钮、"
                            "用户头像、页脚版权、Next/Previous/Submit 等控件)、"
                            "页面标题/Cramming/Module 名字/时间戳/进度条。\n"
                            "**保留**:题目编号、题干、所有 A/B/C/D 选项、必要的图表说明文字。\n"
                            "规则:不要总结、不要改写、不要翻译、不要加任何评论、不要加 \"Question:\" 之类前缀。"
                            "如果图片里没有可识别的题目,只回复一个字:「空」。\n"
                            "**截断检测**:如果题目明显不完整——比如选项缺失/以 \"...\" 结尾、"
                            "或题干最后一句被切掉——在末尾单独一行写一个标记: `[TRUNCATED]`。"
                            "这样调用方会提醒用户重发完整截图。"
                        ),
                    },
                ],
            }
        ],
    )

    parts: list[str] = []
    for block in response.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "".join(parts).strip()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract_text(image_path, usage: ExtractUsage | None = None) -> str:
    """
    Extract text from a standalone image file. Returns "" on failure.

    Args:
        image_path: path to .jpg / .png / .gif / .webp file.
        usage: optional ExtractUsage to fill with stats (for status reporting).

    Returns:
        Extracted text. Empty string on failure (caller should treat as
        "could not read image" and reply accordingly).
    """
    if usage is None:
        usage = ExtractUsage()

    path = image_path
    suffix = str(path).lower().rsplit(".", 1)[-1]
    if f".{suffix}" not in _SUPPORTED_SUFFIXES:
        usage.failed = True
        usage.error = f"unsupported image format: .{suffix}"
        logger.warning("image_extract: %s", usage.error)
        return ""

    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        usage.failed = True
        usage.error = f"read failed: {e}"
        logger.error("image_extract: cannot read %s: %s", path, e)
        return ""

    usage.bytes_in = len(data)
    media_type = _media_type_for(path)

    try:
        text = _call_vl(vl_config.load_vl_config(), data, media_type)
    except Exception as e:
        usage.failed = True
        usage.error = str(e)
        logger.error("image_extract: VL call failed for %s: %s", path, e)
        return ""

    if text.lower() in ("空", "无文字", "无内容", "no text", "empty"):
        usage.failed = True
        usage.error = "VL returned empty"
        logger.info("image_extract: empty for %s", path)
        return ""

    # Server-side [TRUNCATED] fallback: VL sometimes thinks a screenshot
    # is complete when it actually ends mid-question (e.g., the options
    # stop at C but D was cropped off). Detected by option letter
    # sequence — if the highest visible letter is < D, the question is
    # almost certainly missing later options, so flag it for the
    # continuation merge logic downstream.
    text = _maybe_flag_truncation(text)

    usage.chars_out = len(text)
    logger.info("image_extract: %s → %d chars", path, len(text))
    return text


def _maybe_flag_truncation(text: str) -> str:
    """Append `[TRUNCATED]` if the extracted text looks incomplete but
    the VL model didn't flag it.

    Heuristic: extract option letters (A..E). If the highest visible
    letter is below D (i.e., the last visible option is A, B, or C),
    the question almost certainly has at least a D option that's been
    cropped off — CISSP multiple choice is overwhelmingly 4 options
    (A/B/C/D), and 3-option questions are vanishingly rare in this
    corpus. Append the marker so the continuation merge logic in
    `question_archive.append_continuation` can pick up the next
    screenshot.

    Skips when:
      - VL already added the marker (idempotent)
      - the question stem has NO option letters at all (looks like a
        different document type; don't second-guess the VL output)
    """
    if not text:
        return text
    if _TRUNCATED_RE.search(text):
        return text
    letters = _extract_option_letters_two_stage(text)
    if not letters:
        return text
    max_letter = max(l.upper() for l in letters)
    if max_letter < "D":
        logger.info(
            "image_extract: option-letter fallback → appending [TRUNCATED] "
            "(max option = %s)",
            max_letter,
        )
        return text.rstrip() + "\n[TRUNCATED]"
    return text


def _extract_option_letters_two_stage(text: str) -> list[str]:
    """Extract option letters A-E (or a-e) from text using a two-stage
    match: first try the strong marker (period or paren after the
    letter); only if that finds nothing, fall back to the weak marker
    (handles cut-mid-option cases like "...0.002 A").

    Two-stage prevents false positives on regular English text where
    a lone "a"/"e" article appears mid-sentence.
    """
    strong = _STRONG_OPTION_RE.findall(text)
    if strong:
        return strong
    return _WEAK_OPTION_RE.findall(text)
