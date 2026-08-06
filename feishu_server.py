"""
feishu_server.py — Feishu (Lark) bot webhook server.

Receives events from Feishu, routes through im_router, replies via the
Feishu IM API. Both text and image inputs are supported.

Event types handled
-------------------
- im.message.receive_v1 (text)   → im_router.handle_message(platform='feishu', text)
- im.message.receive_v1 (image)  → download → im_router.handle_image(platform='feishu', path)

Setup
-----
See FEISHU_SETUP.md for the developer-console walkthrough.
For local development, expose this server with ngrok:
    ngrok http 9002
and paste the https URL into the "Event Subscription" config.

Hard rules (mirrored from CLAUDE.md)
------------------------------------
- The bot is a thin pass-through to im_router. All safety / Hard Rules
  live in im_router + answer_synth, NOT here.
- No LLM call from this file directly.
- The webhook returns 200 immediately and processes events in a background
  thread, so Feishu's 3-second webhook timeout doesn't kill slow LLM calls.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import urllib.request
from typing import Any

import paths  # noqa: F401  — ensure dirs exist on import

import storage
import im_router
import feishu_config


logger = logging.getLogger("feishu_bot")


# ---------------------------------------------------------------------------
# Tenant access token cache
# ---------------------------------------------------------------------------
_TOKEN_CACHE: dict[str, Any] = {"token": None, "expires_at": 0.0}


def get_tenant_access_token(cfg: feishu_config.FeishuConfig) -> str:
    """
    Fetch a tenant_access_token from Feishu, with in-process caching.
    Tokens last 2h; we refresh 60s early.
    """
    now = time.time()
    if _TOKEN_CACHE["token"] and now < _TOKEN_CACHE["expires_at"] - 60:
        return _TOKEN_CACHE["token"]

    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    body = json.dumps({"app_id": cfg.app_id, "app_secret": cfg.app_secret}).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode())
    if data.get("code") != 0:
        raise RuntimeError(f"feishu tenant_access_token failed: {data}")

    _TOKEN_CACHE["token"] = data["tenant_access_token"]
    _TOKEN_CACHE["expires_at"] = now + data.get("expire", 7200)
    logger.info("feishu tenant_access_token refreshed, expires_at=%d", int(_TOKEN_CACHE["expires_at"]))
    return _TOKEN_CACHE["token"]


# ---------------------------------------------------------------------------
# Feishu API helpers
# ---------------------------------------------------------------------------
def _api_post_json(cfg: feishu_config.FeishuConfig, url: str, body: dict) -> dict:
    """POST a JSON body to a Feishu API with auth. Returns parsed JSON."""
    token = get_tenant_access_token(cfg)
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _api_get_bytes(cfg: feishu_config.FeishuConfig, url: str) -> bytes:
    """GET a Feishu API with auth. Returns raw bytes (for image download)."""
    token = get_tenant_access_token(cfg)
    req = urllib.request.Request(
        url, method="GET",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


# ---------------------------------------------------------------------------
# Image download
# ---------------------------------------------------------------------------
def _detect_image_ext(data: bytes) -> str:
    """
    Best-effort image format detection from magic bytes.
    Returns the suffix including the dot (e.g. ".jpg").
    """
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"  # safe default for chat images


def _download_image(cfg: feishu_config.FeishuConfig, message_id: str, image_key: str) -> bytes:
    """Download image bytes from Feishu by message_id + image_key."""
    url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{image_key}"
    return _api_get_bytes(cfg, url)


# ---------------------------------------------------------------------------
# Reply
# ---------------------------------------------------------------------------
def _reply_text(
    cfg: feishu_config.FeishuConfig,
    receive_id: str,
    receive_id_type: str,
    text: str,
) -> dict:
    """Send a text reply to a Feishu user (open_id) or chat (chat_id)."""
    url = f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_id_type}"
    return _api_post_json(cfg, url, {
        "receive_id": receive_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    })


# ---------------------------------------------------------------------------
# Event payload parsing
# ---------------------------------------------------------------------------
def _extract_text(event: dict) -> str:
    """Extract the user text from a Feishu text message event. '' if not text."""
    msg = event.get("message", {})
    if msg.get("message_type") != "text":
        return ""
    try:
        content = json.loads(msg.get("content") or "{}")
    except json.JSONDecodeError:
        return ""
    return (content.get("text") or "").strip()


def _extract_image_key(event: dict) -> str | None:
    """Extract image_key from a Feishu image message event. None if not image."""
    msg = event.get("message", {})
    if msg.get("message_type") != "image":
        return None
    try:
        content = json.loads(msg.get("content") or "{}")
    except json.JSONDecodeError:
        return None
    key = content.get("image_key")
    return key if key else None


def _resolve_reply_target(event: dict) -> tuple[str, str]:
    """
    Return (receive_id, receive_id_type) for replying to the message.

    p2p  → reply to the sender's open_id (DM back to them)
    group → reply to the chat_id
    """
    msg = event.get("message", {})
    chat_type = msg.get("chat_type", "p2p")
    sender = event.get("sender", {}).get("sender_id", {})
    if chat_type == "p2p":
        return sender.get("open_id", ""), "open_id"
    return msg.get("chat_id", ""), "chat_id"


# ---------------------------------------------------------------------------
# Event handler (runs in a background thread to avoid webhook timeout)
# ---------------------------------------------------------------------------
def _handle_event(event: dict) -> None:
    """Process one message event: route by message_type, reply."""
    cfg = feishu_config.load_feishu_config()
    storage.init_db()  # idempotent

    msg = event.get("message", {})
    msg_id = msg.get("message_id", "?")
    chat_type = msg.get("chat_type", "?")
    sender_open = event.get("sender", {}).get("sender_id", {}).get("open_id", "?")

    # --- text ---
    text = _extract_text(event)
    if text:
        logger.info("feishu text chat=%s from=%s msg=%s: %r",
                    chat_type, sender_open, msg_id, text[:120])
        reply = im_router.handle_message("feishu", text, user_id=sender_open)
        receive_id, receive_id_type = _resolve_reply_target(event)
        try:
            _reply_text(cfg, receive_id, receive_id_type, reply)
        except Exception as e:
            logger.error("feishu reply failed for msg=%s: %s", msg_id, e)
        return

    # --- image ---
    image_key = _extract_image_key(event)
    if image_key:
        logger.info("feishu image chat=%s from=%s msg=%s key=%s",
                    chat_type, sender_open, msg_id, image_key)
        tmp_path = None
        try:
            image_bytes = _download_image(cfg, msg_id, image_key)
            ext = _detect_image_ext(image_bytes)
            fd, tmp_path = tempfile.mkstemp(prefix="kb_feishu_", suffix=ext)
            with os.fdopen(fd, "wb") as f:
                f.write(image_bytes)
            reply = im_router.handle_image("feishu", tmp_path, user_id=sender_open)
        except Exception as e:
            logger.error("feishu image handling failed for msg=%s: %s", msg_id, e)
            reply = (
                "📷 图片识别失败,请改用文字直接发送,或换张图重试。\n"
                f"（错误：{e}）"
            )
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        receive_id, receive_id_type = _resolve_reply_target(event)
        try:
            _reply_text(cfg, receive_id, receive_id_type, reply)
        except Exception as e:
            logger.error("feishu reply failed for msg=%s: %s", msg_id, e)
        return

    # --- unknown / unsupported ---
    logger.info("feishu unsupported msg_type=%s chat=%s from=%s msg=%s",
                msg.get("message_type"), chat_type, sender_open, msg_id)
    receive_id, receive_id_type = _resolve_reply_target(event)
    try:
        _reply_text(
            cfg, receive_id, receive_id_type,
            "请直接发送需要检索的关键词(纯文本)或题目截图(图片)。"
        )
    except Exception as e:
        logger.error("feishu reply failed for msg=%s: %s", msg_id, e)


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
def create_app():
    from flask import Flask, request, jsonify
    app = Flask(__name__)

    @app.route("/feishu/event", methods=["POST"])
    def on_event():
        payload = request.get_json(silent=True) or {}

        # 1) URL verification handshake (sent once when setting up Event Subscription)
        if payload.get("type") == "url_verification":
            return jsonify({"challenge": payload.get("challenge", "")})

        # 2) Real events — process in a background thread to avoid hitting
        #    Feishu's 3-second webhook timeout during LLM/VL calls.
        header = payload.get("header", {})
        event_type = header.get("event_type", "")
        if event_type == "im.message.receive_v1":
            event = payload.get("event", {})

            def _runner(ev=event):
                try:
                    _handle_event(ev)
                except Exception as e:
                    logger.exception("feishu background handler crashed: %s", e)

            t = threading.Thread(target=_runner, name=f"feishu-evt-{event.get('message', {}).get('message_id', '?')[:8]}", daemon=True)
            t.start()
        else:
            logger.info("feishu ignoring event_type=%s", event_type)

        return jsonify({"code": 0, "msg": "ok"})

    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def serve() -> None:
    """Start the Feishu webhook server (Flask dev server)."""
    cfg = feishu_config.load_feishu_config()
    app = create_app()
    logger.info("feishu webhook listening on %s:%d", cfg.host, cfg.port)
    logger.info("Configure this URL in Feishu developer console → Event Subscription.")
    logger.info("For local dev: ngrok http %d", cfg.port)
    app.run(host=cfg.host, port=cfg.port, debug=False, use_reloader=False)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )
    serve()


if __name__ == "__main__":
    main()
