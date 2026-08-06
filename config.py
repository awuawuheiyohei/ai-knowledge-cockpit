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
#
# 2026-08-06: bumped 2.0 → 4.0. With 2.0, only very weak hits triggered
# rewrite; medium-strength hits (3-5) that were actually irrelevant would
# slip through and get parrot'd by answer_synth as "confident bullshit".
# 4.0 catches the "BM25 thinks it found something but it's off-topic"
# zone more aggressively. The cost is one extra LLM call per "maybe"
# query — worth it given the user-facing "人命关天" accuracy rule.
REWRITE_SCORE_THRESHOLD = 4.0

# --- Embeddings + hybrid search (Day 6) -------------------------------
# We complement BM25 with a local sentence-transformer embedding for
# "what does this *mean*" recall that BM25's word-form matching can't
# catch (e.g. colloquial "用户能用什么密码登录" vs academic "身份验证
# 机制采用基于口令的认证"). Hybrid is BM25 + embedding cosine.
#
# The default model is a multilingual MiniLM (~470MB, supports Chinese).
# Override with EMBEDDING_MODEL=.../path if you have a local copy.
EMBEDDING_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"
EMBEDDING_DIM = 384  # must match the chosen model's output dim

# Weights for combining BM25 + embedding scores in hybrid search.
# Tuned on 2026-08-06 with tests/run_qa.py — the heavier 0.85/0.15
# split is what kept the ground-truth test set at 9/9. 0.6/0.4 made
# the embedding layer promote semantically-similar-but-irrelevant
# chunks to top-1 (e.g. a chunk mentioning PKI in passing beat the
# actual PKI definition because the embedding was closer to the
# colloquial query form). For "人命关天" accuracy, BM25 should
# dominate; the embedding layer is a tiebreaker, not a co-pilot.
HYBRID_BM25_WEIGHT = 0.85
HYBRID_EMBED_WEIGHT = 0.15

# If True, the IM router uses hybrid search when embeddings are ready
# (count_embeddings() == n_chunks), and falls back to pure BM25
# otherwise. Default False because on 2026-08-06 the ground-truth
# test set was 9/9 with pure BM25 and 0/9 with hybrid (hybrid
# promoted semantically-similar-but-irrelevant chunks to top-1).
# Turn this on once you've validated hybrid works for YOUR queries
# via `python app.py search-hybrid "..."` and `tests/run_qa.py --hybrid`.
USE_HYBRID_SEARCH_WHEN_READY = False


# --- Synthesis answer-groundedness ------------------------------------
# When validating the LLM's synthesized answer, we require that at least
# one of its cited-and-real chunks has a meaningful *answer-token*
# overlap with the LLM's response. Without this, the LLM can cite a
# real but irrelevant chunk (e.g. one that happened to score above the
# BM25 noise floor) to pass the citation-existence check, then proceed
# to hallucinate an answer "grounded" in that off-topic chunk.
#
# Definition: coverage = |a_tokens ∩ c_tokens| / |a_tokens|.
#   - a_tokens = unique tokens of the LLM's response (with [来源: ...]
#                tags stripped out)
#   - c_tokens = unique tokens of the cited chunk
#
# This is fundamentally different from "question ↔ chunk" overlap (which
# I tried first and dropped on 2026-08-06): the question is usually
# short and phrased colloquially ("PKI 是什么?"), while the chunk is
# long and academic; their token overlap is near-zero even when the
# answer is great. Checking the *LLM's response* ↔ chunk is what we
# actually care about: did the LLM copy/paraphrase content from the
# source, or is it hallucinating on top of a real-looking citation?
#
# 0.20 means at least 20% of the LLM's unique tokens must appear in the
# cited chunk. Tuned to be strict enough to reject "the LLM cited a
# real PDF but the answer is completely made up", while not so strict
# that legitimate paraphrased answers get rejected.
SYNTH_ANSWER_GROUNDEDNESS_MIN = 0.20
# Hard absolute floor: even if coverage is high, require at least this
# many token matches. Defends against degenerate cases (1-token answer
# matching 1 chunk token, etc.).
SYNTH_ANSWER_GROUNDEDNESS_MIN_INTER = 2

# Don't even try to rewrite very short queries — short strings like "PKI"
# are usually already optimal for BM25 and rewriting risks noise.
REWRITE_MIN_QUERY_LEN = 4

# If a hit is found with score below this, it's reported as "weak" — the
# answer is still shown but the user is hinted that they can /expand.
WEAK_HINT_THRESHOLD = 1.0