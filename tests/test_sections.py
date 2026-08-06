"""
test_sections.py — Day 2: section-aware chunking helpers.

These cover the new sections.py module and the ingest-side glue that
prefixes section headings into chunks.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sections import (  # noqa: E402
    Section,
    parse_markdown_sections,
    prefix_chunk,
)


class TestParseMarkdownSections(unittest.TestCase):

    def test_no_headers_returns_one_anonymous(self):
        md = "Just some text.\n\nMore text, no headers anywhere."
        out = parse_markdown_sections(md)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].heading, "")
        self.assertEqual(out[0].text, md)

    def test_empty_input_returns_empty_list(self):
        self.assertEqual(parse_markdown_sections(""), [])
        self.assertEqual(parse_markdown_sections("   \n  "), [])

    def test_single_header_with_body(self):
        md = "# Title\n\nBody text under title."
        out = parse_markdown_sections(md)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].heading, "Title")
        self.assertEqual(out[0].level, 1)
        self.assertEqual(out[0].text, "Body text under title.")

    def test_multiple_headers_split_correctly(self):
        md = (
            "# H1\n\nBody 1.\n\n"
            "## H2a\n\nBody 2a.\n\n"
            "## H2b\n\nBody 2b.\n\n"
            "# H1 again\n\nBody 3."
        )
        out = parse_markdown_sections(md)
        # Expected: 4 sections, no preamble.
        self.assertEqual(len(out), 4)
        self.assertEqual([s.heading for s in out], ["H1", "H2a", "H2b", "H1 again"])
        self.assertEqual([s.level for s in out], [1, 2, 2, 1])
        self.assertEqual(out[0].text, "Body 1.")
        self.assertEqual(out[1].text, "Body 2a.")
        self.assertEqual(out[2].text, "Body 2b.")
        self.assertEqual(out[3].text, "Body 3.")

    def test_preamble_before_first_header(self):
        md = "Some intro text.\n\n# Header\n\nBody under header."
        out = parse_markdown_sections(md)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0].heading, "")  # preamble
        self.assertEqual(out[0].text, "Some intro text.")
        self.assertEqual(out[1].heading, "Header")
        self.assertEqual(out[1].text, "Body under header.")

    def test_header_inside_paragraph_not_matched(self):
        """A '#' inside a paragraph (not at line start) should NOT be
        treated as a header — but the preamble (text before the first
        real header) IS a separate anonymous section."""
        md = "Text with # in middle.\n\n# Real header\n\nBody."
        out = parse_markdown_sections(md)
        # Two sections: preamble (heading="") + "Real header".
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0].heading, "")
        self.assertEqual(out[0].text, "Text with # in middle.")
        self.assertEqual(out[1].heading, "Real header")
        self.assertEqual(out[1].text, "Body.")


class TestPrefixChunk(unittest.TestCase):

    def test_empty_heading_returns_unchanged(self):
        self.assertEqual(prefix_chunk("body", ""), "body")

    def test_heading_prefixes_with_label(self):
        out = prefix_chunk("body text", "第二章 安全架构")
        self.assertEqual(out, "章节: 第二章 安全架构\n\nbody text")

    def test_heading_with_extra_whitespace_normalized(self):
        out = prefix_chunk("body", "  Multi   spaced  ")
        # Multiple internal whitespace gets squashed to single space.
        self.assertEqual(out, "章节: Multi spaced\n\nbody")


class TestSectionDataclass(unittest.TestCase):
    def test_page_num_is_none_for_markdown(self):
        s = Section(heading="X", level=1, text="t", page_num=None)
        self.assertIsNone(s.page_num)


if __name__ == "__main__":
    unittest.main(verbosity=2)
