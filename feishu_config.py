"""
feishu_config.py — Load Feishu (Lark) bot credentials from environment.

Feishu / Lark bot uses an enterprise app (自建应用) inside the
organization. The minimum we need to receive + reply to messages:

  FEISHU_APP_ID       — App ID from the developer console
                         (looks like "cli_xxxxxxxxxxxxxx")
  FEISHU_APP_SECRET   — App Secret
  FEISHU_VERIFICATION_TOKEN   — used for event subscription handshake
  FEISHU_ENCRYPT_KEY         — optional, for encrypted event payloads
  FEISHU_HOST        — bind host (default 0.0.0.0)
  FEISHU_PORT        — webhook listen port (default 9002)

For event subscription we need a public HTTPS callback URL.
For development, ngrok works:

    ngrok http 9002
    # then put the https://...ngrok-free.app URL in the
    # "Event Subscription" config in Feishu developer console.

See FEISHU_SETUP.md (TODO) for the full onboarding.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from env_loader import load_dotenv

load_dotenv()


@dataclass
class FeishuConfig:
    app_id: str
    app_secret: str
    verification_token: str
    encrypt_key: str  # may be ""
    host: str
    port: int


def is_feishu_configured() -> bool:
    """True iff the minimum credentials are set."""
    return bool(
        os.environ.get("FEISHU_APP_ID", "").strip()
        and os.environ.get("FEISHU_APP_SECRET", "").strip()
    )


def load_feishu_config() -> FeishuConfig:
    """Load Feishu config. Raises ValueError on missing required fields."""
    required = {
        "app_id": "FEISHU_APP_ID",
        "app_secret": "FEISHU_APP_SECRET",
    }
    missing = [v for k, v in required.items() if not os.environ.get(v)]
    if missing:
        raise ValueError(
            "Feishu is not configured. Missing environment variables: "
            + ", ".join(missing)
            + ". See FEISHU_SETUP.md."
        )
    return FeishuConfig(
        app_id=os.environ["FEISHU_APP_ID"].strip(),
        app_secret=os.environ["FEISHU_APP_SECRET"].strip(),
        verification_token=os.environ.get("FEISHU_VERIFICATION_TOKEN", "").strip(),
        encrypt_key=os.environ.get("FEISHU_ENCRYPT_KEY", "").strip(),
        host=os.environ.get("FEISHU_HOST", "0.0.0.0").strip(),
        port=int(os.environ.get("FEISHU_PORT", "9002")),
    )
