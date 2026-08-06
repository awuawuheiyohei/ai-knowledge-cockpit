"""
embedding.py — Local sentence-transformer embeddings for hybrid search.

Day 6 (2026-08-06): BM25 alone is word-form exact — a query in casual
Chinese like "用户能用什么密码登录" can't find a chunk that says "身份
验证机制采用基于口令的认证". Adding a semantic embedding layer (mixed
into the final ranking alongside BM25) closes that gap.

Hard rules
----------
- We do NOT call any external API for embeddings. The model is loaded
  once and run on-device (Apple MPS / CUDA / CPU, whatever's available).
- The default model is `paraphrase-multilingual-MiniLM-L12-v2` — a
  ~470MB download, runs at ~500 sentences/sec on Apple M-series chips,
  supports 50+ languages including Chinese. Good enough for our scale
  (20K chunks); we don't need a heavier model.
- BM25 is still the primary signal; embeddings are a *complement*,
  not a replacement. The hybrid weight is configurable in config.py.

First-run cost
--------------
- Model download: ~470MB (cached in ./.cache/embeddings/ and
  ~/.cache/huggingface/).
- Embedding 20K chunks: ~30-60s on M-series CPU; ~5-10s on MPS.

To rebuild embeddings after a code change or chunking tweak:
    python app.py rebuild --with-embeddings
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import config


logger = logging.getLogger("embedding")

# Lazy singleton — model is heavy, load once.
_MODEL: object | None = None
_MODEL_LOAD_ERROR: str | None = None


# We use Apple Metal (MPS) when available, fall back to CPU.
# CUDA is supported if torch sees it but the user would have to
# set it up themselves — most of our users are on MacBooks.
def _select_device() -> str:
    try:
        import torch  # type: ignore
        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except ImportError:
        pass
    return "cpu"


def get_model():
    """
    Return the cached SentenceTransformer model, loading it on first call.

    Returns None if the model can't be loaded (network down, install
    broken, etc.) — the caller should fall back to BM25-only search.
    """
    global _MODEL, _MODEL_LOAD_ERROR
    if _MODEL is not None:
        return _MODEL
    if _MODEL_LOAD_ERROR is not None:
        # Already failed once this process — don't try again per query.
        return None

    try:
        # Lazy import so the rest of the app stays importable even if
        # torch / sentence-transformers is missing or broken.
        from sentence_transformers import SentenceTransformer  # type: ignore
    except ImportError as e:
        _MODEL_LOAD_ERROR = f"sentence-transformers not installed: {e}"
        logger.warning("embedding: %s", _MODEL_LOAD_ERROR)
        return None

    cache_dir = str(Path(__file__).resolve().parent / ".cache" / "embeddings")
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    device = _select_device()
    try:
        logger.info("embedding: loading %s (device=%s, cache=%s)…",
                    config.EMBEDDING_MODEL, device, cache_dir)
        m = SentenceTransformer(
            config.EMBEDDING_MODEL,
            cache_folder=cache_dir,
            device=device,
        )
        dim = m.get_sentence_embedding_dimension()
        logger.info("embedding: loaded, dim=%d", dim)
        if dim != config.EMBEDDING_DIM:
            logger.warning(
                "embedding: model dim=%d != config.EMBEDDING_DIM=%d; "
                "the hybrid index may need rebuild",
                dim, config.EMBEDDING_DIM,
            )
        _MODEL = m
        return m
    except Exception as e:
        _MODEL_LOAD_ERROR = f"failed to load {config.EMBEDDING_MODEL}: {e}"
        logger.warning("embedding: %s", _MODEL_LOAD_ERROR)
        return None


def is_available() -> bool:
    """True iff the embedding model is loaded successfully (or just
    didn't fail yet — call get_model() to force a load attempt)."""
    if _MODEL is not None:
        return True
    if _MODEL_LOAD_ERROR is not None:
        return False
    # Try to load lazily.
    return get_model() is not None


def last_load_error() -> str | None:
    """Return the reason the model failed to load, if any. Useful for
    surfacing in /status so the user knows why hybrid search is off."""
    return _MODEL_LOAD_ERROR


def embed_texts(texts: list[str], batch_size: int = 64) -> "object | None":
    """
    Embed a list of strings. Returns an np.ndarray of shape
    (len(texts), EMBEDDING_DIM), or None if the model isn't available.

    `batch_size=64` is a reasonable default; bigger uses more memory.
    """
    m = get_model()
    if m is None:
        return None
    if not texts:
        import numpy as np
        return np.zeros((0, config.EMBEDDING_DIM), dtype="float32")
    # `convert_to_numpy=True` is the default in modern versions but
    # being explicit guards against future API changes.
    vectors = m.encode(
        texts,
        batch_size=batch_size,
        show_progress_bar=False,
        convert_to_numpy=True,
        normalize_embeddings=True,  # so cosine = dot product
    )
    return vectors.astype("float32")
