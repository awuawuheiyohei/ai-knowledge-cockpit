"""
config.py — Tunable parameters for the knowledge base.

Centralized so behavior changes don't require code hunting.
"""
from __future__ import annotations

# --- Chunking ----------------------------------------------------------
# Target chunk size in characters. BM25 works well at 200-800 chars per chunk
# for most QA-style retrieval. Smaller = more precise recall, larger = more
# context per hit.
CHUNK_SIZE = 400
# Hard overlap between adjacent chunks. Helps when a sentence straddles a
# boundary — overlap means the same terms appear in two chunks.
CHUNK_OVERLAP = 60

# --- Scan-page detection ----------------------------------------------
# A page is considered "scanned / image-only" if the extracted text is
# shorter than this many characters AND the page reports no images of text.
# We use a small threshold to catch near-empty pages (typical of OCR-less
# image PDFs) without false-flagging genuine single-line pages.
SCAN_PAGE_MIN_CHARS = 30

# --- BM25 --------------------------------------------------------------
# Standard BM25 hyperparameters. k1 controls term-frequency saturation,
# b controls length normalization. Defaults from Robertson et al.
BM25_K1 = 1.5
BM25_B = 0.75

# --- Retrieval ---------------------------------------------------------
# Default number of hits returned by `search`.
DEFAULT_TOP_K = 5
# Max hits ever returned, to keep CLI output sane.
MAX_TOP_K = 50

# --- Tokenization ------------------------------------------------------
# Chinese: characters are not space-separated. We fall back to bigram
# tokenization (every adjacent character pair is one token). This is a
# zero-dependency, language-agnostic compromise that works reasonably well
# for both Chinese and English.
USE_BIGRAM_FOR_CJK = True

# --- Query rewriting --------------------------------------------------
# When the BM25 top-hit score is below this threshold, the im_router
# triggers an LLM-based query rewrite before re-searching. This rescues
# colloquial / vague queries ("用户能用什么密码登录") that BM25 alone
# handles poorly. Set to a large number to effectively disable rewriting,
# or to 0 to always rewrite.
REWRITE_SCORE_THRESHOLD = 2.0

# Don't even try to rewrite very short queries — short strings like "PKI"
# are usually already optimal for BM25 and rewriting risks noise.
REWRITE_MIN_QUERY_LEN = 4

# If a hit is found with score below this, it's reported as "weak" — the
# answer is still shown but the user is hinted that they can /expand.
WEAK_HINT_THRESHOLD = 1.0