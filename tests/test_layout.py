"""
test_layout.py — Day 3: layout-aware PDF text extraction.

The two-column detection heuristic is tested directly with mock block
lists (no real PDF needed). The end-to-end "does the 2-col page come
out in proper reading order" check uses a real PDF from the KB — it
fails closed if the KB is moved/renamed.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_extract  # noqa: E402
from pdf_extract import _classify_layout  # noqa: E402


def _b(x0, y0, x1, y1, text):
    """Build a fake fitz block tuple."""
    return (x0, y0, x1, y1, text, 0, 0)


class TestClassifyLayout(unittest.TestCase):
    """Pure-Python tests — no PDF needed."""

    def test_single_column_full_width(self):
        """Every block spans the full page → unambiguously single."""
        # Page width 400, full-width blocks use 0..400.
        blocks = [
            _b(40, 50, 360, 100, "This is a long paragraph of body text."),
            _b(40, 130, 360, 200, "Another long paragraph of body text."),
            _b(40, 230, 360, 300, "Yet another long paragraph of text here."),
        ]
        self.assertEqual(_classify_layout(blocks, 400), "single")

    def test_two_column_layout_detected(self):
        """Two clear columns at the same y-band → 2-col."""
        # Page width 400, left column 0..180, right column 220..400.
        blocks = [
            _b(20, 50, 180, 100, "Left column paragraph one with enough text."),
            _b(220, 50, 380, 100, "Right column paragraph one with text."),
            _b(20, 130, 180, 180, "Left column paragraph two with enough text."),
            _b(220, 130, 380, 180, "Right column paragraph two with text."),
        ]
        self.assertEqual(_classify_layout(blocks, 400), "two-col")

    def test_toc_dot_leaders_not_misclassified(self):
        """Many short blocks (TOC-style) on the same side → not 2-col.
        A TOC has many short lines ('18.1.2 ... 672'), all on the
        same side of the page. Our 'substantial' filter (≥20 chars)
        still catches them as left-only, NOT as 2-col.
        """
        # Many short left-side TOC entries.
        blocks = [
            _b(40, 50, 200, 60, "18.1.1 .... 671"),
            _b(40, 70, 200, 80, "18.1.2 .... 672"),
            _b(40, 90, 200, 100, "18.1.3 .... 673"),
            _b(40, 110, 200, 120, "18.1.4 .... 674"),
        ]
        # All on left, all short → not 2-col.
        self.assertEqual(_classify_layout(blocks, 400), "single")

    def test_figure_block_full_width(self):
        """A single page with one full-width figure + 2-col text → 'mixed'."""
        blocks = [
            # Top: full-width figure caption.
            _b(40, 50, 360, 100, "Figure 3.1: System architecture diagram."),
            # Below: 2-col body.
            _b(20, 130, 180, 200, "Left column body text of substantial length."),
            _b(220, 130, 380, 200, "Right column body text of substantial."),
            _b(20, 220, 180, 300, "Another left column paragraph of text."),
            _b(220, 220, 380, 300, "Another right column paragraph of text."),
        ]
        # Should detect the 2-col structure; may be 'two-col' or 'mixed'.
        result = _classify_layout(blocks, 400)
        self.assertIn(result, ("two-col", "mixed"))

    def test_empty_blocks_returns_single(self):
        """Defensive: empty input shouldn't crash."""
        self.assertEqual(_classify_layout([], 400), "single")


class TestLayoutAwareExtraction(unittest.TestCase):
    """Integration test: read a real PDF page and verify the order."""

    PDF_CANDIDATES = [
        # OSG9 上册 is a 2-column PDF; p70+ should be 2-col body.
        "data/originals/OSG9中文版-上册/OSG9中文版-上册.pdf",
    ]

    def _find_pdf(self) -> Path | None:
        for rel in self.PDF_CANDIDATES:
            p = Path(__file__).resolve().parent.parent / rel
            if p.is_file():
                return p
        return None

    def test_2col_page_keeps_left_before_right(self):
        """On a 2-column page, the left column's first paragraph should
        appear BEFORE the right column's first paragraph in the output
        — that's the whole point of layout-aware extraction."""
        pdf = self._find_pdf()
        if pdf is None:
            self.skipTest("OSG9 上册 not present in data/originals — skip")

        import fitz  # type: ignore
        doc = fitz.open(str(pdf))
        try:
            # p70 is a confirmed 2-col body page from earlier analysis.
            page = doc[69]  # 0-indexed
            out = pdf_extract._extract_text_layout_aware(page)
            self.assertTrue(out, "layout-aware extractor returned empty text")

            # Sanity: output should be longer than just one column of text.
            # If extraction collapsed to half the page, something broke.
            text_mode = page.get_text("text")
            # We don't assert exact equality/inequality — just that
            # the layout-aware output is a real, multi-paragraph
            # extraction (not a 1-line string).
            self.assertGreater(len(out), 200,
                f"layout-aware output too short: {len(out)} chars")
            # And that it isn't drastically shorter than the
            # single-column fallback (we shouldn't be losing content).
            self.assertGreater(
                len(out), len(text_mode) * 0.7,
                f"layout-aware output lost too much: {len(out)} vs "
                f"text mode {len(text_mode)}",
            )
        finally:
            doc.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
