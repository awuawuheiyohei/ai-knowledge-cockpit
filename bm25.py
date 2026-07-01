"""
bm25.py — Inverted index + BM25 scoring, zero external deps.

Design notes
------------
- We don't pull in `rank_bm25` or `whoosh`. The index is small enough to
  live entirely in SQLite, which means: single file, no daemon, easy to
  back up.
- Tokens: ASCII word boundaries + CJK bigrams when USE_BIGRAM_FOR_CJK.
  Stopwords are NOT removed at index time — short common terms still help
  discriminate in BM25, and SQLite is fast enough we don't need to prune.
- Score: standard BM25Okapi, computed per-query from per-term stats.

Hard rule: no LLM involved. This is pure information retrieval.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Iterable

import config
from storage import (
    get_conn,
    iter_term_postings,
    corpus_stats,
)


# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------

# Latin word: a-z, digits, underscore. We lowercase. CJK runs are kept whole
# and bigrammed at tokenization time.
_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]+")
# CJK Unified Ideographs + extension A. Covers the common Simplified/Traditional
# range. We deliberately do NOT include Japanese kana — out of scope for this KB.
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")


def tokenize(text: str) -> list[str]:
    """
    Convert a string into a list of tokens suitable for BM25 indexing.

    Latin words are kept as-is (lowercased).
    CJK characters are split into bigrams (each overlapping pair = one token).
    """
    if not text:
        return []
    text = text.lower()
    tokens: list[str] = []

    for m in _LATIN_RE.finditer(text):
        tokens.append(m.group(0))

    if config.USE_BIGRAM_FOR_CJK:
        cjk_chars = _CJK_RE.findall(text)
        for i in range(len(cjk_chars) - 1):
            tokens.append(cjk_chars[i] + cjk_chars[i + 1])

    return tokens


# ---------------------------------------------------------------------------
# Inverted index writes
# ---------------------------------------------------------------------------

def index_document(doc_id: int, chunks: list[str]) -> int:
    """
    Add a document's chunks to the inverted index.

    Idempotent: deletes any existing postings for this doc_id first.

    Returns the number of chunks indexed.
    """
    conn = get_conn()
    try:
        conn.execute("DELETE FROM index_term WHERE doc_id = ?", (doc_id,))

        inverted: dict[str, dict[int, int]] = {}

        for chunk_idx, text in enumerate(chunks):
            tokens = tokenize(text)
            if not tokens:
                continue
            tf = Counter(tokens)
            for term, count in tf.items():
                inverted.setdefault(term, {})[chunk_idx] = count

        rows = []
        for term, postings in inverted.items():
            for chunk_idx, tf in postings.items():
                rows.append((term, doc_id, chunk_idx, float(tf)))
        conn.executemany(
            "INSERT INTO index_term(term, doc_id, chunk_id, tf) VALUES (?,?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return len(chunks)


def remove_document_from_index(doc_id: int) -> None:
    """Drop all index postings belonging to a document."""
    conn = get_conn()
    try:
        conn.execute("DELETE FROM index_term WHERE doc_id = ?", (doc_id,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# BM25 scoring
# ---------------------------------------------------------------------------

def _fetch_chunk_lengths(chunk_keys: Iterable[tuple[int, int]]) -> dict[tuple[int, int], int]:
    """Bulk fetch chunk lengths for the given (doc_id, chunk_id) pairs."""
    chunk_keys = list(chunk_keys)
    if not chunk_keys:
        return {}
    conn = get_conn()
    out: dict[tuple[int, int], int] = {}
    try:
        for doc_id, chunk_id in chunk_keys:
            cur = conn.execute(
                "SELECT LENGTH(chunk_text) FROM chunks WHERE doc_id = ? AND chunk_id = ?",
                (doc_id, chunk_id),
            )
            r = cur.fetchone()
            if r:
                out[(doc_id, chunk_id)] = r[0]
    finally:
        conn.close()
    return out


def score_query(query: str, top_k: int = config.DEFAULT_TOP_K) -> list[dict]:
    """
    Score every chunk against the query using BM25Okapi.

    Returns a list of dicts: {doc_id, chunk_id, chunk_index, score, page_num,
    filename, source_type, relative_path, chunk_text}, sorted by score desc,
    capped at top_k. Empty list if no terms hit.
    """
    query_tokens = tokenize(query)
    if not query_tokens:
        return []

    q_tf = Counter(set(query_tokens))

    stats = corpus_stats()
    # We index and score at chunk granularity. Each chunk is one "document"
    # for BM25 purposes, so N = number of chunks. This avoids the degenerate
    # case where every term in a single-doc corpus has df == n_docs and
    # thus a negative IDF.
    N = stats["n_chunks"]
    if N == 0:
        return []

    avgdl = stats["avg_chunk_len"]
    k1 = config.BM25_K1
    b = config.BM25_B

    # Pass 1: gather candidate chunks + per-term (df, idf).
    chunk_scores: dict[tuple[int, int], float] = {}
    term_df_idf: dict[str, float] = {}
    term_postings: dict[str, list[tuple[int, int, float]]] = {}

    # Junk-query guard: a query whose majority of unique terms has no
    # posting at all should return zero hits. Without this, a typo-laden
    # query like "abcdef 不存在的词 xyz" matches on a single bigram like
    # "存在" and returns noise. Require >=50% of unique terms to appear in
    # the index.
    uniq_terms = list(q_tf.keys())
    terms_with_hits = 0
    per_term_postings: dict[str, list[tuple[int, int, float]]] = {}
    for term in uniq_terms:
        postings = list(iter_term_postings(term))
        per_term_postings[term] = postings
        if postings:
            terms_with_hits += 1
    if uniq_terms and (terms_with_hits / len(uniq_terms)) < 0.5:
        return []

    for term, postings in per_term_postings.items():
        if not postings:
            continue
        unique_chunks = {(d, c) for d, c, _ in postings}
        df = len(unique_chunks)
        idf = math.log(1 + (N - df + 0.5) / (df + 0.5))
        if idf <= 0:
            continue
        term_df_idf[term] = idf
        term_postings[term] = postings
        for d, c, _ in postings:
            chunk_scores.setdefault((d, c), 0.0)

    if not chunk_scores:
        return []

    # Pull all chunk lengths in one go.
    chunk_lens = _fetch_chunk_lengths(chunk_scores.keys())

    # Pass 2: score.
    for term, idf in term_df_idf.items():
        for doc_id, chunk_id, tf in term_postings[term]:
            dl = chunk_lens.get((doc_id, chunk_id))
            if not dl:
                continue
            norm = 1 - b + b * (dl / avgdl)
            contrib = idf * (tf * (k1 + 1)) / (tf + k1 * norm)
            chunk_scores[(doc_id, chunk_id)] += contrib

    ranked = sorted(chunk_scores.items(), key=lambda kv: kv[1], reverse=True)
    top = ranked[: max(1, min(top_k, config.MAX_TOP_K))]

    # Hydrate with chunk_text + document metadata.
    conn = get_conn()
    try:
        results: list[dict] = []
        for (doc_id, chunk_id), score in top:
            cur = conn.execute(
                """
                SELECT c.chunk_text, c.page_num, c.chunk_index,
                       d.filename, d.source_type, d.relative_path
                FROM chunks c JOIN documents d ON d.id = c.doc_id
                WHERE c.doc_id = ? AND c.chunk_id = ?
                """,
                (doc_id, chunk_id),
            )
            r = cur.fetchone()
            if not r:
                continue
            chunk_text, page_num, chunk_index, filename, source_type, relpath = r
            results.append(
                {
                    "doc_id": doc_id,
                    "chunk_id": chunk_id,
                    "chunk_index": chunk_index,
                    "score": score,
                    "page_num": page_num,
                    "filename": filename,
                    "source_type": source_type,
                    "relative_path": relpath,
                    "chunk_text": chunk_text,
                }
            )
        return results
    finally:
        conn.close()