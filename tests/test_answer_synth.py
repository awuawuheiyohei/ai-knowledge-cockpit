"""
test_answer_synth.py — Regression tests for answer_synth._is_valid_synthesis.

These exercise the citation-grounding + chunk-on-topic checks added
2026-08-06 to defend against "confident bullshit" — the failure mode
where the LLM cites a real-but-irrelevant chunk and writes a plausible
but fabricated answer on top of it.

Run with:
    .venv/bin/python -m pytest tests/  -v
or simply:
    .venv/bin/python tests/test_answer_synth.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Make the project root importable when running this file directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from answer_synth import (  # noqa: E402
    _answer_groundedness,
    _is_valid_synthesis,
    _strip_citations,
)


PKI_CHUNK = (
    "PKI 是 Public Key Infrastructure 公钥基础设施的缩写,"
    "提供数字证书签发和验证服务,广泛用于 HTTPS 和邮件加密。"
)
PKI_HIT = {
    "filename": "test.pdf", "page_num": 35, "score": 5.0,
    "chunk_text": PKI_CHUNK,
}

KITCHEN_CHUNK = (
    "厨房里有番茄、黄瓜、洋葱和辣椒,炒菜时应该先放油。"
)
KITCHEN_HIT = {
    "filename": "cookbook.pdf", "page_num": 20, "score": 4.5,
    "chunk_text": KITCHEN_CHUNK,
}


class TestCitationGrounding(unittest.TestCase):
    """
    Each case below was a real failure mode surfaced in Day-1
    accuracy hardening (2026-08-06). Comments link the test to the
    failure pattern it defends against.
    """

    def test_legit_grounded_answer_passes(self):
        """LLM answer is derived from the cited chunk + chunk is on-topic."""
        text = "PKI 是公钥基础设施,用于数字证书签发和验证。[来源: test.pdf, p.35]"
        self.assertTrue(_is_valid_synthesis(text, [PKI_HIT], question="PKI 是什么?"))

    def test_llm_fabrication_rejected(self):
        """LLM cites a real chunk but writes a fabricated answer not from it.
        This is the original 'confident bullshit' failure mode — the LLM
        confidently invents a 'blockchain-based PKI' answer while citing
        the legitimate PKI definition page. answer-groundedness check
        catches it: none of the fabricated tokens appear in the chunk.
        """
        text = (
            "PKI 是一种基于区块链的新型加密货币,使用零知识证明技术。"
            "[来源: test.pdf, p.35]"
        )
        self.assertFalse(_is_valid_synthesis(text, [PKI_HIT], question="PKI 是什么?"))

    def test_standard_empty_answer_bypass(self):
        """The '未在资料中检索到' line is a valid answer (no LLM was used)."""
        text = "未在资料中检索到相关内容。"
        self.assertTrue(_is_valid_synthesis(text, [PKI_HIT], question="whatever"))

    def test_fabricated_citation_rejected(self):
        """LLM cites a chunk that's NOT in the retrieved hits.
        Caught by the existence check — strict (filename, page) match.
        """
        text = "PKI 是公钥基础设施。[来源: hacker.pdf, p.99]"
        self.assertFalse(_is_valid_synthesis(text, [PKI_HIT], question="PKI 是什么?"))

    def test_mixed_citations_one_grounded_enough(self):
        """LLM cites two chunks: one real-on-topic, one off-topic real.
        Should PASS because the on-topic one is grounded in the answer.
        The off-topic citation is just noise; the user can see it
        alongside the answer.
        """
        text = (
            "PKI 是公钥基础设施,广泛用于 HTTPS 和邮件加密。"
            "[来源: test.pdf, p.35] [来源: cookbook.pdf, p.20]"
        )
        ok = _is_valid_synthesis(
            text, [PKI_HIT, KITCHEN_HIT], question="PKI 是什么?",
        )
        self.assertTrue(ok)

    def test_only_off_topic_real_chunk_cited(self):
        """LLM faithfully paraphrases an off-topic real chunk.
        The 'answer-groundedness' check passes (LLM really did copy the
        chunk) but the 'chunk-on-topic' secondary check rejects it
        because the chunk shares zero tokens with the question.
        This is the 'faithful but useless' failure mode.
        """
        text = "厨房里有番茄和洋葱,炒菜要放油。[来源: cookbook.pdf, p.20]"
        ok = _is_valid_synthesis(
            text, [KITCHEN_HIT], question="PKI 是什么?",
        )
        self.assertFalse(ok)

    def test_short_answer_on_topic_passes(self):
        """Short answer that is on-topic and grounded should pass."""
        hits = [{
            "filename": "a.pdf", "page_num": 1, "score": 5.0,
            "chunk_text": "PKI 是公钥基础设施,提供加密签名身份认证服务。",
        }]
        text = "PKI 公钥基础设施 [来源: a.pdf, p.1]"
        self.assertTrue(_is_valid_synthesis(text, hits, question="PKI 是什么?"))

    def test_no_question_param_relies_on_groundedness(self):
        """If caller passes no question, only groundedness is checked.
        (Defensive — we shouldn't reject legitimate synth if the caller
        forgot to pass the question, since 'no question' ≠ 'off-topic'.)
        """
        text = "PKI 是公钥基础设施。[来源: test.pdf, p.35]"
        self.assertTrue(_is_valid_synthesis(text, [PKI_HIT], question=""))

    def test_legit_answer_for_legit_offtopic_question(self):
        """If the question is genuinely about cooking, kitchen chunks are on-topic."""
        text = "厨房里有番茄和洋葱,炒菜要放油。[来源: cookbook.pdf, p.20]"
        ok = _is_valid_synthesis(
            text, [KITCHEN_HIT], question="做菜要放什么?",
        )
        self.assertTrue(ok)

    def test_strict_page_mismatch_rejected(self):
        """LLM cites p.10 but the only matching hit is p.1.
        Caught by strict (filename, page) match. This is intentional —
        for '人命关天' accuracy, we'd rather reject and fall back than
        accept a page-mismatched citation.
        """
        hits = [{
            "filename": "a.pdf", "page_num": 1, "score": 5.0,
            "chunk_text": "PKI 是公钥基础设施,提供加密签名身份认证服务。",
        }]
        text = "PKI 是公钥基础设施。[来源: a.pdf, p.10]"
        self.assertFalse(_is_valid_synthesis(text, hits, question="PKI 是什么?"))


class TestStripCitations(unittest.TestCase):
    def test_strip_single(self):
        self.assertEqual(
            _strip_citations("PKI 是公钥基础设施。[来源: a.pdf, p.10]"),
            "PKI 是公钥基础设施。",
        )

    def test_strip_multiple(self):
        self.assertEqual(
            _strip_citations(
                "PKI 是公钥基础设施。[来源: a.pdf, p.10] 又见 [来源: b.pdf, p.20]"
            ),
            "PKI 是公钥基础设施。 又见",
        )

    def test_strip_empty(self):
        self.assertEqual(_strip_citations(""), "")


class TestConfigValues(unittest.TestCase):
    """Sanity-check the new Day-1 config knobs exist and have sensible values."""

    def test_groundedness_threshold(self):
        self.assertTrue(0.0 < config.SYNTH_ANSWER_GROUNDEDNESS_MIN <= 1.0)
        # Default 0.20; if you tune this, update the test.
        self.assertEqual(config.SYNTH_ANSWER_GROUNDEDNESS_MIN, 0.20)

    def test_groundedness_absolute_floor(self):
        self.assertGreaterEqual(config.SYNTH_ANSWER_GROUNDEDNESS_MIN_INTER, 2)

    def test_rewrite_threshold(self):
        # Day 1 bumped 2.0 → 4.0 to catch medium-strength weak hits.
        self.assertEqual(config.REWRITE_SCORE_THRESHOLD, 4.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
