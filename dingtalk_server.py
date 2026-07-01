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
text into `im_router.handle_message()`. Replies go back as Markdown.

Reference: https://github.com/open-dingtalk/dingtalk-stream-sdk-python
"""
from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

import paths  # noqa: F401  — ensure dirs exist on import
import storage
from im_config import load_dingtalk_config
from im_router import handle_message


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

                text = _extract_text(incoming)
                sender = getattr(incoming, "sender_nick", None) or getattr(
                    incoming, "sender_id", "unknown"
                )
                handler_self._logger.info(
                    "DingTalk msg from %s (msgtype=%s): %r",
                    sender,
                    getattr(incoming, "message_type", "?"),
                    text[:120],
                )

                if getattr(incoming, "message_type", None) != "text" or not text:
                    self.reply_text(
                        "请直接发送需要检索的关键词（纯文本）。",
                        incoming,
                    )
                    return dingtalk_stream.AckMessage.STATUS_OK, "OK"

                reply = handle_message("dingtalk", text)
                # Markdown renders nicer in DingTalk than plain text,
                # especially with code blocks for source filenames.
                title = "知识库检索"
                try:
                    self.reply_markdown(title, reply, incoming)
                except Exception as e:  # pragma: no cover
                    handler_self._logger.warning(
                        "reply_markdown failed (%s); falling back to reply_text",
                        e,
                    )
                    self.reply_text(reply, incoming)

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