"""
feishu_server.py — Feishu (Lark) bot Webhook server (Phase C skeleton).

Status: SCAFFOLD ONLY. Phase C in the original 3-phase plan is to
wire image + text handling from Feishu events into im_router.

What this file has
------------------
- Config loading (via feishu_config)
- A Flask app exposing /feishu/event for event subscription
- URL verification handshake (Feishu sends a "challenge" on setup)
- Helpers to fetch a tenant_access_token (needed to reply to messages)

What is TODO
------------
- text message handler → im_router.handle_message(platform='feishu', raw_text=...)
- image message handler → download image → im_router.handle_image(...)
- message reply POST (use the message API with tenant_access_token)
- encrypted-payload support (if FEISHU_ENCRYPT_KEY is set)
- FEISHU_SETUP.md guide

Hard rules (mirrored from CLAUDE.md)
------------------------------------
- The bot is a thin pass-through to `im_router`. All safety / Hard Rules
  live in `im_router` + `answer_synth`, NOT here.
- No LLM call from this file directly.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import feishu_config

logger = logging.getLogger("feishu_server")


# ---------------------------------------------------------------------------
# Lazy imports
# ---------------------------------------------------------------------------
def _flask():
    try:
        from flask import Flask, request, jsonify
    except ImportError as e:
        raise RuntimeError(
            "Flask not installed. Run: pip install flask"
        ) from e
    return Flask, request, jsonify


# ---------------------------------------------------------------------------
# tenant_access_token cache
# ---------------------------------------------------------------------------
_TOKEN_CACHE: dict[str, Any] = {"token": None, "expires_at": 0.0}


def get_tenant_access_token(cfg: feishu_config.FeishuConfig) -> str:
    """
    Fetch a tenant_access_token from Feishu's open API, with simple
    in-process caching. Tokens last 2h; we refresh 60s early.
    """
    import urllib.request

    now = time.time()
    if _TOKEN_CACHE["token"] and now < _TOKEN_CACHE["expires_at"] - 60:
        return _TOKEN_CACHE["token"]

    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    body = json.dumps({"app_id": cfg.app_id, "app_secret": cfg.app_secret}).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode())
    if data.get("code") != 0:
        raise RuntimeError(f"feishu tenant_access_token failed: {data}")

    _TOKEN_CACHE["token"] = data["tenant_access_token"]
    _TOKEN_CACHE["expires_at"] = now + data.get("expire", 7200)
    return _TOKEN_CACHE["token"]


# ---------------------------------------------------------------------------
# Webhook server
# ---------------------------------------------------------------------------
def create_app():
    """
    Build the Flask app. Wire to a real WSGI server in `serve()`.
    """
    Flask, _, jsonify = _flask()
    app = Flask(__name__)

    @app.route("/feishu/event", methods=["POST"])
    def on_event():
        payload = _.get_json(silent=True) or {}

        # 1) URL verification handshake — Feishu sends a "challenge"
        #    when you first set up the event subscription URL.
        if payload.get("type") == "url_verification":
            return jsonify({"challenge": payload.get("challenge", "")})

        # 2) Real events. Header carries encrypt / verification token.
        #    For now we only handle the unencrypted case.
        header = payload.get("header", {})
        event_type = header.get("event_type", "")

        # TODO Phase C: dispatch on event_type
        #   - im.message.receive_v1.text → handle text
        #   - im.message.receive_v1.image → download + handle_image
        # For now we just log and 200 OK.
        logger.info("feishu event received: %s", event_type)
        return jsonify({"code": 0, "msg": "ok"})

    return app


def serve():
    """Start the Feishu webhook server (Flask dev server)."""
    cfg = feishu_config.load_feishu_config()
    app = create_app()
    logger.info("feishu webhook listening on %s:%d", cfg.host, cfg.port)
    app.run(host=cfg.host, port=cfg.port, debug=False, use_reloader=False)


# ---------------------------------------------------------------------------
# Entry point used by `app.py serve feishu` (TODO: wire into app.py)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    serve()
