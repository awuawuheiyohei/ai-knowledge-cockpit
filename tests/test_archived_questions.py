"""
test_archived_questions.py — unit tests for the archived_questions table
+ storage.CISSP_DOMAIN_NAMES + the dedup/normalize logic.

Run with: .venv/bin/python -m pytest tests/test_archived_questions.py -v
or:       .venv/bin/python tests/test_archived_questions.py
"""
from __future__ import annotations

import os
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
        rid1 = storage.archive_question("What is CIA?", 1, "test:user")
        self.assertIsNotNone(rid1)
        rid2 = storage.archive_question("What is CIA?", 1, "test:user")
        self.assertIsNone(rid2)  # dedup hit
        self.assertEqual(storage.count_archived_questions(), 1)

    def test_normalize_collapse_whitespace_and_case(self):
        import storage
        rid1 = storage.archive_question("What is CIA?", 1, "test:user")
        self.assertIsNotNone(rid1)
        # different case + extra whitespace + trailing space should dedup
        rid2 = storage.archive_question("  WHAT  is   cia?  ", 1, "test:user")
        self.assertIsNone(rid2)
        self.assertEqual(storage.count_archived_questions(), 1)
        rows = storage.list_archived_questions()
        self.assertEqual(rows[0]["question_text"], "what is cia?")

    def test_domain_filter(self):
        import storage
        storage.archive_question("Q1", 1, "src")
        storage.archive_question("Q2", 4, "src")
        storage.archive_question("Q3", 4, "src")
        self.assertEqual(storage.count_archived_questions(), 3)
        self.assertEqual(storage.count_archived_questions(domain=4), 2)
        self.assertEqual(storage.count_archived_questions(domain=8), 0)
        rows = storage.list_archived_questions(domain=4)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["domain"] == 4 for r in rows))

    def test_domain_validation(self):
        import storage
        with self.assertRaises(ValueError):
            storage.archive_question("Q", 0, "src")
        with self.assertRaises(ValueError):
            storage.archive_question("Q", 9, "src")

    def test_empty_text_is_noop(self):
        import storage
        self.assertIsNone(storage.archive_question("", 1, "src"))
        self.assertIsNone(storage.archive_question("   ", 1, "src"))
        self.assertEqual(storage.count_archived_questions(), 0)


if __name__ == "__main__":
    unittest.main()
