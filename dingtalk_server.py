"""
dingtalk_server.py — DingTalk Stream-mode chatbot.

Why Stream mode
---------------
DingTalk supports two ways to receive bot messages:

1. Webhook (custom robot)  — push only, can't receive user messages.
2. Stream mode (this)     — WebSocket long connection; you push a small
                            Python process and DingTalk dials in.

Stream mode requires:
  - A "企业内部应用" with the 机器人 capability enabled
  - Its AppKey + AppSecret (these are the Stream credentials)
  - No public callback URL needed; no nginx; no ngrok; no cert.

This module subscribes to the ChatbotMessage topic and routes incoming
text AND images into `im_router.handle_message()` / `im_router.handle_image()`.
Replies go back as Markdown.

DingTalk message_type values
----------------------------
- "text"     — plain text. Field: incoming.text.content
- "picture"  — image.       Field: incoming.image_content.download_code
- "richText" — rich text.   Field: incoming.rich_text_content (not yet handled)

Hard-won knowledge (2026-08-03 first E2E test)
-----------------------------------------------
The SDK's `get_image_download_url()` makes a `requests.post()` with NO
timeout. If DingTalk's API gateway hangs or the network is slow, the
whole bot process hangs in image processing and never replies. We
wrap it in `concurrent.futures` with an explicit timeout (15s) so
the user gets a clear error instead of an indefinite wait.

Reference: https://github.com/open-dingtalk/dingtalk-stream-sdk-python
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Optional

import requests

import paths  # noqa: F401  — ensure dirs exist on import
import storage
from im_config import load_dingtalk_config
from im_router import handle_message, handle_image


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("dingtalk_bot")
    if logger.handlers:
        return logger
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(
        logging.Formatter(
            "%(asctime)s %(name)s %(levelname)s %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    logger.addHandler(h)
    logger.setLevel(logging.INFO)
    return logger


def _send_markdown(handler_self, sdk_handler, incoming, reply: str) -> None:
    """
    Send `reply` as a Markdown message, falling back to text if the
    Markdown send raises. Logs the failure either way.
    """
    try:
        sdk_handler.reply_markdown("知识库检索", reply, incoming)
    except Exception as e:  # pragma: no cover
        handler_self._logger.warning(
            "reply_markdown failed (%s); falling back to reply_text", e,
        )
        sdk_handler.reply_text(reply, incoming)


def _extract_text(incoming) -> str:
    """
    Pull the user's text out of an incoming ChatbotMessage.

    DingTalk SDK puts text into `incoming.text.content` only for msgtype
    == 'text'. For other types we just return an empty string so the
    caller can reply with a hint.
    """
    try:
        if incoming.text and incoming.text.content:
            return incoming.text.content.strip()
    except AttributeError:
        pass
    return ""


def _detect_image_ext(data: bytes) -> str:
    """Best-effort image format from magic bytes. Returns ".jpg" / ".png" / etc."""
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


def _extract_image_download_code(incoming) -> Optional[str]:
    """
    Extract the image download_code from a DingTalk 'picture' ChatbotMessage.

    Per the SDK source, image messages have:
      - message_type == "picture"
      - incoming.image_content.download_code (snake_case)
    """
    if getattr(incoming, "message_type", None) != "picture":
        return None
    img = getattr(incoming, "image_content", None)
    if img is None:
        return None
    return getattr(img, "download_code", None)


def _download_dingtalk_image(handler_self, sdk_handler, download_code: str) -> bytes:
    """
    Two-step download, both steps explicitly time-bounded so the bot
    never hangs silently waiting for DingTalk.

    The SDK's `get_image_download_url` does `requests.post()` with no
    timeout. If DingTalk's gateway is slow, the whole process stalls.
    We wrap it in a thread + future timeout (15s) and re-raise on
    timeout so the user gets a clear error.
    """
    handler_self._logger.info(
        "DingTalk image download: requesting URL (code=%s…)",
        download_code[:16],
    )
    with ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(sdk_handler.get_image_download_url, download_code)
        try:
            download_url = future.result(timeout=15)
        except FutureTimeout:
            raise RuntimeError(
                f"DingTalk get_image_download_url timed out after 15s "
                f"(code={download_code[:16]}…)"
            )
    if not download_url:
        raise RuntimeError(
            f"DingTalk get_image_download_url returned empty "
            f"(code={download_code[:16]}…)"
        )
    handler_self._logger.info("DingTalk image download: got URL, fetching bytes…")
    resp = requests.get(download_url, timeout=30)
    resp.raise_for_status()
    handler_self._logger.info(
        "DingTalk image download: got %d bytes", len(resp.content)
    )
    return resp.content


class KBChatbotHandler:
    """
    Adapter that wraps `dingtalk_stream.ChatbotHandler` without requiring
    a top-level import (the SDK's symbol surface shifts between minor
    versions, so we resolve at runtime).
    """

    def __init__(self, logger: logging.Logger):
        self._logger = logger
        self._sdk_handler = None  # built lazily

    def build(self):
        """Construct the underlying SDK handler. Call this once at startup."""
        import dingtalk_stream  # type: ignore

        handler_self = self

        class _Handler(dingtalk_stream.ChatbotHandler):
            async def process(self, callback):  # type: ignore[override]
                try:
                    incoming = dingtalk_stream.ChatbotMessage.from_dict(callback.data)
                except Exception as e:  # pragma: no cover
                    handler_self._logger.error("could not parse incoming message: %s", e)
                    return dingtalk_stream.AckMessage.STATUS_OK, "OK"

                sender = getattr(incoming, "sender_nick", None) or getattr(
                    incoming, "sender_id", "unknown"
                )
                msgtype = getattr(incoming, "message_type", "?")

                # --- text branch (场景 1) ---
                text = _extract_text(incoming)
                if msgtype == "text" and text:
                    handler_self._logger.info(
                        "DingTalk text from %s: %r", sender, text[:120],
                    )
                    reply = handle_message("dingtalk", text)
                    _send_markdown(handler_self, self, incoming, reply)
                    return dingtalk_stream.AckMessage.STATUS_OK, "OK"

                # --- image branch (场景 2) ---
                if msgtype == "picture":
                    download_code = _extract_image_download_code(incoming)
                    handler_self._logger.info(
                        "DingTalk picture from %s (code=%s)", sender,
                        (download_code or "")[:16] + "…" if download_code else "<none>",
                    )
                    if not download_code:
                        self.reply_text(
                            "📷 收到图片但无法获取 download_code,请确认机器人"
                            "权限里有「接收图片」能力。",
                            incoming,
                        )
                        return dingtalk_stream.AckMessage.STATUS_OK, "OK"

                    # Watchdog: the whole image pipeline (download +
                    # VL OCR + LLM synth) should finish in <2 min even
                    # on a slow network. If it takes longer we abort and
                    # tell the user, so they always get SOME response
                    # within 2 min instead of an indefinite hang.
                    reply_box: list = [None]
                    err_box: list = [None]

                    def _image_pipeline():
                        tmp_path = None
                        try:
                            image_bytes = _download_dingtalk_image(
                                handler_self, self, download_code,
                            )
                            ext = _detect_image_ext(image_bytes)
                            fd, tmp_path = tempfile.mkstemp(prefix="kb_dt_", suffix=ext)
                            with os.fdopen(fd, "wb") as f:
                                f.write(image_bytes)
                            reply_box[0] = handle_image("dingtalk", tmp_path)
                        except Exception as e:
                            err_box[0] = e
                        finally:
                            if tmp_path:
                                try:
                                    os.unlink(tmp_path)
                                except OSError:
                                    pass

                    with ThreadPoolExecutor(max_workers=1) as ex:
                        future = ex.submit(_image_pipeline)
                        try:
                            future.result(timeout=120)
                        except FutureTimeout:
                            handler_self._logger.error(
                                "DingTalk image pipeline timed out after 120s"
                            )
                            self.reply_text(
                                "📷 图片处理超时(>120s)。可能原因:网络慢、"
                                "VL 凭证失效、或图片过大。请重试,或改用文字。",
                                incoming,
                            )
                            return dingtalk_stream.AckMessage.STATUS_OK, "OK"

                    if err_box[0]:
                        handler_self._logger.error(
                            "DingTalk image handling failed: %s", err_box[0],
                        )
                        self.reply_text(
                            f"📷 图片识别失败: {err_box[0]}\n请改用文字直接发送,或换张图重试。",
                            incoming,
                        )
                        return dingtalk_stream.AckMessage.STATUS_OK, "OK"

                    _send_markdown(handler_self, self, incoming, reply_box[0])
                    return dingtalk_stream.AckMessage.STATUS_OK, "OK"

                # --- unsupported ---
                handler_self._logger.info(
                    "DingTalk unsupported msgtype=%s from %s", msgtype, sender,
                )
                self.reply_text(
                    "请直接发送需要检索的关键词(纯文本)或题目截图(图片)。",
                    incoming,
                )
                return dingtalk_stream.AckMessage.STATUS_OK, "OK"

        self._sdk_handler = _Handler()
        return self._sdk_handler


def run(app_key: Optional[str] = None, app_secret: Optional[str] = None) -> None:
    """Start the Stream client. Blocks until interrupted."""
    logger = setup_logger()
    cfg = load_dingtalk_config()
    app_key = app_key or cfg.app_key
    app_secret = app_secret or cfg.app_secret

    storage.init_db()

    import dingtalk_stream  # type: ignore

    credential = dingtalk_stream.Credential(app_key, app_secret)
    client = dingtalk_stream.DingTalkStreamClient(credential)

    handler = KBChatbotHandler(logger).build()
    client.register_callback_handler(
        dingtalk_stream.ChatbotMessage.TOPIC,
        handler,
    )

    logger.info("DingTalk bot starting (Stream mode). Press Ctrl+C to stop.")
    logger.info("Make sure your DingTalk app has 机器人 enabled and "
                "messages will be delivered to this process.")

    # start_forever() blocks and handles KeyboardInterrupt internally.
    # We just wrap it so the "stopped" log line shows up cleanly on Ctrl+C.
    try:
        client.start_forever()
    finally:
        logger.info("DingTalk bot stopped.")


def main() -> None:
    parser = argparse.ArgumentParser(description="DingTalk Stream-mode KB bot")
    # Optional CLI overrides — env vars are the source of truth.
    parser.add_argument("--client-id", dest="app_key",
                        help="AppKey (overrides $DINGTALK_APP_KEY)")
    parser.add_argument("--client-secret", dest="app_secret",
                        help="AppSecret (overrides $DINGTALK_APP_SECRET)")
    args = parser.parse_args()
    run(app_key=args.app_key, app_secret=args.app_secret)


if __name__ == "__main__":
    main()