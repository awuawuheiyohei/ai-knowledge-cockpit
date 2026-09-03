"""
question_archive.py — save OCR'd English CISSP questions for self-study.

User flow (DingTalk image input → practice mode):
  1. user sends a screenshot of an English CISSP question
  2. we OCR the question (image_extract.extract_text)
  3. we classify it to one of the 8 CISSP domains (_classify_domain_via_llm)
  4. we ask the LLM to translate the question to Chinese
     (no KB lookup, no answer generation — the user does the question
     themselves and only wants the Chinese so they can read it as
     study scaffolding)
  5. we write the file to data/questions/域N/<timestamp>-<hash>.md
  6. we return the saved path so the reply can show it

Dedup:
  - storage.archive_question() already dedups by normalized text (returns
    None if the question was already archived). The .md file is only
    written on a fresh archive (row_id is not None).
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from pathlib import Path

import storage

logger = logging.getLogger("question_archive")

QUESTIONS_DIR = Path(__file__).resolve().parent / "data" / "questions"

# Same domain names as im_router._DOMAIN_NAMES — kept in sync deliberately.
# If they diverge, the saved file's "域N" line will disagree with the
# domain_name column in archived_questions.
DOMAIN_NAMES: dict[int, str] = storage.CISSP_DOMAIN_NAMES


def _slug_hash(text: str) -> str:
    """Short stable hash of the normalized question, for filenames."""
    norm = " ".join(text.lower().split())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:10]


def _translate_to_chinese(en_text: str) -> str:
    """Ask the LLM to translate an English CISSP question to Chinese.
    No KB, no extra context — pure translation. Returns the raw text
    (may be empty on failure).

    Uses the same Anthropic client + config as answer_synth so no new
    credentials needed. The LLM prompt is short and explicit so the
    output stays focused on the question (no model commentary).
    """
    if not en_text or not en_text.strip():
        return ""
    try:
        import llm_config
        import anthropic
    except ImportError:
        return ""
    if not llm_config.is_llm_configured():
        return ""
    try:
        cfg = llm_config.load_llm_config()
        client = anthropic.Anthropic(
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            timeout=cfg.timeout_s,
            max_retries=0,
        )
        resp = client.messages.create(
            model=cfg.model,
            max_tokens=cfg.synth_max_tokens,
            messages=[{
                "role": "user",
                "content": (
                    "你是一个英中翻译。把下面这段 CISSP 考试英文题目"
                    "忠实地翻译成中文,**只翻译,不要回答、不要解释、不要加任何评论**。"
                    "如果原文有 A/B/C/D 选项,保留选项标记。\n\n"
                    f"{en_text[:3000]}"
                ),
            }],
        )
        out = "".join(
            getattr(b, "text", "")
            for b in resp.content
            if getattr(b, "type", None) == "text"
        ).strip()
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("translate_to_chinese failed: %s", e)
        return ""


def save_question(
    en_text: str,
    domain: int,
    source: str = "",
) -> dict:
    """Save the (English, Chinese) question pair to the per-domain folder.

    Returns a dict with:
      - path: Path to the .md file (or None if dedup hit / no domain)
      - is_new: True if newly written, False if already in archive
      - domain, domain_name: the domain fields

    Dedup is done by storage.archive_question() (normalized text key).
    The .md file is only written when archive_question returns a new id.
    """
    en_text = (en_text or "").strip()
    if not en_text or domain not in DOMAIN_NAMES:
        return {"path": None, "is_new": False, "domain": domain,
                "domain_name": DOMAIN_NAMES.get(domain)}

    # 1. archive (dedup by normalized text)
    try:
        row_id = storage.archive_question(en_text, domain, source or "dingtalk")
    except Exception as e:  # noqa: BLE001
        logger.warning("archive_question failed: %s", e)
        return {"path": None, "is_new": False, "domain": domain,
                "domain_name": DOMAIN_NAMES[domain]}

    if row_id is None:
        # already archived — don't write a duplicate file
        # (find the existing file for path reporting)
        existing = _find_existing_file(en_text, domain)
        return {
            "path": existing,
            "is_new": False,
            "domain": domain,
            "domain_name": DOMAIN_NAMES[domain],
        }

    # 2. translate (best effort — empty string is acceptable; the .md
    # will just have an empty 中文 section the user can fill in)
    zh_text = _translate_to_chinese(en_text)

    # 3. write file
    domain_name = DOMAIN_NAMES[domain]
    dir_ = QUESTIONS_DIR / f"域{domain}"
    dir_.mkdir(parents=True, exist_ok=True)
    slug = _slug_hash(en_text)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = dir_ / f"{ts}-{slug}.md"
    body = _render_markdown(
        en_text=en_text, zh_text=zh_text,
        domain=domain, domain_name=domain_name,
        source=source or "dingtalk",
    )
    path.write_text(body, encoding="utf-8")
    logger.info(
        "saved question id=%d domain=域%d path=%s",
        row_id, domain, path,
    )
    return {
        "path": path,
        "is_new": True,
        "domain": domain,
        "domain_name": domain_name,
    }


def _find_existing_file(en_text: str, domain: int) -> Path | None:
    """Best-effort lookup of an already-archived question's file path.
    The stored question_text is the normalized form, not the file
    name, so we scan the directory for any .md with the matching hash.
    """
    if domain not in DOMAIN_NAMES:
        return None
    slug = _slug_hash(en_text)
    dir_ = QUESTIONS_DIR / f"域{domain}"
    if not dir_.exists():
        return None
    for p in dir_.glob(f"*-{slug}.md"):
        return p
    return None


def _render_markdown(
    en_text: str, zh_text: str,
    domain: int, domain_name: str, source: str,
) -> str:
    """Format the saved .md file. English first, Chinese second —
    matches the user's reading order (read English to attempt,
    then check Chinese to verify understanding)."""
    en_text = en_text.strip()
    zh_text = zh_text.strip()
    parts: list[str] = []
    parts.append(f"# 域{domain} · {domain_name}")
    parts.append("")
    parts.append(f"**来源**: `{source}`")
    parts.append(f"**归档时间**: {datetime.now().isoformat(timespec='seconds')}")
    parts.append("")
    parts.append("## English")
    parts.append("")
    parts.append(en_text)
    parts.append("")
    parts.append("## 中文")
    parts.append("")
    if zh_text:
        parts.append(zh_text)
    else:
        parts.append("_(LLM 翻译失败,请手动补充)_")
    parts.append("")
    return "\n".join(parts)
