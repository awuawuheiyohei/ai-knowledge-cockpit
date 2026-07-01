"""
llm_config.py — Load LLM credentials for query rewriting.

We piggy-back on the same MiniMax API key as VL (same MiniMax-M3 model
serves both text-only chat for query rewriting and multimodal chat for
OCR). To keep things explicit, the LLM_* env vars win; if absent, we
fall back to VL_*.

Env vars (in priority order):

  LLM_API_KEY     →  VL_API_KEY      (required, MiniMax API key)
  LLM_BASE_URL    →  VL_BASE_URL     (default https://api.minimaxi.com/anthropic)
  LLM_MODEL       →  VL_MODEL        (default MiniMax-M3)
  LLM_TIMEOUT_S   →  no fallback     (default 30; rewrite calls are small)
  LLM_MAX_TOKENS  →  no fallback     (default 200; rewrite outputs few tokens)

`.env` is loaded automatically at import time (see env_loader).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from env_loader import load_dotenv

load_dotenv()


DEFAULT_BASE_URL = "https://api.minimaxi.com/anthropic"
DEFAULT_MODEL = "MiniMax-M3"


@dataclass
class LlmConfig:
    api_key: str
    base_url: str
    model: str
    timeout_s: int
    max_tokens: int


def is_llm_configured() -> bool:
    """True iff a usable LLM API key is available (LLM_ or VL_)."""
    return bool(
        os.environ.get("LLM_API_KEY", "").strip()
        or os.environ.get("VL_API_KEY", "").strip()
    )


def load_llm_config() -> LlmConfig:
    """Load LLM config; raises ValueError on missing API key."""
    api_key = (
        os.environ.get("LLM_API_KEY", "").strip()
        or os.environ.get("VL_API_KEY", "").strip()
    )
    if not api_key:
        raise ValueError(
            "LLM_API_KEY (or VL_API_KEY) is not set. Query rewriting "
            "needs an LLM key. See OCR_SETUP.md for setup."
        )
    base_url = (
        os.environ.get("LLM_BASE_URL", "").strip()
        or os.environ.get("VL_BASE_URL", "").strip()
        or DEFAULT_BASE_URL
    )
    model = (
        os.environ.get("LLM_MODEL", "").strip()
        or os.environ.get("VL_MODEL", "").strip()
        or DEFAULT_MODEL
    )
    return LlmConfig(
        api_key=api_key,
        base_url=base_url,
        model=model,
        timeout_s=int(os.environ.get("LLM_TIMEOUT_S", "30")),
        max_tokens=int(os.environ.get("LLM_MAX_TOKENS", "200")),
    )