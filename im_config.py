"""
im_config.py — Load WeCom / DingTalk credentials from environment.

Reads from `os.environ`, with a fallback to `.env` in the project root.
The `.env` loader is intentionally minimal (see env_loader.py); real
shell exports always win over `.env`.

If a credential is missing for the platform you're trying to start, the
server fails fast with a clear message — better than silently auth-failing
mid-handshake.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from env_loader import load_dotenv

# Load .env once at module import. Idempotent; safe if .env is missing.
load_dotenv()


# ---------------------------------------------------------------------------
# WeCom
# ---------------------------------------------------------------------------

@dataclass
class WeComConfig:
    corp_id: str
    agent_id: str
    token: str
    encoding_aes_key: str
    host: str
    port: int


def load_wecom_config() -> WeComConfig:
    """Load WeCom config from environment. Raises ValueError on missing fields."""
    required = {
        "corp_id": "WECOM_CORP_ID",
        "agent_id": "WECOM_AGENT_ID",
        "token": "WECOM_TOKEN",
        "encoding_aes_key": "WECOM_ENCODING_AES_KEY",
    }
    missing = [v for k, v in required.items() if not os.environ.get(v)]
    if missing:
        raise ValueError(
            "WeCom is not configured. Missing environment variables: "
            + ", ".join(missing)
            + ". See IM_SETUP.md."
        )

    encoding_aes_key = os.environ["WECOM_ENCODING_AES_KEY"].strip()
    if len(encoding_aes_key) != 43:
        raise ValueError(
            f"WECOM_ENCODING_AES_KEY must be 43 characters (got {len(encoding_aes_key)})."
        )

    return WeComConfig(
        corp_id=os.environ["WECOM_CORP_ID"].strip(),
        agent_id=os.environ["WECOM_AGENT_ID"].strip(),
        token=os.environ["WECOM_TOKEN"].strip(),
        encoding_aes_key=encoding_aes_key,
        host=os.environ.get("WECOM_HOST", "0.0.0.0").strip(),
        port=int(os.environ.get("WECOM_PORT", "9001")),
    )


# ---------------------------------------------------------------------------
# DingTalk
# ---------------------------------------------------------------------------

@dataclass
class DingTalkConfig:
    app_key: str
    app_secret: str


def load_dingtalk_config() -> DingTalkConfig:
    """Load DingTalk Stream-mode config from environment."""
    required = {
        "app_key": "DINGTALK_APP_KEY",
        "app_secret": "DINGTALK_APP_SECRET",
    }
    missing = [v for k, v in required.items() if not os.environ.get(v)]
    if missing:
        raise ValueError(
            "DingTalk is not configured. Missing environment variables: "
            + ", ".join(missing)
            + ". See IM_SETUP.md."
        )
    return DingTalkConfig(
        app_key=os.environ["DINGTALK_APP_KEY"].strip(),
        app_secret=os.environ["DINGTALK_APP_SECRET"].strip(),
    )