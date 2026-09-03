"""
test_question_archive.py — unit tests for the practice-mode
question archive (no-answer flow).

Covers:
  - file path & folder layout (域N subdir)
  - dedup by normalized text (storage.archive_question)
  - markdown body format (English + 中文 sections)
  - "no domain" path (LLM classify failed) doesn't write a file
  - re-save returns the existing file path without writing a duplicate

Run: .venv/bin/python tests/test_question_archive.py
"""
from __future__ import annotations

import os
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
        # Also patch the module-level domain dict to ensure it lines up
        # with the one in im_router (it does today, but be defensive).
        self.qa = question_archive

    def tearDown(self):
        self.tmp.cleanup()

    def _call(self, en, dom, src="test"):
        return self.qa.save_question(en, dom, src)

    def test_basic_save(self):
        r = self._call("What is RBAC?", 5)
        self.assertTrue(r["is_new"])
        self.assertEqual(r["domain"], 5)
        self.assertEqual(r["domain_name"], "身份与访问管理")
        self.assertIsNotNone(r["path"])
        # file exists at the per-domain subdir
        assert r["path"] is not None
        self.assertTrue(str(r["path"]).endswith(".md"))
        self.assertIn("/域5/", str(r["path"]))

    def test_dedup_returns_existing(self):
        r1 = self._call("What is RBAC?", 5)
        r2 = self._call("What is RBAC?", 5)
        self.assertTrue(r1["is_new"])
        self.assertFalse(r2["is_new"])
        self.assertEqual(r1["path"], r2["path"])
        # only one file in the per-domain folder
        assert r1["path"] is not None
        files = list((self.questions_root / "域5").glob("*.md"))
        self.assertEqual(len(files), 1)

    def test_normalize_for_dedup(self):
        r1 = self._call("What is RBAC?", 5)
        r2 = self._call("  WHAT   is rbac?  ", 5)
        self.assertTrue(r1["is_new"])
        self.assertFalse(r2["is_new"])
        self.assertEqual(r1["path"], r2["path"])

    def test_no_domain_no_file(self):
        # simulate LLM classify failure: domain is None (we use -1 inside
        # the function as the "skip" sentinel; here we test the public
        # contract — invalid domain must not write a file)
        r = self._call("What is TLS?", 0)  # invalid
        self.assertFalse(r["is_new"])
        self.assertIsNone(r["path"])
        # nothing on disk
        self.assertFalse(any(self.questions_root.rglob("*.md")))

    def test_empty_text_no_file(self):
        r = self._call("", 5)
        self.assertFalse(r["is_new"])
        self.assertIsNone(r["path"])
        r = self._call("   ", 5)
        self.assertFalse(r["is_new"])
        self.assertIsNone(r["path"])

    def test_markdown_format(self):
        r = self._call("Sample question text here", 1)
        assert r["path"] is not None
        body = r["path"].read_text(encoding="utf-8")
        self.assertIn("# 域1 · 安全与风险管理", body)
        self.assertIn("## English", body)
        self.assertIn("## 中文", body)
        self.assertIn("Sample question text here", body)
        # The Chinese section should not be empty (LLM is configured in CI env)
        # but the marker should at least be there.
        self.assertIn("归档时间", body)

    def test_different_questions_different_files(self):
        r1 = self._call("What is CIA?", 1)
        r2 = self._call("What is RBAC?", 5)
        self.assertTrue(r1["is_new"])
        self.assertTrue(r2["is_new"])
        self.assertNotEqual(r1["path"], r2["path"])
        self.assertIn("/域1/", str(r1["path"]))
        self.assertIn("/域5/", str(r2["path"]))


if __name__ == "__main__":
    unittest.main()
