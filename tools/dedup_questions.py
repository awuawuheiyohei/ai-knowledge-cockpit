"""
dedup_questions.py — background scanner that merges near-duplicate
question files in data/questions/.

This is the offline counterpart to question_archive.save_question's
real-time fuzzy dedup: it catches duplicates that already exist on
disk (from before the real-time check was added, or that slipped
through because OCR was wildly different on two passes).

Usage:
  python tools/dedup_questions.py              # dry-run, list candidates
  python tools/dedup_questions.py --apply      # actually merge + delete
  python tools/dedup_questions.py --threshold 0.90  # tighter similarity

Strategy (apply mode):
  - group files by pairwise fuzzy similarity >= threshold
    (we use SequenceMatcher.ratio on the normalized English text)
  - within each group: keep the OLDEST file (earliest ctime), remove
    the rest
  - the SQLite table keeps both rows (harmless; matches the runtime
    fuzzy-dedup behaviour where we don't reach into storage internals
    to delete rows on fuzzy-hit)

What "oldest" means: filesystem mtime, falling back to the timestamp
embedded in the filename (`YYYYMMDD-HHMMSS-hash.md`) so the tiebreak
is deterministic even when two files were written in the same
minute.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Make the project root importable when running directly.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import question_archive  # for QUESTIONS_DIR
import storage  # for listing archived rows (informational)


logger = logging.getLogger("dedup_questions")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TS_RE = re.compile(r"^(\d{8}-\d{6})-")


def _english_from_md(path: Path) -> str:
    """Read back the English question from a saved .md (same format
    as question_archive._read_english_from_md, copied here to keep
    the tool self-contained)."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    m = re.search(r"## English\s*\n(.+?)(?=\n## |\Z)", text, flags=re.DOTALL)
    return m.group(1).strip() if m else ""


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def _tokens(text: str) -> set[str]:
    """Token set used for Jaccard similarity. Cheap word-level tokens
    (alphanumeric runs of length >= 2). Drops punctuation and single
    chars (which add noise: the letter "A" in option labels dominates)."""
    return {t for t in re.findall(r"[a-z0-9]{2,}", text.lower())}


def _similarity(a: str, b: str) -> float:
    """Jaccard similarity over token sets. O(n) per comparison vs
    SequenceMatcher's O(n^3); 65 files × 9-char tokens finishes in
    well under a second instead of timing out."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


def _sort_key(path: Path) -> tuple[str, str]:
    """Lower = older. Use filename timestamp prefix if present,
    else fall back to mtime ISO string."""
    m = _TS_RE.match(path.name)
    if m:
        return (m.group(1), "")
    try:
        return (datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y%m%d-%H%M%S"), "")
    except OSError:
        return ("zzzzzzzz", "")


# ---------------------------------------------------------------------------
# Scan + group
# ---------------------------------------------------------------------------

@dataclass
class Group:
    kept: Path
    removed: list[Path]
    similarity: float  # similarity between kept and the worst match


def scan(threshold: float) -> list[Group]:
    """Find groups of near-duplicate .md files. Returns one Group
    per cluster that needs merging (clusters of size 1 are skipped).
    """
    dir_ = question_archive.QUESTIONS_DIR
    if not dir_.exists():
        return []

    files = sorted(p for p in dir_.glob("*.md"))
    if not files:
        return []

    # Pairwise similarity scan — O(n^2) but the directory has ~65
    # files today and grows slowly, so this is fine. If we ever hit
    # thousands, swap in a minhash / LSH index.
    # Normalize once so the inner loop is cheap.
    bodies: dict[Path, str] = {p: _normalize(_english_from_md(p)) for p in files}
    bodies = {p: b for p, b in bodies.items() if b}  # skip empties

    parent: dict[Path, Path] = {p: p for p in bodies}

    def find(x: Path) -> Path:
        # Standard path-compression. The earlier `parent[x] = parent[parent[x]]`
        # is wrong — it skips nodes. Walk to root, then compress the path.
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(x: Path, y: Path) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            # union by sort key — older becomes root
            if _sort_key(rx) <= _sort_key(ry):
                parent[ry] = rx
            else:
                parent[rx] = ry

    paths = list(bodies.keys())
    for i, a in enumerate(paths):
        for b in paths[i + 1:]:
            if _similarity(bodies[a], bodies[b]) >= threshold:
                union(a, b)

    # Cluster
    clusters: dict[Path, list[Path]] = defaultdict(list)
    for p in paths:
        clusters[find(p)].append(p)

    # Build Group objects (skip singletons)
    groups: list[Group] = []
    for root_p, members in clusters.items():
        if len(members) < 2:
            continue
        members.sort(key=_sort_key)
        kept = members[0]
        removed = members[1:]
        worst_sim = min(
            _similarity(bodies[kept], bodies[m]) for m in removed
        )
        groups.append(Group(kept=kept, removed=removed, similarity=worst_sim))
    return groups


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def apply(groups: list[Group]) -> None:
    for g in groups:
        for victim in g.removed:
            try:
                victim.unlink()
                logger.info("deleted %s (kept=%s)", victim.name, g.kept.name)
            except OSError as e:
                logger.warning("failed to delete %s: %s", victim, e)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _report(groups: list[Group]) -> None:
    if not groups:
        print("✅ no duplicates found")
        return
    print(f"Found {len(groups)} duplicate group(s):")
    for i, g in enumerate(groups, 1):
        print(f"\n[{i}] keep: {g.kept}")
        print(f"    worst-similarity: {g.similarity:.3f}")
        print(f"    remove ({len(g.removed)}):")
        for v in g.removed:
            print(f"      - {v}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Dedupe archived question files.")
    parser.add_argument("--threshold", type=float, default=0.7,
                        help="Jaccard token-similarity cutoff (default 0.7)")
    parser.add_argument("--apply", action="store_true",
                        help="actually delete the duplicates (default is dry-run)")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    groups = scan(args.threshold)

    if args.json:
        out = [
            {
                "kept": str(g.kept),
                "removed": [str(v) for v in g.removed],
                "similarity": g.similarity,
            }
            for g in groups
        ]
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        _report(groups)

    if args.apply:
        apply(groups)
        print(f"\nMerged {len(groups)} group(s).")
    elif groups:
        print(f"\n(dry-run; pass --apply to actually merge)")

    return 0


if __name__ == "__main__":
    sys.exit(main())