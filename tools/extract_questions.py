"""
extract_questions.py — Extract MC questions from the 综合测试 PDFs (KB chunks).

Why this version is robust:
  - The KB chunks for these docs are split mid-question (the chunker cuts at
    ~400 chars regardless of question boundaries). So we see things like
    "A.Block packets with" on one line (truncated), then later
    "A.Block packets with internal source addresses... (正确答案)" (complete).
  - Naive line-dedup misses these because the partial line has a unique prefix.
  - This version does:
      1. Per-page concatenation with full line-set dedup (also fuzzy-merge
         lines that share a long prefix — handles "A.Block packets with" being
         a strict prefix of "A.Block packets with internal...").
      2. Option-letter de-duplication: when "A." appears multiple times in a
         question block, keep the LONGEST one.
      3. Orphan line filter: drop lines that are clearly sentence fragments
         (no Q-prefix, no option letter, no known marker).

Usage:
  python tools/extract_questions.py --doc "综合测试一.pdf"
  python tools/extract_questions.py                    # all 4 docs
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "kb.sqlite"
OUT_PATH = ROOT / "exam" / "questions.json"

TEST_DOCS = [
    "综合测试一.pdf",
    "综合测试二.pdf",
    "综合测试三.pdf",
    "综合测试四.pdf",
]

RE_QNUM = re.compile(r"^(\d{1,3})\.\s+[A-Z]")
# Match A./B./C./D. at line start. OCR sometimes drops the space after the
# period, so we accept either "A.foo" or "A. foo".
RE_OPT = re.compile(r"^([A-D])\.(?:\s+|\S)")
RE_CORRECT = re.compile(r"(\S.*?)\s*[(\uff08]正确答案[)\uff09]\s*$")
RE_CORRECT_INLINE = re.compile(r"[(\uff08]正确答案[)\uff09]")
RE_EXPLAIN_START = re.compile(r"答案解析[::\uff1a]\s*")
RE_TYPE = re.compile(r"\[(单选题|多选题)\]")
# Lines that look like orphan fragments: very short, no Q/opt marker, no
# Chinese-question-start, no period-ending sentence.
RE_ORPHAN = re.compile(
    r"^[^A-Za-z\uff00-\uffef\d\[\(]|^.{0,4}$"
    r"|^[A-Z][a-z]+\s+[a-z]+,\s+[a-z]+$"  # English word fragments
)


def is_orphan(line: str) -> bool:
    """Heuristic: a line that's a question fragment, not a real sentence."""
    s = line.strip()
    if not s:
        return False
    # Has option letter?
    if RE_OPT.match(s):
        return False
    # Has Q-number?
    if re.match(r"^\d{1,3}\.\s", s):
        return False
    # Has type marker?
    if RE_TYPE.search(s):
        return False
    # Has [单选题] or [多选题] or 答案解析?
    if "答案解析" in s or "正确答案" in s:
        return False
    # Very short line with no period? Probably a fragment.
    if len(s) < 12 and not s.endswith((".", "。", ":", "：", "?", "？", "!")):
        return True
    # Starts with lowercase letter (sentence continuation)?
    if re.match(r"^[a-z]", s):
        return True
    # Has internal-source-address-style fragments like "A is a B"?
    return False


def get_doc_id(db: sqlite3.Connection, filename: str) -> int | None:
    row = db.execute(
        "SELECT id FROM documents WHERE filename = ?", (filename,)
    ).fetchone()
    return row[0] if row else None


def get_page_text(db: sqlite3.Connection, doc_id: int) -> dict[int, str]:
    """Return {page_num: text} with aggressive dedup at page level."""
    rows = db.execute(
        "SELECT page_num, chunk_text FROM chunks "
        "WHERE doc_id = ? ORDER BY page_num, chunk_index",
        (doc_id,),
    ).fetchall()
    pages: dict[int, list[str]] = {}
    for pn, text in rows:
        if pn is None:
            continue
        pages.setdefault(pn, []).append(text)
    out = {}
    for pn, pieces in pages.items():
        full = "\n".join(pieces)
        # 1. Drop exact-duplicate lines
        seen = set()
        deduped = []
        for line in full.splitlines():
            s = line.strip()
            if not s:
                deduped.append(line)
                continue
            if s in seen:
                continue
            seen.add(s)
            deduped.append(line)
        # 2. Fuzzy-merge: if a line is a strict prefix of a later line on the
        # same page, drop the prefix version.
        final = []
        for i, line in enumerate(deduped):
            s = line.strip()
            if not s:
                final.append(line)
                continue
            # Check if some later line starts with s + space
            replaced = False
            for j in range(i + 1, min(i + 6, len(deduped))):
                t = deduped[j].strip()
                if t.startswith(s + " ") and len(t) > len(s) + 5:
                    # The line at j is a fuller version of line at i
                    # Skip i, keep j
                    replaced = True
                    break
            if not replaced:
                final.append(line)
        out[pn] = "\n".join(final)
    return out


def extract_options(block: str) -> tuple[list[dict], str]:
    """Extract A/B/C/D options from a question block. Returns (options, rest_text).

    Handles fragmented options: if the same letter appears multiple times,
    keeps the LONGEST one (which has the full text + 正确答案 marker).
    """
    lines = block.splitlines()
    # Find first line that starts with A./B./C./D.
    opt_start = None
    for i, line in enumerate(lines):
        if RE_OPT.match(line.strip()):
            opt_start = i
            break
    if opt_start is None:
        return [], ""

    # Group lines by option letter
    groups: dict[str, list[str]] = {"A": [], "B": [], "C": [], "D": []}
    current_letter = None
    for idx, line in enumerate(lines[opt_start:]):
        s = line.strip()
        m = RE_OPT.match(s)
        if m:
            current_letter = m.group(1)
            # Strip "A." prefix (could be "A.foo" or "A. foo")
            text = s[2:].lstrip() if len(s) > 2 and s[2] == ' ' else s[2:]
            # If next line is just "(正确答案)", absorb it into this option
            if idx + 1 < len(lines[opt_start:]):
                next_line = lines[opt_start + idx + 1].strip()
                if next_line in ("(正确答案)", "（正确答案）"):
                    text = text + " (正确答案)"
            groups[current_letter].append(text)
        elif current_letter:
            # Stop at 答案解析
            if RE_EXPLAIN_START.match(s):
                current_letter = None
                continue
            # If this line is just "(正确答案)", it might belong to the previous option
            if s in ("(正确答案)", "（正确答案）"):
                # Promote the previous option candidate to correct
                if groups[current_letter]:
                    last = groups[current_letter][-1]
                    if "(正确答案)" not in last and "（正确答案）" not in last:
                        groups[current_letter][-1] = last + " (正确答案)"
                continue
            if s:
                groups[current_letter].append(s)

    options = []
    for letter in ["A", "B", "C", "D"]:
        candidates = groups[letter]
        if not candidates:
            continue
        # Take the longest candidate
        best = max(candidates, key=len)
        is_correct = bool(RE_CORRECT_INLINE.search(best))
        clean = RE_CORRECT_INLINE.sub("", best).strip()
        if clean:
            options.append({
                "letter": letter,
                "text": clean,
                "is_correct": is_correct,
            })

    # Find where options end and explanation begins
    rest = ""
    for line in lines[opt_start:]:
        if RE_EXPLAIN_START.match(line.strip()):
            rest = "\n".join(lines[lines.index(line):])
            break
    return options, rest


def parse_question_block(blk: str) -> dict | None:
    if not blk.strip():
        return None
    # Question text = everything before first option line
    m_opt = None
    for line in blk.splitlines():
        m = RE_OPT.match(line.strip())
        if m:
            m_opt = m
            break
    if m_opt is None:
        return None

    # Get question text (filtering orphan fragments)
    q_lines = []
    for line in blk.splitlines():
        s = line.strip()
        if RE_OPT.match(s):
            break
        if is_orphan(s):
            continue
        q_lines.append(s)
    qtext = "\n".join(q_lines).strip()

    options, rest = extract_options(blk)
    if len(options) < 2:
        return None

    # Question type
    qtype = "unknown"
    if RE_TYPE.search(blk):
        qtype = "multi" if "多选题" in blk else "single"

    # Correct answer letters
    correct = [o["letter"] for o in options if o["is_correct"]]

    # Explanation
    explanation = ""
    m = RE_EXPLAIN_START.search(rest)
    if m:
        explanation = rest[m.end():].strip()

    # If no correct marked, try to find "正确答案: A" in explanation
    if not correct:
        m = re.search(r"正确答案[::]\s*([A-D](?:\s*[,\s、]\s*[A-D])*)", explanation)
        if m:
            correct = [c.strip() for c in re.split(r"[,\s、]+", m.group(1)) if c.strip()]
            for o in options:
                if o["letter"] in correct:
                    o["is_correct"] = True

    # Question number
    m = re.match(r"^(\d{1,3})\.\s", blk.lstrip())
    qnum = int(m.group(1)) if m else None

    return {
        "qnum": qnum,
        "qtype": qtype,
        "question": qtext,
        "options": options,
        "correct": correct,
        "explanation": explanation,
    }


def extract_questions(db: sqlite3.Connection, doc: str) -> list[dict]:
    doc_id = get_doc_id(db, doc)
    if doc_id is None:
        return []
    pages = get_page_text(db, doc_id)
    if not pages:
        return []
    full_text = "\n".join(pages[pn] for pn in sorted(pages))
    lines = full_text.splitlines()

    # Split into blocks on Q-number starts
    blocks = []
    current = []
    for line in lines:
        s = line.strip()
        m = re.match(r"^(\d{1,3})\.\s+([A-Z\u4e00-\u9fff])", s)
        if m and 1 <= int(m.group(1)) <= 999:
            if current:
                blocks.append("\n".join(current))
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append("\n".join(current))

    questions = []
    for blk in blocks:
        parsed = parse_question_block(blk)
        if parsed:
            parsed["source_doc"] = doc
            questions.append(parsed)
    return questions


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", help="only extract this doc")
    ap.add_argument("--out", default=str(OUT_PATH))
    ap.add_argument("--sample", type=int, default=3)
    args = ap.parse_args()

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    try:
        all_q = []
        docs = [args.doc] if args.doc else TEST_DOCS
        for doc in docs:
            qs = extract_questions(db, doc)
            print(f"  {doc}: {len(qs)} questions parsed")
            all_q.extend(qs)
    finally:
        db.close()

    OUT_PATH.write_text(json.dumps(all_q, ensure_ascii=False, indent=2))
    print(f"\nWrote {len(all_q)} questions to {OUT_PATH}")
    n_correct = sum(1 for q in all_q if q["correct"])
    n_single = sum(1 for q in all_q if q["qtype"] == "single")
    n_multi = sum(1 for q in all_q if q["qtype"] == "multi")
    print(f"  single: {n_single}, multi: {n_multi}, with_correct_answer: {n_correct}")

    if args.sample > 0 and all_q:
        # Filter to questions with valid options + answer for sampling
        valid = [q for q in all_q if q["correct"] and len(q["options"]) >= 2]
        sample = valid[: args.sample]
        print(f"\n=== SAMPLE {len(sample)} VALID QUESTIONS ===")
        for q in sample:
            print(f"\n--- {q['source_doc']} #{q.get('qnum', '?')} ({q['qtype']}) ---")
            print(f"Q: {q['question'][:200]}")
            for o in q['options']:
                mark = "✓" if o['is_correct'] else " "
                print(f"  [{mark}] {o['letter']}. {o['text'][:100]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
