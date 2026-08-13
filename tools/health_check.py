#!/usr/bin/env python3
"""
health_check.py — spot-check the KB against the ground-truth QA set,
                  and report any new /bad or /partial feedback from
                  today's bot usage.

Outputs a one-screen summary suitable for a cron tick.

Usage: python tools/health_check.py [--qa] [--feedback] [--days 1]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_qa_subset(n: int = 3) -> dict:
    """Pick `n` random QA cases and run them. Return summary dict."""
    qa_path = ROOT / "tests" / "qa.jsonl"
    if not qa_path.is_file():
        return {"error": "tests/qa.jsonl not found"}
    cases = [json.loads(line) for line in qa_path.read_text().splitlines() if line.strip()]
    import random
    random.seed(42)
    sample = random.sample(cases, min(n, len(cases)))
    # Delegate to run_qa.py
    tmp = ROOT / "tests" / "qa_sample.jsonl"
    tmp.write_text("\n".join(json.dumps(c) for c in sample) + "\n")
    try:
        result = subprocess.run(
            [".venv/bin/python", "tests/run_qa.py", "--input", str(tmp.relative_to(ROOT))],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
        )
        out = result.stdout + result.stderr
        # Parse: count "passed" / "failed" lines
        passed = out.count("✅")
        failed = out.count("❌")
        return {"sampled": n, "passed": passed, "failed": failed, "output_tail": out[-500:]}
    except subprocess.TimeoutExpired:
        return {"error": "run_qa.py timed out"}
    finally:
        tmp.unlink(missing_ok=True)


def read_recent_feedback(days: int = 1) -> list[dict]:
    """Read all feedback records from the last `days` days."""
    feedback_dir = ROOT / "data" / "feedback"
    if not feedback_dir.is_dir():
        return []
    records = []
    cutoff = date.today() - timedelta(days=days - 1)
    for f in sorted(feedback_dir.glob("*.jsonl")):
        try:
            d = date.fromisoformat(f.stem)
        except ValueError:
            continue
        if d < cutoff:
            continue
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def feedback_summary(records: list[dict]) -> dict:
    """Bucket feedback by verdict."""
    summary = {"good": 0, "bad": 0, "partial": 0, "other": 0}
    bad_queries = []
    for r in records:
        v = r.get("verdict", "other")
        summary[v if v in summary else "other"] += 1
        if v == "bad":
            bad_queries.append(r.get("query", "?")[:60])
    return {"counts": summary, "bad_samples": bad_queries[:5]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--qa", action="store_true", help="run QA spot-check")
    ap.add_argument("--feedback", action="store_true", help="read recent feedback")
    ap.add_argument("--days", type=int, default=1, help="feedback window in days")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    out = {}
    if args.qa:
        out["qa"] = run_qa_subset(3)
    if args.feedback:
        records = read_recent_feedback(args.days)
        out["feedback"] = {
            "window_days": args.days,
            "total": len(records),
            **feedback_summary(records),
        }

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        if "qa" in out:
            qa = out["qa"]
            if "error" in qa:
                print(f"QA: ERROR — {qa['error']}")
            else:
                print(f"QA spot-check: {qa['passed']}/{qa['sampled']} passed, {qa['failed']} failed")
        if "feedback" in out:
            fb = out["feedback"]
            print(f"Feedback ({fb['window_days']}d): total={fb['total']} "
                  f"good={fb['counts']['good']} bad={fb['counts']['bad']} "
                  f"partial={fb['counts']['partial']}")
            if fb["bad_samples"]:
                print("Recent bad queries:")
                for q in fb["bad_samples"]:
                    print(f"  - {q}")
        if not out:
            print("(nothing to do — pass --qa and/or --feedback)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
