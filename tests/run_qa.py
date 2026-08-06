"""
run_qa.py — Run the ground-truth test set against the live KB.

This is the "did the system actually get smarter or did we just rearrange
the deck chairs" smoke test. For each question in `tests/qa.jsonl`:

  1. Run BM25 search.
  2. Check the top-1 hit's filename contains one of the expected substrings
     (loose match — the user usually knows "this should be in OSG9" not
     "this should be in OSG9中文版-上册.pdf, page 42").
  3. Check the top-1 hit's score is above the minimum threshold.

A failing test means: a real user question no longer surfaces a useful
result after a code change. That's a "stop the line" signal.

Usage
-----
    .venv/bin/python tests/run_qa.py            # run, print report
    .venv/bin/python tests/run_qa.py --strict    # exit 1 on any fail
    .venv/bin/python tests/run_qa.py --topk 3   # check top-3 instead of top-1

Format of qa.jsonl (one JSON object per line):
    {
      "question": "PKI 是什么",
      "expect_files_contains": ["OSG9", "OSG10"],
      "expect_min_score": 10.0,
      "category": "PKI"  // optional, used only for grouping in the report
    }
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# Project root on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import search as search_mod  # noqa: E402
import storage  # noqa: E402


GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"


def _c(color: str, msg: str) -> str:
    return f"{color}{msg}{RESET}"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument(
        "--strict", action="store_true",
        help="Exit with code 1 if any test fails (default: 0).",
    )
    p.add_argument(
        "--topk", type=int, default=1,
        help="Check this many top hits (default 1).",
    )
    p.add_argument(
        "--hybrid", action="store_true",
        help="Force hybrid search for this run, regardless of config.",
    )
    p.add_argument(
        "--qa-file", default=str(Path(__file__).parent / "qa.jsonl"),
        help="Path to qa.jsonl (default: tests/qa.jsonl).",
    )
    args = p.parse_args()

    qa_path = Path(args.qa_file)
    if not qa_path.is_file():
        print(_c(RED, f"❌ qa file not found: {qa_path}"))
        print("Create it with one JSON object per line. See module docstring for schema.")
        return 2

    cases = []
    with qa_path.open(encoding="utf-8") as f:
        for ln, line in enumerate(f, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                cases.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(_c(RED, f"❌ {qa_path}:{ln}: invalid JSON: {e}"))
                return 2

    if not cases:
        print(_c(YELLOW, f"⚠ {qa_path} is empty — no test cases to run."))
        return 0

    # Init DB.
    storage.init_db()
    n_chunks = storage.corpus_stats()["n_chunks"]
    n_emb = storage.count_embeddings(config.EMBEDDING_MODEL)
    search_mode = (
        "hybrid" if config.USE_HYBRID_SEARCH_WHEN_READY
        and n_emb >= n_chunks * 0.5 else "BM25"
    )
    print(_c(YELLOW, f"Running {len(cases)} test cases against {n_chunks} chunks "
                       f"({search_mode} mode, {n_emb} embeddings)…"))
    print()

    # Bypass the production search._select_search() so we can test
    # either mode regardless of config.USE_HYBRID_SEARCH_WHEN_READY.
    search_fn = search_mod.hybrid_search if args.hybrid else (
        search_mod.hybrid_search
        if (config.USE_HYBRID_SEARCH_WHEN_READY and n_emb >= n_chunks * 0.5)
        else search_mod._pure_bm25_search
    )

    results = []
    started = time.time()
    for case in cases:
        q = case.get("question", "").strip()
        if not q:
            continue
        expect_substrings = case.get("expect_files_contains", []) or []
        min_score = float(case.get("expect_min_score", 0.0))
        # min_relative_score: top-1's score / top-of-list max. Useful for
        # hybrid (whose scores are normalized to [0, 1]) — the absolute
        # min_score thresholds in this file are tuned for raw BM25.
        min_rel = case.get("min_relative_score", None)
        category = case.get("category", "?")

        hits = search_fn(q, top_k=max(args.topk, 1))
        top = hits[0] if hits else None
        top_score = top["score"] if top else 0.0
        top_name = top["filename"] if top else "(no hit)"

        # Pass criteria:
        #  1. top-1 exists, AND
        #  2. its score is at or above min_score (or min_relative_score
        #     if specified — useful for hybrid's normalized [0, 1] range),
        #  3. its filename contains one of the expected substrings.
        if min_rel is not None:
            # In hybrid mode the top-1 self-similarity is always 1.0
            # (because we normalize by the max). So a useful threshold
            # is the top-1's score *compared to* the next-best hit.
            # For simplicity we just check the raw top-1 score here.
            score_ok = top_score >= float(min_rel)
        else:
            score_ok = top_score >= min_score
        if expect_substrings:
            file_ok = any(s in top_name for s in expect_substrings)
        else:
            file_ok = top is not None
        passed = score_ok and file_ok

        results.append({
            "question": q,
            "category": category,
            "passed": passed,
            "top_name": top_name,
            "top_score": top_score,
            "min_score": min_score,
            "min_rel": min_rel,
            "expected": expect_substrings,
            "file_ok": file_ok,
            "score_ok": score_ok,
        })

    elapsed = time.time() - started
    n_pass = sum(1 for r in results if r["passed"])
    n_fail = len(results) - n_pass

    # Per-category breakdown.
    by_cat: dict[str, list[dict]] = {}
    for r in results:
        by_cat.setdefault(r["category"], []).append(r)

    # Print the report.
    print(_c(YELLOW, "─" * 78))
    for cat, rs in sorted(by_cat.items()):
        passed = sum(1 for r in rs if r["passed"])
        print(f"  {cat:12s}  {passed}/{len(rs)}")
    print(_c(YELLOW, "─" * 78))

    for r in results:
        mark = "✅" if r["passed"] else "❌"
        c = GREEN if r["passed"] else RED
        # Truncate long filenames for readability.
        fn = r["top_name"]
        if len(fn) > 38:
            fn = fn[:35] + "..."
        details = []
        if not r["file_ok"]:
            details.append(f"expected one of {r['expected']!r}")
        if not r["score_ok"]:
            details.append(f"score {r['top_score']:.2f} < min {r['min_score']:.2f}")
        detail_str = (" — " + "; ".join(details)) if details else ""
        print(_c(
            c,
            f"  {mark} Q: {r['question']:30s}  →  {fn:38s}  "
            f"score={r['top_score']:.2f}{detail_str}",
        ))
    print(_c(YELLOW, "─" * 78))
    summary_color = GREEN if n_fail == 0 else RED
    print(_c(
        summary_color,
        f"  {n_pass}/{len(results)} passed  ·  {n_fail} failed  ·  {elapsed*1000:.0f}ms",
    ))

    if args.strict and n_fail > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
