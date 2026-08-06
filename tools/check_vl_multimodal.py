"""
check_vl_multimodal.py — P0 sanity check: does the configured VL model
actually see images, or is it a text-only model that the gateway is
silently letting hallucinate?

Why this exists
---------------
vl_config.py defaults to MiniMax-M3 with the comment "M3 is natively
multimodal" — but M3 is reached via an Anthropic-compatible endpoint
(https://api.minimaxi.com/anthropic), not a native multimodal API.
Worst case: the model silently ignores the image block, writes a
plausible-looking paragraph from its training data, and our entire
image-ingest pipeline becomes garbage-in / garbage-out.

This script makes 3 probing calls and prints a clear pass/fail verdict.

Usage
-----
    .venv/bin/python tools/check_vl_multimodal.py <path/to/test.jpg>

If you don't have a test image handy, pass --no-image and we'll
just ask the model "are you multimodal?" — but that's a weaker test
(models often claim they are, even when their image-handling is
broken in subtle ways).
"""
from __future__ import annotations

import argparse
import base64
import mimetypes
import sys
import textwrap
from pathlib import Path


GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"


def _c(color: str, msg: str) -> str:
    return f"{color}{msg}{RESET}"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument(
        "image", nargs="?",
        help="Path to a test image. Use --no-image for a weaker text-only probe.",
    )
    p.add_argument(
        "--no-image", action="store_true",
        help="Skip the image probes; only do the text-only 'are you multimodal?' probe.",
    )
    args = p.parse_args()

    try:
        import anthropic  # noqa: F401
    except ImportError:
        print(_c(RED, "❌ anthropic SDK not installed. Run: pip install anthropic"))
        return 2

    import vl_config
    cfg = vl_config.load_vl_config()
    client = anthropic.Anthropic(
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        timeout=cfg.timeout_s,
        max_retries=0,
    )
    print(_c(YELLOW, f"VL config: model={cfg.model}  base_url={cfg.base_url}  timeout={cfg.timeout_s}s"))
    print()

    fail = 0

    # ------------------------------------------------------------------
    # Probe 1: text-only "are you multimodal?" — weak signal
    # ------------------------------------------------------------------
    print(_c(YELLOW, "Probe 1/3  text-only self-report"))
    try:
        r = client.messages.create(
            model=cfg.model,
            max_tokens=80,
            messages=[{"role": "user", "content": [
                {"type": "text", "text":
                    "请用一句话回答:你能理解图片吗?能的话回复「能」,"
                    "不能的话回复「不能」,其他什么都不要输出。"},
            ]}],
        )
        out = "".join(b.text for b in r.content if getattr(b, "type", None) == "text").strip()
        print(f"  → model says: {out!r}")
        if "不能" in out or "no" in out.lower():
            print(_c(RED, "  ❌ Model self-reports as text-only. STOP — image pipeline is broken."))
            fail = 1
        elif "能" in out or "yes" in out.lower():
            print(_c(GREEN, "  ✅ Self-report OK (weak signal — keep going)"))
        else:
            print(_c(YELLOW, "  ⚠️  Unclear answer, continuing to image probes…"))
    except Exception as e:
        print(_c(RED, f"  ❌ text-only call failed: {e}"))
        return 2

    if args.no_image or not args.image:
        print()
        print(_c(YELLOW, "Skipping image probes (no image provided). Run with a real image for a real test."))
        return 0 if fail == 0 else 1

    image_path = Path(args.image)
    if not image_path.is_file():
        print(_c(RED, f"❌ not a file: {image_path}"))
        return 2

    mt, _ = mimetypes.guess_type(str(image_path))
    if not mt or not mt.startswith("image/"):
        print(_c(RED, f"❌ not an image: {image_path} (mime={mt})"))
        return 2

    data = image_path.read_bytes()
    if len(data) < 100:
        print(_c(RED, f"❌ image too small ({len(data)} bytes), probably not a real image"))
        return 2
    img_b64 = base64.standard_b64encode(data).decode("ascii")

    # ------------------------------------------------------------------
    # Probe 2: read a single character from a known image
    # ------------------------------------------------------------------
    # The user is told: "in the test image, the first visible text/character
    # is X" — they paste the expected answer, and we compare.
    # ------------------------------------------------------------------
    print()
    print(_c(YELLOW, "Probe 2/3  read a specific character from the image"))
    print("  (we don't know what's in your image, so we'll ask the model")
    print("   to transcribe the first 50 characters; you eyeball-verify)")
    try:
        r = client.messages.create(
            model=cfg.model,
            max_tokens=200,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": mt, "data": img_b64,
                }},
                {"type": "text", "text":
                    "请只输出这张图片里能读到的前 50 个字/字符,按原顺序。"
                    "不要总结、不要评论、不要解释、不要加任何前缀(如'图片显示')。"
                    "如果图片里完全没文字,只输出一个字:「空」。"},
            ]}],
        )
        out = "".join(b.text for b in r.content if getattr(b, "type", None) == "text").strip()
        print(f"  → model output: {out[:200]!r}")
        if out in ("空", "无文字", "无内容", "no text", "empty", ""):
            print(_c(YELLOW, "  ⚠️  Model says image is empty — verify that's actually true"))
        else:
            print(_c(YELLOW, "  → eyeball-check: does the output match what's actually in your image?"))
            print(_c(YELLOW, "    (this is the key test — if it doesn't match, image is fabricated)"))
    except Exception as e:
        print(_c(RED, f"  ❌ image call failed: {e}"))
        fail = 1

    # ------------------------------------------------------------------
    # Probe 3: ask a question with a known answer (count things, read a number)
    # ------------------------------------------------------------------
    print()
    print(_c(YELLOW, "Probe 3/3  answer a verifiable question about the image"))
    try:
        r = client.messages.create(
            model=cfg.model,
            max_tokens=200,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": mt, "data": img_b64,
                }},
                {"type": "text", "text":
                    "请数一下这张图片里能数得清的对象(字/词/数字/图形均可),"
                    "然后只输出一个数字(不要输出其他任何东西)。"
                    "如果完全数不清,只输出 0。"},
            ]}],
        )
        out = "".join(b.text for b in r.content if getattr(b, "type", None) == "text").strip()
        print(f"  → model output: {out!r}")
        # We can't auto-verify the number, but if the model produced a
        # plausible number AND its magnitude is reasonable for the image
        # size, that's a positive signal.
        first_token = out.split()[0] if out.split() else ""
        if first_token.isdigit():
            n = int(first_token)
            if 1 <= n <= 1000:
                print(_c(GREEN, f"  ✅ numeric answer {n} is plausible (eyeball-check it)"))
            else:
                print(_c(YELLOW, f"  ⚠️  numeric answer {n} is out of plausible range — could be hallucinated"))
        else:
            print(_c(YELLOW, f"  ⚠️  model didn't return a clean number (got: {out[:60]!r})"))
    except Exception as e:
        print(_c(RED, f"  ❌ image call failed: {e}"))
        fail = 1

    print()
    print("=" * 60)
    if fail == 0:
        print(_c(GREEN, "✅ All probes completed without API errors."))
        print(_c(GREEN, "   Now eyeball-check Probe 2 + 3 outputs against your image."))
        print(_c(GREEN, "   If both match → VL model is real, ingest pipeline is OK."))
        print(_c(GREEN, "   If they don't match → model is hallucinating, image pipeline is broken."))
        return 0
    else:
        print(_c(RED, "❌ At least one probe failed. See output above."))
        return 1


if __name__ == "__main__":
    sys.exit(main())
