"""
wecom_server.py — Enterprise WeChat "Smart Bot" callback receiver.

We deliberately do NOT depend on the `wechatpy` ecosystem. Its crypto
helpers are buried inside a tree of model classes and old werkzeug
quirks — historically a source of breakage on new werkzeug releases.
The crypto we need is small enough to own here.

What this implements
--------------------
1. URL verification (GET) — WeCom hits our URL with `echostr`; we verify
   the signature, decrypt echostr, and return the plaintext.
2. Message receipt (POST) — WeCom POSTs an encrypted XML payload; we
   verify, decrypt, parse, run `im_router.handle_message()`, encrypt the
   reply, and return the encrypted XML.
3. AES-256-CBC with PKCS#7 padding, IV = first 16 bytes of AESKey.
   The message layout is the standard WeCom one:
        random(16) || msg_len(4, network-order) || msg || receiveid

Reference (canonical, verified): https://developer.work.weixin.qq.com/document/path/90968
"""
from __future__ import annotations

import base64
import hashlib
import os
import random
import socket
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional

from Crypto.Cipher import AES
from flask import Flask, request

import paths  # noqa: F401  — ensure dirs exist on import
import storage
from im_config import load_wecom_config
from im_router import handle_message


# ---------------------------------------------------------------------------
# Crypto primitives — minimal subset of WeCom's WXBizMsgCrypt
# ---------------------------------------------------------------------------

class WeComCryptoError(Exception):
    """Raised on signature/decrypt failures."""


class WeComCrypto:
    """AES-256-CBC + SHA1 sig, per the WeCom open-doc spec."""

    BLOCK_SIZE = 32  # PKCS#7 block size (32 bytes, not 16 — this is WeCom-specific)

    def __init__(self, token: str, encoding_aes_key: str, receive_id: str):
        if len(encoding_aes_key) != 43:
            raise WeComCryptoError(
                f"encoding_aes_key must be 43 chars (got {len(encoding_aes_key)})"
            )
        self.token = token
        self.receive_id = receive_id
        # AESKey = Base64Decode(encodingAesKey + "=")
        self.aes_key = base64.b64decode(encoding_aes_key + "=")

    # -- signature ----------------------------------------------------------

    @staticmethod
    def _sha1_sign(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
        """SHA1(sort([token, timestamp, nonce, encrypt])) → lowercase hex."""
        parts = sorted([token, timestamp, nonce, encrypt])
        s = "".join(parts).encode("utf-8")
        return hashlib.sha1(s).hexdigest()

    def _verify_signature(
        self, msg_signature: str, timestamp: str, nonce: str, encrypt: str
    ) -> None:
        expected = self._sha1_sign(self.token, timestamp, nonce, encrypt)
        if expected != msg_signature:
            raise WeComCryptoError(
                f"signature mismatch: got {msg_signature}, expected {expected}"
            )

    # -- PKCS#7 (WeCom variant: 32-byte blocks) ----------------------------

    def _pkcs7_encode(self, data: bytes) -> bytes:
        pad = self.BLOCK_SIZE - (len(data) % self.BLOCK_SIZE)
        if pad == 0:
            pad = self.BLOCK_SIZE
        return data + bytes([pad]) * pad

    def _pkcs7_decode(self, data: bytes) -> bytes:
        if not data:
            return data
        pad = data[-1]
        if pad < 1 or pad > self.BLOCK_SIZE:
            return data  # tolerate zero-padding fallback
        return data[:-pad]

    # -- AES ---------------------------------------------------------------

    def _aes_encrypt(self, plaintext: bytes) -> bytes:
        cipher = AES.new(
            self.aes_key,
            AES.MODE_CBC,
            iv=self.aes_key[:16],
        )
        return cipher.encrypt(self._pkcs7_encode(plaintext))

    def _aes_decrypt(self, ciphertext: bytes) -> bytes:
        cipher = AES.new(
            self.aes_key,
            AES.MODE_CBC,
            iv=self.aes_key[:16],
        )
        return self._pkcs7_decode(cipher.decrypt(ciphertext))

    # -- high-level: encrypt/decrypt payloads ------------------------------

    def _encrypt(self, plaintext: bytes) -> str:
        """Build the byte layout and return base64(ciphertext)."""
        rand_bytes = bytes(random.randint(0, 255) for _ in range(16))
        msg_len = struct.pack(">I", len(plaintext))
        receive_id_bytes = self.receive_id.encode("utf-8")
        buf = rand_bytes + msg_len + plaintext + receive_id_bytes
        return base64.b64encode(self._aes_encrypt(buf)).decode("ascii")

    def _decrypt(self, b64_ciphertext: str) -> bytes:
        ct = base64.b64decode(b64_ciphertext)
        plain = self._aes_decrypt(ct)
        # Skip 16 random bytes + 4 msg_len bytes
        msg_len = struct.unpack(">I", plain[16:20])[0]
        msg = plain[20 : 20 + msg_len]
        receive_id = plain[20 + msg_len :]
        if receive_id.decode("utf-8", errors="replace") != self.receive_id:
            # Different receive_id is a hard fail per spec.
            raise WeComCryptoError(
                f"receive_id mismatch: expected {self.receive_id!r}, "
                f"got {receive_id.decode('utf-8', errors='replace')!r}"
            )
        return msg

    # -- URL verification (GET) -------------------------------------------

    def verify_url(
        self,
        msg_signature: str,
        timestamp: str,
        nonce: str,
        echo_str: str,
    ) -> str:
        self._verify_signature(msg_signature, timestamp, nonce, echo_str)
        return self._decrypt(echo_str).decode("utf-8")

    # -- Message decrypt (POST) -------------------------------------------

    def decrypt_message(
        self,
        msg_signature: str,
        timestamp: str,
        nonce: str,
        post_xml: str,
    ) -> str:
        # Extract <Encrypt>...</Encrypt>
        try:
            root = ET.fromstring(post_xml)
            encrypt_el = root.find("Encrypt")
            if encrypt_el is None or not encrypt_el.text:
                raise WeComCryptoError("no <Encrypt> element in POST body")
            encrypt = encrypt_el.text.strip()
        except ET.ParseError as e:
            raise WeComCryptoError(f"could not parse POST XML: {e}") from e

        self._verify_signature(msg_signature, timestamp, nonce, encrypt)
        return self._decrypt(encrypt).decode("utf-8")

    # -- Message encrypt (reply) ------------------------------------------

    def encrypt_message(self, reply_xml: str, timestamp: str, nonce: str) -> str:
        encrypt = self._encrypt(reply_xml.encode("utf-8"))
        signature = self._sha1_sign(self.token, timestamp, nonce, encrypt)
        return (
            "<xml>"
            f"<Encrypt><![CDATA[{encrypt}]]></Encrypt>"
            f"<MsgSignature><![CDATA[{signature}]]></MsgSignature>"
            f"<TimeStamp>{timestamp}</TimeStamp>"
            f"<Nonce><![CDATA[{nonce}]]></Nonce>"
            "</xml>"
        )


# ---------------------------------------------------------------------------
# XML parsing of decrypted WeCom messages
# ---------------------------------------------------------------------------

@dataclass
class WeComIncomingMessage:
    to_user_name: str
    from_user_name: str
    create_time: str
    msg_type: str       # 'text' for now
    content: str
    msg_id: str
    agent_id: str

    @classmethod
    def from_xml(cls, xml_text: str) -> "WeComIncomingMessage":
        root = ET.fromstring(xml_text)

        def get(tag: str) -> str:
            el = root.find(tag)
            return (el.text or "").strip() if el is not None else ""

        return cls(
            to_user_name=get("ToUserName"),
            from_user_name=get("FromUserName"),
            create_time=get("CreateTime"),
            msg_type=get("MsgType"),
            content=get("Content"),
            msg_id=get("MsgId"),
            agent_id=get("AgentID"),
        )


def _build_text_reply_xml(
    from_user: str, to_user: str, content: str, create_time: str
) -> str:
    """Build the XML body WeCom expects for a text reply."""
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{from_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{to_user}]]></FromUserName>"
        f"<CreateTime>{create_time}</CreateTime>"
        f"<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{content}]]></Content>"
        "</xml>"
    )


# ---------------------------------------------------------------------------
# Flask app factory
# ---------------------------------------------------------------------------

@dataclass
class _ServerContext:
    crypto: WeComCrypto
    corp_id: str


def make_app() -> Flask:
    cfg = load_wecom_config()
    storage.init_db()
    crypto = WeComCrypto(
        token=cfg.token,
        encoding_aes_key=cfg.encoding_aes_key,
        receive_id=cfg.corp_id,  # for plain 企业应用, receive_id == corp_id
    )
    ctx = _ServerContext(crypto=crypto, corp_id=cfg.corp_id)

    app = Flask("wecom_bot")
    app.config["JSON_AS_ASCII"] = False
    app.config["ctx"] = ctx

    # Default route — WeCom will hit this when you save the config.
    @app.route("/", methods=["GET", "POST"])
    @app.route("/wecom/callback", methods=["GET", "POST"])
    def callback():
        msg_signature = request.args.get("msg_signature", "")
        timestamp = request.args.get("timestamp", "")
        nonce = request.args.get("nonce", "")

        if request.method == "GET":
            # URL verification step
            echo_str = request.args.get("echostr", "")
            try:
                plaintext = ctx.crypto.verify_url(
                    msg_signature, timestamp, nonce, echo_str
                )
                return plaintext  # must be raw plaintext, no quotes/whitespace
            except WeComCryptoError as e:
                app.logger.error("WeCom URL verify failed: %s", e)
                return ("signature error", 403)

        # POST: incoming encrypted message
        post_xml = request.get_data(as_text=True)
        try:
            plain_xml = ctx.crypto.decrypt_message(
                msg_signature, timestamp, nonce, post_xml
            )
            incoming = WeComIncomingMessage.from_xml(plain_xml)
        except (WeComCryptoError, ET.ParseError) as e:
            app.logger.error("WeCom decrypt/parse failed: %s", e)
            return ("bad message", 400)

        app.logger.info(
            "WeCom msg from %s type=%s content=%r",
            incoming.from_user_name,
            incoming.msg_type,
            incoming.content[:120],
        )

        if incoming.msg_type != "text":
            reply_text = "暂时只支持文字消息，请直接发送关键词。"
        else:
            reply_text = handle_message("wecom", incoming.content)

        reply_xml = _build_text_reply_xml(
            from_user=incoming.to_user_name,
            to_user=incoming.from_user_name,
            content=reply_text,
            create_time=incoming.create_time or str(_now()),
        )
        encrypted = ctx.crypto.encrypt_message(reply_xml, timestamp, nonce)
        return encrypted, 200, {"Content-Type": "application/xml"}

    @app.route("/healthz", methods=["GET"])
    def healthz():
        return {"ok": True, "kb_documents": len(storage.list_documents())}

    return app


def _now() -> int:
    import time
    return int(time.time())


def main() -> None:
    cfg = load_wecom_config()
    app = make_app()
    print(f"WeCom bot listening on http://{cfg.host}:{cfg.port}")
    print("Configure this URL in the WeCom admin console:")
    print(f"  http://<your-public-host>:{cfg.port}/wecom/callback")
    print("See IM_SETUP.md for ngrok instructions if you don't have a public host.")
    # Use 0.0.0.0 by default; turn off the dev-server reloader to keep crypto state stable.
    app.run(host=cfg.host, port=cfg.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()