"""
test_question_archive.py — unit tests for the practice-mode
question archive (no-answer flow, single flat dir).

Covers (post 2026-09-07 simplification — no per-domain subdirs):
  - file path lives directly under QUESTIONS_DIR (no 域N subdir)
  - dedup by normalized text
  - markdown body format (English + 中文 sections)
  - empty text / no domain doesn't write a file
  - re-save returns the existing file path without writing a duplicate

Run: .venv/bin/python tests/test_question_archive.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TestQuestionArchive(unittest.TestCase):
    """Use a temp DB + temp questions/ dir so we never touch the real
    data/questions/ folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # Patch the storage DB path.
        import storage
        storage.DB_PATH = Path(self.tmp.name) / "kb.sqlite"  # type: ignore[attr-defined]
        storage.init_db()
        # Redirect the question_archive output dir to the temp dir.
        import question_archive
        self.questions_root = Path(self.tmp.name) / "questions"
        question_archive.QUESTIONS_DIR = self.questions_root  # type: ignore[attr-defined]
        self.qa = question_archive

    def tearDown(self):
        self.tmp.cleanup()

    def _call(self, en, src="test"):
        return self.qa.save_question(en, src)

    def test_basic_save_to_flat_dir(self):
        r = self._call("What is RBAC?")
        self.assertTrue(r["is_new"])
        self.assertIsNotNone(r["path"])
        assert r["path"] is not None
        self.assertTrue(str(r["path"]).endswith(".md"))
        # The file lives directly under QUESTIONS_DIR — no 域N subdir
        self.assertEqual(r["path"].parent, self.questions_root)
        # The path is just <questions>/<ts>-<hash>.md
        self.assertNotIn("域", str(r["path"]))
        # zh_text is returned for inline display
        self.assertIsInstance(r["zh_text"], str)

    def test_dedup_returns_existing(self):
        r1 = self._call("What is RBAC?")
        r2 = self._call("What is RBAC?")
        self.assertTrue(r1["is_new"])
        self.assertFalse(r2["is_new"])
        self.assertEqual(r1["path"], r2["path"])
        files = list(self.questions_root.glob("*.md"))
        self.assertEqual(len(files), 1)

    def test_normalize_for_dedup(self):
        r1 = self._call("What is RBAC?")
        r2 = self._call("  WHAT   is rbac?  ")
        self.assertTrue(r1["is_new"])
        self.assertFalse(r2["is_new"])
        self.assertEqual(r1["path"], r2["path"])

    def test_empty_text_no_file(self):
        r = self._call("")
        self.assertFalse(r["is_new"])
        self.assertIsNone(r["path"])
        r = self._call("   ")
        self.assertFalse(r["is_new"])
        self.assertIsNone(r["path"])

    def test_markdown_format(self):
        r = self._call("Sample question text here")
        assert r["path"] is not None
        body = r["path"].read_text(encoding="utf-8")
        self.assertIn("## English", body)
        self.assertIn("## 中文", body)
        self.assertIn("Sample question text here", body)
        self.assertIn("来源", body)

    def test_different_questions_different_files(self):
        r1 = self._call("What is CIA?")
        r2 = self._call("What is RBAC?")
        self.assertTrue(r1["is_new"])
        self.assertTrue(r2["is_new"])
        self.assertNotEqual(r1["path"], r2["path"])
        # both in the same flat dir
        self.assertEqual(r1["path"].parent, r2["path"].parent)


if __name__ == "__main__":
    unittest.main()
