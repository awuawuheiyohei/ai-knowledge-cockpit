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


# ---------------------------------------------------------------------------
# Continuation detection (auto-merge consecutive screenshots)
# ---------------------------------------------------------------------------

# Heuristics guard rails — if any of these fires, we DO NOT merge.
# Listed positively so tests can assert the exact decision boundary.
_MAX_CONTINUATIONS_PER_QUESTION = 5  # hard safety cap
_TIME_WINDOW_SECONDS = 180  # default merge window
_TRUNCATED_RE = re.compile(r"\[truncated\]", re.IGNORECASE)
_NEW_QUESTION_START_RE = re.compile(r"^\s*(?:\d+[\.\)、]|q(?:uestion)?\s*\d+|question\b)",
                                   re.IGNORECASE)


def _strip_word_punct(s: str) -> list[str]:
    """Tokenize like .split() but also strip leading/trailing
    punctuation from every word so 'rotated.' and 'rotated' compare
    equal. Without this, a sentence-final period breaks trigram
    matching across the screenshot boundary."""
    out: list[str] = []
    for w in s.lower().split():
        out.append(w.strip(".,;:!?'\"()[]{}<>"))
    return [w for w in out if w]


_OPTION_LETTER_RE = re.compile(r"^\s*([A-Ea-e])[\.\)]")

# Common short words that frequently appear at sentence/clause boundaries
# in OCR text. We filter these out of the n=1 boundary alignment so a
# pair of unrelated screenshots that both happen to end/start with "the"
# doesn't get falsely merged.
_BOUNDARY_STOPWORDS = frozenset({
    "the", "and", "or", "is", "are", "was", "were", "be", "been",
    "a", "an", "of", "to", "in", "for", "on", "at", "by", "with",
    "as", "from", "this", "that", "it", "its", "if", "but", "not",
})


def _is_option_letter_continuation(prev_text: str, new_text: str) -> bool:
    """Detect: prev's last visible line is option X (A..E) and new's
    first line starts with option X+1. Strong signal that the new
    screenshot is the rest of the options list."""
    prev_lines = [ln for ln in prev_text.splitlines() if ln.strip()]
    new_lines = [ln for ln in new_text.splitlines() if ln.strip()]
    if not prev_lines or not new_lines:
        return False
    m_prev = _OPTION_LETTER_RE.match(prev_lines[-1])
    m_new = _OPTION_LETTER_RE.match(new_lines[0])
    if not (m_prev and m_new):
        return False
    prev_letter = m_prev.group(1).upper()
    new_letter = m_new.group(1).upper()
    expected = chr(ord(prev_letter) + 1)
    if expected > "E":
        return False
    return new_letter == expected


def _has_boundary_alignment(
    prev_tokens: list[str],
    new_tokens: list[str],
    min_token_len: int = 4,
) -> bool:
    """Detect suffix/prefix overlap at the prev/new boundary.

    Returns True when the last n tokens of `prev_tokens` equal the
    first n tokens of `new_tokens` for some n, with these guards:
      - n >= 2: any match counts (handles "...be rotated every ninety")
      - n == 1: only counts when the matched token is a content word
        (length >= min_token_len AND not in _BOUNDARY_STOPWORDS). This
        catches the mid-word OCR-cut case ("...rotated" / "rotated
        every...") without false-merging two unrelated screenshots
        that happen to both start/end with a stopword like "the".

    This replaces the old shared-trigram check, which required 3 tokens
    of overlap and missed the very common 1-token mid-word case.
    """
    if not prev_tokens or not new_tokens:
        return False
    max_n = min(len(prev_tokens), len(new_tokens))
    # n >= 2 first (any match wins)
    for n in range(max_n, 1, -1):
        if prev_tokens[-n:] == new_tokens[:n]:
            return True
    # n == 1: content-word only
    last = prev_tokens[-1]
    first = new_tokens[0]
    if last == first and len(last) >= min_token_len and last not in _BOUNDARY_STOPWORDS:
        return True
    return False


def _is_continuation(prev_text: str, new_text: str) -> bool:
    """Decide whether `new_text` is a continuation of `prev_text`.

    Conservative — only returns True when MULTIPLE independent signals
    align. False negatives (refusing to merge a real continuation) are
    cheap; false positives (silently merging two different questions)
    corrupt the archive, so we err on the side of refusing.

    Signals (1 and 2 are mandatory; 3a OR 3b is also required):
      1. prev_text contains `[TRUNCATED]` (set by the OCR prompt when
         it suspected the prior screenshot was cropped)
      2. new_text's first non-space character does NOT look like the
         start of a brand-new question (digit / "Question N" / "Q.")
      3a. boundary alignment: prev's last n tokens == new's first n
          tokens for n >= 2 (any) or n == 1 with content-word check.
          Catches both "...phrase continuation" (n>=2) and "...word
          continuation" (n=1, mid-word OCR cut).
      3b. option-letter continuation: prev's last visible line ends
          with option X (A..E) and new's first line starts with X+1.

    Tokens are stripped of leading/trailing punctuation before
    comparison so "rotated." ≈ "rotated".
    """
    if not prev_text or not new_text:
        return False
    if not _TRUNCATED_RE.search(prev_text):
        return False
    if _NEW_QUESTION_START_RE.match(new_text):
        return False

    prev_clean = _TRUNCATED_RE.sub("", prev_text).strip()
    if not prev_clean.strip():
        return False

    prev_tokens = _strip_word_punct(prev_clean)[-50:]
    new_tokens = _strip_word_punct(new_text)[:30]
    if len(prev_tokens) < 3 or len(new_tokens) < 3:
        return False

    if _has_boundary_alignment(prev_tokens, new_tokens):
        return True
    if _is_option_letter_continuation(prev_clean, new_text):
        return True
    return False


def append_continuation(
    en_text: str,
    source: str,
    seconds: int = _TIME_WINDOW_SECONDS,
    max_per_question: int = _MAX_CONTINUATIONS_PER_QUESTION,
) -> dict | None:
    """Try to append `en_text` to the most recent truncated archive
    from `source` within `seconds`.

    Returns:
      - None: no continuation candidate (caller should fall through
        to save_question)
      - dict: {path, is_new=False, zh_text, fuzzy_match=False,
                      was_continuation=True, merged_from_id=<old_id>,
                      merged_len=<new len>}

    The merge is precise:
      - prev had `[TRUNCATED]` (set by OCR)
      - new is not a new question start
      - at least one shared trigram across the boundary
      - within the time window from same source
      - dedup against the merged text (collision is rolled back)

    Hard safety: each row's question_text is capped at
    `_MAX_CONTINUATIONS_PER_QUESTION` implied continuations by
    counting `[TRUNCATED]` markers in the previous archive — if
    too many were already merged into the candidate, we don't merge
    again.
    """
    if not en_text.strip():
        return None

    candidates = storage.find_recent_archives(
        source=source,
        seconds=seconds,
        requires_truncated=True,
        max_count=max_per_question,
    )
    for cand in candidates:
        prev_text = cand["question_text"]

        # Safety cap: refuse to merge if the candidate has already
        # absorbed too many continuations. Each merge strips one
        # [TRUNCATED] marker from `prev_text` and adds no new marker,
        # so counting markers in `prev_text` approximates the
        # number of unfinished continuations still pending. (0 markers
        # means the prev wasn't truncated — but we filter on
        # requires_truncated=True so this branch is defensive only.)
        prev_marker_count = len(_TRUNCATED_RE.findall(prev_text))
        if prev_marker_count == 0:
            # Already-merged (someone else completed it). Skip.
            continue

        if not _is_continuation(prev_text, en_text):
            continue

        # Merge: strip the [TRUNCATED] marker, append new text.
        prev_clean = _TRUNCATED_RE.sub(" ", prev_text).strip()
        merged_text = (prev_clean + " " + en_text.strip()).strip()

        # Re-translate the FULL merged text so the Chinese translation
        # is consistent with the new English (cheap: one LLM call).
        zh_text = _translate_to_chinese(merged_text)

        # Update SQLite atomically (delete old, insert new — both in
        # the same tx so concurrent reads see a coherent state).
        try:
            new_id = storage.replace_archived_text(cand["id"], merged_text)
        except Exception as e:  # noqa: BLE001
            logger.warning("merge DB update failed: %s", e)
            return None
        if new_id is None:
            logger.info(
                "merge dedup collision on id=%d — merged text already exists",
                cand["id"],
            )
            return None

        # Rewrite .md file at a fresh path (timestamp + new hash).
        old_path = _find_existing_file(prev_text)
        new_slug = _slug_hash(merged_text)
        new_ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        new_path = QUESTIONS_DIR / f"{new_ts}-{new_slug}.md"
        body = _render_markdown(
            en_text=merged_text, zh_text=zh_text, source=source,
        )
        new_path.write_text(body, encoding="utf-8")
        if old_path and old_path != new_path:
            try:
                old_path.unlink()
            except OSError:
                pass

        logger.info(
            "merged continuation: old_id=%d -> new_id=%d "
            "(len %d -> %d, source=%s)",
            cand["id"], new_id, len(prev_text), len(merged_text), source,
        )
        return {
            "path": new_path,
            "is_new": False,
            "zh_text": zh_text,
            "fuzzy_match": False,
            "was_continuation": True,
            "merged_from_id": cand["id"],
            "merged_len": len(merged_text),
        }

    return None


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
