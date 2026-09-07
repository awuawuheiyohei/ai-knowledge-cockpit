"""
test_archived_questions.py — unit tests for the archived_questions table
+ storage.CISSP_DOMAIN_NAMES + the dedup/normalize logic.

Domain classification was dropped (2026-09-07), so we no longer
require domain to be 1..8 — we still pass it for backwards
compatibility with the existing schema.

Run with: .venv/bin/python -m pytest tests/test_archived_questions.py -v
or:       .venv/bin/python tests/test_archived_questions.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

# Make the project root importable when running this file directly.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TestArchivedQuestions(unittest.TestCase):
    """Use a temp DB so we don't touch the real kb.sqlite."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "kb.sqlite"
        # Patch the module-level DB_PATH before storage imports anything.
        import storage
        storage.DB_PATH = self.db_path  # type: ignore[attr-defined]
        storage.init_db()

    def tearDown(self):
        self.tmp.cleanup()

    def test_domain_names_complete(self):
        """All 8 CISSP domains must be defined and have non-empty names."""
        import storage
        self.assertEqual(len(storage.CISSP_DOMAIN_NAMES), 8)
        self.assertEqual(set(storage.CISSP_DOMAIN_NAMES.keys()), set(range(1, 9)))
        for n, name in storage.CISSP_DOMAIN_NAMES.items():
            self.assertTrue(name.strip(), f"domain {n} name empty")

    def test_insert_and_dedup(self):
        import storage
        rid1 = storage.archive_question("What is CIA?", 0, "test:user")
        self.assertIsNotNone(rid1)
        rid2 = storage.archive_question("What is CIA?", 0, "test:user")
        self.assertIsNone(rid2)  # dedup hit
        self.assertEqual(storage.count_archived_questions(), 1)

    def test_normalize_collapse_whitespace_and_case(self):
        import storage
        rid1 = storage.archive_question("What is CIA?", 0, "test:user")
        self.assertIsNotNone(rid1)
        # different case + extra whitespace + trailing space should dedup
        rid2 = storage.archive_question("  WHAT  is   cia?  ", 0, "test:user")
        self.assertIsNone(rid2)
        self.assertEqual(storage.count_archived_questions(), 1)
        rows = storage.list_archived_questions()
        self.assertEqual(rows[0]["question_text"], "what is cia?")

    def test_no_domain_filter(self):
        # After 2026-09-07, we don't filter by domain anymore
        # (all questions go into a single flat dir).
        import storage
        storage.archive_question("Q1", 0, "src")
        storage.archive_question("Q2", 0, "src")
        storage.archive_question("Q3", 0, "src")
        self.assertEqual(storage.count_archived_questions(), 3)
        rows = storage.list_archived_questions()
        self.assertEqual(len(rows), 3)

    def test_legacy_domain_param_still_accepted(self):
        # archive_question still accepts the int (for backwards compat)
        # but no longer validates 1..8 — anything is fine.
        import storage
        rid1 = storage.archive_question("Q with domain=5", 5, "src")
        self.assertIsNotNone(rid1)
        rid2 = storage.archive_question("Q with domain=0", 0, "src")
        self.assertIsNotNone(rid2)
        rid3 = storage.archive_question("Q with domain=99", 99, "src")
        self.assertIsNotNone(rid3)
        self.assertEqual(storage.count_archived_questions(), 3)

    def test_empty_text_is_noop(self):
        import storage
        self.assertIsNone(storage.archive_question("", 0, "src"))
        self.assertIsNone(storage.archive_question("   ", 0, "src"))
        self.assertEqual(storage.count_archived_questions(), 0)


if __name__ == "__main__":
    unittest.main()
