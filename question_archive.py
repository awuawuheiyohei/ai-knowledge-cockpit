"""
question_archive.py — save OCR'd English CISSP questions for self-study.

User flow (DingTalk image input → practice mode):
  1. user sends a screenshot of an English CISSP question
  2. we OCR the question (image_extract.extract_text)
  3. we ask the LLM to translate the question to Chinese
     (no KB lookup, no answer generation — the user does the question
     themselves and only wants the Chinese so they can read it as
     study scaffolding)
  4. we write the file to data/questions/<timestamp>-<hash>.md
  5. we return the saved path so the reply can show it

Note: as of 2026-09-07 the user dropped the per-domain subdirectory
layout (intelligent classification was too inaccurate). All
questions now go into the single `data/questions/` folder. The
SQLite dedup table still tracks them so we don't re-archive
duplicates.

Dedup:
  - storage.archive_question() dedups by normalized text. The .md file
    is only written on a fresh archive (row_id is not None).
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from pathlib import Path

import storage

logger = logging.getLogger("question_archive")

# Single flat directory — no per-domain subdirs (2026-09-07 simplification,
# per user request: 8-way auto-classification wasn't accurate enough).
QUESTIONS_DIR = Path(__file__).resolve().parent / "data" / "questions"


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
    source: str = "",
    fuzzy_threshold: float = 0.88,
) -> dict:
    """Save the (English, Chinese) question pair to QUESTIONS_DIR.

    Returns a dict with:
      - path: Path to the .md file (or None if dedup hit)
      - is_new: True if newly written, False if already in archive
      - zh_text: the Chinese translation (always present, so the IM
                 reply can show it inline; re-translated on dedup hit)

    Dedup is two-tier:
      1. exact (storage.archive_question) — normalized-text UNIQUE key
      2. fuzzy (this function) — SequenceMatcher.ratio >= fuzzy_threshold
         against recent saved files; catches near-duplicates that the
         exact match misses (different OCR of the same screenshot,
         trailing whitespace, dropped question numbers, etc.)

    The .md file is only written when both tiers say "new".
    """
    en_text = (en_text or "").strip()
    if not en_text:
        return {"path": None, "is_new": False, "zh_text": ""}

    # Tier 1: normalized-text exact dedup.
    try:
        row_id = storage.archive_question(en_text, 0, source or "dingtalk")
    except Exception as e:  # noqa: BLE001
        logger.warning("archive_question failed: %s", e)
        return {"path": None, "is_new": False, "zh_text": ""}

    # Translate regardless of outcome — the IM reply always wants the
    # Chinese translation even on a dedup hit.
    zh_text = _translate_to_chinese(en_text)

    if row_id is None:
        # exact dedup hit — find the existing file
        existing = _find_existing_file(en_text)
        return {
            "path": existing,
            "is_new": False,
            "zh_text": zh_text,
            "fuzzy_match": False,
        }

    # Tier 2: fuzzy dedup — check if any existing file has very similar
    # text (different OCR of same question). If so, treat as duplicate
    # and DO NOT write a new file. The just-inserted SQLite row stays
    # (harmless — it's only ~80 bytes and we don't want to reach into
    # storage internals from here).
    fuzzy_hit = _fuzzy_find_existing(en_text, threshold=fuzzy_threshold)
    if fuzzy_hit is not None:
        logger.info(
            "fuzzy dedup hit (threshold=%.2f): new=%s, existing=%s",
            fuzzy_threshold, en_text[:40], fuzzy_hit.stem,
        )
        return {
            "path": fuzzy_hit,
            "is_new": False,
            "zh_text": zh_text,
            "fuzzy_match": True,
        }

    # 3. write file
    QUESTIONS_DIR.mkdir(parents=True, exist_ok=True)
    slug = _slug_hash(en_text)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = QUESTIONS_DIR / f"{ts}-{slug}.md"
    body = _render_markdown(
        en_text=en_text, zh_text=zh_text, source=source or "dingtalk",
    )
    path.write_text(body, encoding="utf-8")
    logger.info("saved question id=%d path=%s", row_id, path)
    return {
        "path": path,
        "is_new": True,
        "zh_text": zh_text,
        "fuzzy_match": False,
    }


def _normalize(text: str) -> str:
    """Local copy of the storage normalizer (lowercase + collapse ws).
    Kept here so we don't have to reach into storage internals."""
    return " ".join(text.lower().split())


def _fuzzy_find_existing(en_text: str, threshold: float) -> Path | None:
    """Scan the QUESTIONS_DIR for any .md whose English question is
    fuzzy-similar to en_text. Returns the existing path if a match is
    found above the threshold, else None.

    We only inspect the .md files (not the SQLite rows) because the
    fuzzy similarity is best computed on the exact English text we
    would have saved — not the normalized form used for the dedup key.
    """
    if not QUESTIONS_DIR.exists():
        return None
    from difflib import SequenceMatcher

    needle = _normalize(en_text)
    for p in QUESTIONS_DIR.glob("*.md"):
        body = _read_english_from_md(p)
        if not body:
            continue
        ratio = SequenceMatcher(None, needle, _normalize(body)).ratio()
        if ratio >= threshold:
            return p
    return None


def _read_english_from_md(path: Path) -> str:
    """Read back the English question text from a saved .md file. The
    file is laid out by _render_markdown with an `## English` header
    followed by the raw text, terminated by the next `## ` section.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    m = re.search(r"## English\s*\n(.+?)(?=\n## |\Z)", text, flags=re.DOTALL)
    return m.group(1).strip() if m else ""


def _find_existing_file(en_text: str) -> Path | None:
    """Best-effort lookup of an already-archived question's file path.
    The stored question_text is the normalized form, not the file
    name, so we scan the directory for any .md with the matching hash.
    """
    if not QUESTIONS_DIR.exists():
        return None
    slug = _slug_hash(en_text)
    for p in QUESTIONS_DIR.glob(f"*-{slug}.md"):
        return p
    return None


def _render_markdown(
    en_text: str, zh_text: str, source: str,
) -> str:
    """Format the saved .md file. English first, Chinese second —
    matches the user's reading order (read English to attempt,
    then check Chinese to verify understanding)."""
    en_text = en_text.strip()
    zh_text = zh_text.strip()
    parts: list[str] = []
    parts.append(f"# {datetime.now().isoformat(timespec='seconds')}")
    parts.append("")
    parts.append(f"**来源**: `{source}`")
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
