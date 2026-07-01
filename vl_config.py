"""
vl_config.py — Load vision-language (VL) credentials for OCR fallback.

We use the Anthropic SDK pointed at MiniMax's Anthropic-compatible
endpoint, with the M3 model (which is natively multimodal).

Environment variables (defaults shown):

  VL_API_KEY       — required, your MiniMax API key
  VL_BASE_URL      — https://api.minimaxi.com/anthropic
  VL_MODEL         — MiniMax-M3 (M2.x does NOT support images)
  VL_OCR_PROMPT    — instruction sent with each scanned page
  VL_TIMEOUT_S     — per-call timeout in seconds (default 60)
  VL_MAX_TOKENS    — output cap for one OCR call (default 2000)
  VL_DPI           — render DPI for scanned pages (default 200)

If `VL_API_KEY` is not set, `is_vl_configured()` returns False and the
ingest pipeline simply skips OCR (scanned pages stay as scan warnings).

The `.env` file in the project root is loaded automatically as a fallback;
real shell exports take priority over `.env`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from env_loader import load_dotenv

# Load .env once at module import. Idempotent; safe if .env is missing.
load_dotenv()


DEFAULT_BASE_URL = "https://api.minimaxi.com/anthropic"
DEFAULT_MODEL = "MiniMax-M3"
DEFAULT_PROMPT = (
    "请识别这张图片中的所有文字内容，按原文段落顺序输出。"
    "不要总结、不要改写、不要翻译、不要加任何评论。"
    "如果图片中没有可识别的文字，只回复一个字：「空」。"
)


@dataclass
class VlConfig:
    api_key: str
    base_url: str
    model: str
    prompt: str
    timeout_s: int
    max_tokens: int
    dpi: int


def is_vl_configured() -> bool:
    """True iff the user has set a VL API key."""
    return bool(os.environ.get("VL_API_KEY", "").strip())


def load_vl_config() -> VlConfig:
    """Load VL config; raises ValueError on missing API key."""
    api_key = os.environ.get("VL_API_KEY", "").strip()
    if not api_key:
        raise ValueError(
            "VL_API_KEY is not set. Run `python app.py ingest --ocr` will "
            "auto-detect and tell you how to set it. See OCR_SETUP.md."
        )
    return VlConfig(
        api_key=api_key,
        base_url=os.environ.get("VL_BASE_URL", DEFAULT_BASE_URL).strip(),
        model=os.environ.get("VL_MODEL", DEFAULT_MODEL).strip(),
        prompt=os.environ.get("VL_OCR_PROMPT", DEFAULT_PROMPT).strip(),
        timeout_s=int(os.environ.get("VL_TIMEOUT_S", "60")),
        max_tokens=int(os.environ.get("VL_MAX_TOKENS", "2000")),
        dpi=int(os.environ.get("VL_DPI", "200")),
    )