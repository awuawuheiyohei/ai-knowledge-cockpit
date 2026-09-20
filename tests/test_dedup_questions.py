"""
test_dedup_questions.py — unit tests for the offline dedup scanner.

Covers:
  - Jaccard similarity helper (>= threshold considered duplicate)
  - End-to-end scan: 3 files, 1 near-dup pair, 1 unique
  - Apply mode actually unlinks the chosen victims
  - Filename timestamp prefix is the tiebreaker for "oldest"
  - Empty / unreadable files are skipped, not crashed

Run: .venv/bin/python tests/test_dedup_questions.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _make_md(path: Path, english: str) -> None:
    """Write a minimal valid .md with the given English question so
    the scanner's regex picks it up."""
    path.write_text(
        f"# 2026-09-20T00:00:00\n\n**来源**: `test`\n\n"
        f"## English\n\n{english}\n\n## 中文\n\n中文测试\n",
        encoding="utf-8",
    )


class TestDedupScanner(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.qdir = Path(self.tmp.name) / "questions"
        self.qdir.mkdir(parents=True, exist_ok=True)

        # Redirect the question_archive output dir to the temp dir.
        import question_archive
        question_archive.QUESTIONS_DIR = self.qdir  # type: ignore[attr-defined]
        # also make sure storage uses a temp DB (in case anything logs)
        import storage
        storage.DB_PATH = Path(self.tmp.name) / "kb.sqlite"  # type: ignore[attr-defined]
        storage.init_db()

        # Import the tool AFTER patching QUESTIONS_DIR
        sys.path.insert(0, str(ROOT / "tools"))
        import dedup_questions
        self.scanner = dedup_questions

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name: str, english: str) -> Path:
        p = self.qdir / name
        _make_md(p, english)
        return p

    def test_jaccard_above_threshold(self):
        self.assertGreaterEqual(
            self.scanner._similarity("what is rbac?", "what is RBAC?"),
            0.7,
        )

    def test_jaccard_below_threshold(self):
        # completely different questions
        self.assertLess(
            self.scanner._similarity(
                "What is RBAC?",
                "Why is the sky blue?",
            ),
            0.3,
        )

    def test_scan_finds_near_duplicates(self):
        self._write("20260101-100000-aaaaaa.md", "What is RBAC?")
        self._write("20260101-100100-bbbbbb.md", "what is rbac?")  # near-dup
        self._write("20260101-100200-cccccc.md", "Why is the sky blue?")

        groups = self.scanner.scan(threshold=0.7)
        self.assertEqual(len(groups), 1)
        g = groups[0]
        # oldest kept
        self.assertEqual(g.kept.name, "20260101-100000-aaaaaa.md")
        self.assertEqual(len(g.removed), 1)
        self.assertEqual(g.removed[0].name, "20260101-100100-bbbbbb.md")

    def test_scan_returns_empty_for_unique(self):
        self._write("20260101-100000-aaaaaa.md", "What is RBAC?")
        self._write("20260101-100100-bbbbbb.md", "Why is the sky blue?")
        self._write("20260101-100200-cccccc.md", "Define CIA triad.")

        groups = self.scanner.scan(threshold=0.7)
        self.assertEqual(groups, [])

    def test_apply_deletes_victims(self):
        a = self._write("20260101-100000-aaaaaa.md", "What is RBAC?")
        b = self._write("20260101-100100-bbbbbb.md", "what is rbac?")
        self._write("20260101-100200-cccccc.md", "Why is the sky blue?")

        self.assertTrue(a.exists())
        self.assertTrue(b.exists())

        groups = self.scanner.scan(threshold=0.7)
        self.scanner.apply(groups)

        self.assertTrue(a.exists())  # kept (oldest)
        self.assertFalse(b.exists())  # removed

    def test_unreadable_files_are_skipped(self):
        # File that exists but has no ## English section.
        bad = self.qdir / "20260101-100000-zzzzzz.md"
        bad.write_text("# 2026\n\nNo content here.\n", encoding="utf-8")

        # File with a valid English question (so scan() sees > 0 items)
        self._write("20260101-100100-aaaaaa.md", "What is RBAC?")

        groups = self.scanner.scan(threshold=0.7)
        # no pairs to compare → no groups
        self.assertEqual(groups, [])

    def test_three_way_chain_merges_into_one_group(self):
        # A and B are near-dups, B and C are near-dups, A and C are
        # slightly less similar. With transitive closure, all three
        # should land in one group (two removed, one kept).
        self._write("20260101-100000-aaaaaa.md", "What is RBAC role-based access control?")
        self._write("20260101-100100-bbbbbb.md", "What is RBAC role-based access?")
        self._write("20260101-100200-cccccc.md", "What is RBAC role-based access control system?")

        groups = self.scanner.scan(threshold=0.7)
        # All three should be in one group
        self.assertEqual(len(groups), 1)
        g = groups[0]
        all_files = {g.kept.name} | {v.name for v in g.removed}
        self.assertEqual(len(all_files), 3)
        self.assertEqual(len(g.removed), 2)
        # kept = oldest by filename timestamp
        self.assertEqual(g.kept.name, "20260101-100000-aaaaaa.md")


if __name__ == "__main__":
    unittest.main()