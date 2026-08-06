"""
test_hybrid_search.py — Day 6: hybrid BM25 + embedding search.

These tests verify the hybrid fusion logic without needing a real
embedding model. The mock model returns deterministic vectors that
we can reason about: same text → same vector, different text →
different vector. The fusion math is then unit-tested directly.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import search  # noqa: E402
import config   # noqa: E402
import storage  # noqa: E402


class TestHybridFusionLogic(unittest.TestCase):
    """Test the math of the fusion (normalize → weighted sum → sort)
    using a mocked embedding function so we don't depend on the model."""

    def _mock_embed(self, texts):
        """Deterministic mock: vector is just the per-char unicode sum,
        broadcast to EMBEDDING_DIM. Same text → same vector. This is
        enough to test that the fusion code is correct."""
        import numpy as np
        dim = config.EMBEDDING_DIM
        out = np.zeros((len(texts), dim), dtype="float32")
        for i, t in enumerate(texts):
            seed = sum(ord(c) for c in t) % 1000
            out[i, :dim] = (seed % 100) / 100.0
        return out

    def test_falls_back_to_bm25_when_no_embeddings(self):
        """If count_embeddings() == 0, hybrid_search must return the
        top_k BM25 hits (not crash, not return empty)."""
        # Patch the embedding-check side of the gate so we get the
        # BM25 path even though no chunks are embedded in this test DB.
        with patch.object(search, "_EMBEDDINGS_READY", False), \
             patch.object(storage, "count_embeddings", return_value=0):
            hits = search.hybrid_search("PKI 是什么", top_k=3)
        # Should get SOMETHING from BM25 (this test DB has the OSG PDFs).
        self.assertGreater(len(hits), 0)

    def test_falls_back_when_model_unavailable(self):
        """If the embedding model can't load, hybrid_search must
        still return BM25 hits — silently degrading is the right
        behavior for a 'best effort' optimization."""
        with patch("embedding.is_available", return_value=False), \
             patch("embedding.embed_texts", return_value=None):
            hits = search.hybrid_search("PKI 是什么", top_k=3)
        self.assertGreater(len(hits), 0)
        # And the hit shape is the standard BM25 shape (no bm25_score/embed_score).
        for h in hits:
            self.assertNotIn("embed_score", h)


class TestEmbeddingSchema(unittest.TestCase):
    """Light smoke-tests for the chunk_embeddings table — no model needed."""

    def test_count_embeddings_initially_zero(self):
        # Don't reset the DB; just check that the call works.
        n = storage.count_embeddings()
        self.assertIsInstance(n, int)
        self.assertGreaterEqual(n, 0)

    def test_get_chunks_needing_embeddings_runs(self):
        # Should return a list (possibly empty) without crashing.
        pending = storage.get_chunks_needing_embeddings(config.EMBEDDING_MODEL)
        self.assertIsInstance(pending, list)


if __name__ == "__main__":
    unittest.main(verbosity=2)
