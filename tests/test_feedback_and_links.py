"""
test_feedback_and_links.py — Regression tests for Day 4 + Day 5.

Day 4: /good /bad /partial user feedback loop.
Day 5: clickable `file://` source links in formatted hits.

These touch the public surface of im_router (handle_message signature,
format helpers) and the feedback persistence layer.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Make project root importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import paths  # noqa: E402
import im_router  # noqa: E402
from im_router import (  # noqa: E402
    _source_link,
    _record_last_reply,
    _pop_last_reply,
    save_feedback,
    handle_message,
)


class TestSourceLink(unittest.TestCase):
    """Day 5: source citations should render as file:// links when possible."""

    def test_pdf_with_page_renders_clickable_link(self):
        # A hit with a real relative_path that exists in the project.
        hit = {
            "filename": "test.pdf", "page_num": 42, "score": 5.0,
            "relative_path": "README.md",  # any existing file works
        }
        out = _source_link(hit)
        # Should contain the clickable link syntax.
        self.assertIn("[test.pdf p.42](file://", out)
        # And the plain-text path fallback.
        self.assertIn("`", out)
        # Score always shown.
        self.assertIn("score=5.00", out)

    def test_markdown_source_no_page(self):
        hit = {
            "filename": "notes.md", "page_num": None, "score": 3.5,
            "relative_path": "README.md",
        }
        out = _source_link(hit)
        # No `p.N` in the link label for markdown sources.
        self.assertNotIn(" p.", out)
        self.assertIn("[notes.md](file://", out)

    def test_missing_relative_path_falls_back_gracefully(self):
        """Defensive: hit with no relative_path should still render something."""
        hit = {"filename": "orphan.pdf", "page_num": 7, "score": 2.0}
        out = _source_link(hit)
        # Falls back to plain `filename` + `p.N` + score — no link, no crash.
        self.assertIn("orphan.pdf", out)
        self.assertIn("p.7", out)
        self.assertIn("score=2.00", out)


class TestFeedbackLoop(unittest.TestCase):
    """Day 4: /good /bad /partial should save to data/feedback/*.jsonl."""

    def setUp(self):
        # Use a sandbox data dir so we don't pollute the real KB.
        self._tmpdir = tempfile.mkdtemp(prefix="kb_feedback_test_")
        self._orig_data = paths.DATA
        paths.DATA = Path(self._tmpdir) / "data"
        paths.DATA.mkdir(parents=True, exist_ok=True)
        # Clear the in-memory last_replies dict between tests.
        im_router._LAST_REPLIES.clear()

    def tearDown(self):
        paths.DATA = self._orig_data
        im_router._LAST_REPLIES.clear()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_record_and_pop_last_reply(self):
        _record_last_reply("test", "user1", "PKI 是什么?", "PKI 是...", "text")
        last = _pop_last_reply("test", "user1")
        self.assertIsNotNone(last)
        self.assertEqual(last["question"], "PKI 是什么?")
        self.assertEqual(last["reply"], "PKI 是...")
        # Pop again — should be gone (one-shot).
        self.assertIsNone(_pop_last_reply("test", "user1"))

    def test_record_skips_when_no_user_id(self):
        _record_last_reply("test", "", "q", "r", "text")
        self.assertEqual(im_router._LAST_REPLIES, {})

    def test_save_feedback_writes_jsonl(self):
        last = {"question": "q1", "reply": "r1", "message_type": "text"}
        ok, msg = save_feedback("test", "user1", "good", last)
        self.assertTrue(ok)
        self.assertIn("good", msg)
        # File exists and has one JSON line.
        feedback_dir = paths.DATA / "feedback"
        files = list(feedback_dir.glob("*.jsonl"))
        self.assertEqual(len(files), 1)
        lines = files[0].read_text(encoding="utf-8").strip().split("\n")
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["verdict"], "good")
        self.assertEqual(record["last_reply"]["question"], "q1")

    def test_save_feedback_rejects_invalid_verdict(self):
        ok, msg = save_feedback("test", "user1", "ok", {"question": "q", "reply": "r"})
        self.assertFalse(ok)
        self.assertIn("未知", msg)

    def test_save_feedback_requires_context(self):
        ok, msg = save_feedback("test", "user1", "bad", None)
        self.assertFalse(ok)
        self.assertIn("找不到", msg)

    def test_handle_message_dispatch_records(self):
        """handle_message should record the reply for /good /bad /partial
        to attach to."""
        # /help doesn't record (it's a slash command, not a real query).
        handle_message("test", "/help", user_id="u1")
        self.assertNotIn(("test", "u1"), im_router._LAST_REPLIES)

        # /status doesn't record either.
        handle_message("test", "/status", user_id="u1")
        self.assertNotIn(("test", "u1"), im_router._LAST_REPLIES)

    def test_full_feedback_cycle(self):
        """Simulate: user asks → bot answers → user sends /good → feedback saved."""
        # Step 1: User asks a question; bot records the reply.
        # We can't easily call handle_message end-to-end (needs KB), so
        # simulate by manually recording.
        _record_last_reply(
            "test", "u1",
            question="PKI 是什么?",
            reply="PKI 是公钥基础设施...",
            message_type="text",
        )
        # Step 2: User sends /good.
        last = _pop_last_reply("test", "u1")
        self.assertIsNotNone(last)
        ok, _ = save_feedback("test", "u1", "good", last)
        self.assertTrue(ok)
        # Step 3: feedback file exists.
        files = list((paths.DATA / "feedback").glob("*.jsonl"))
        self.assertEqual(len(files), 1)


class TestHandleMessageSignature(unittest.TestCase):
    """Make sure the new user_id parameter doesn't break the old call sites."""

    def test_handle_message_works_without_user_id(self):
        """Backwards-compat: handle_message('wecom', text) still works."""
        # /help should always work, no KB needed.
        out = handle_message("wecom", "/help")
        self.assertIn("AI Knowledge Cockpit", out)

    def test_handle_message_with_user_id(self):
        out = handle_message("dingtalk", "/help", user_id="ding-123")
        self.assertIn("AI Knowledge Cockpit", out)

    def test_good_command_without_recent_reply_warns(self):
        """Sending /good before any query should produce a clear warning."""
        im_router._LAST_REPLIES.clear()
        out = handle_message("test", "/good", user_id="u1")
        self.assertIn("找不到", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
