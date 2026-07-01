"""
storage.py — SQLite layer for the knowledge base.

Schema
------
documents       — one row per ingested source file (PDF or Markdown)
chunks          — one row per text chunk with metadata
index_term      — BM25 inverted index: (term, doc_id, chunk_id) -> tf

Design notes
------------
- Single SQLite file (`data/kb.sqlite`). Easy to back up, easy to inspect.
- WAL mode + indexes for fast read/write.
- The inverted index lives in SQLite too. This keeps everything in one file
  and avoids memory-blowing the working set when corpora grow.
- No LLM-touching code lives here. Pure data.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterable

from paths import DB_PATH, ensure_dirs

_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    filename      TEXT NOT NULL,            -- display name (e.g. foo.pdf)
    source_type   TEXT NOT NULL,            -- 'pdf' | 'markdown'
    relative_path TEXT NOT NULL,            -- path inside inbox/, used to find the file
    file_hash     TEXT NOT NULL UNIQUE,     -- sha1 of bytes; dedupe key
    page_count    INTEGER,                  -- for PDF: total pages; NULL for MD
    char_count    INTEGER NOT NULL,         -- total extracted chars
    status        TEXT NOT NULL,            -- 'ok' | 'partial' | 'failed'
    scan_pages    TEXT,                     -- JSON list of page numbers that looked scanned (NULL for MD or fully OCR'd)
    ocr_pages     TEXT,                     -- JSON list of page numbers OCR'd via VL
    ingested_at   TEXT NOT NULL             -- ISO timestamp
);

CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(file_hash);
CREATE INDEX IF NOT EXISTS idx_documents_filename ON documents(filename);

CREATE TABLE IF NOT EXISTS chunks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id       INTEGER NOT NULL,
    chunk_id     INTEGER NOT NULL,          -- index within the document (0-based)
    chunk_index  INTEGER NOT NULL,          -- same as chunk_id kept for clarity in joins
    page_num     INTEGER,                   -- source page number; NULL for MD
    chunk_text   TEXT NOT NULL,
    via_ocr      INTEGER NOT NULL DEFAULT 0, -- 1 if this chunk's text came from VL OCR
    FOREIGN KEY (doc_id) REFERENCES documents(id) ON DELETE CASCADE,
    UNIQUE (doc_id, chunk_id)
);

CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);

-- BM25 inverted index. (term, doc_id, chunk_id) is unique.
CREATE TABLE IF NOT EXISTS index_term (
    term     TEXT NOT NULL,
    doc_id   INTEGER NOT NULL,
    chunk_id INTEGER NOT NULL,
    tf       REAL NOT NULL,
    PRIMARY KEY (term, doc_id, chunk_id)
);

CREATE INDEX IF NOT EXISTS idx_index_term_term ON index_term(term);
CREATE INDEX IF NOT EXISTS idx_index_term_doc ON index_term(doc_id);
"""


def get_conn() -> sqlite3.Connection:
    """
    Open a SQLite connection.

    Each call returns a fresh connection. SQLite is fine with this for
    single-process CLI use; we don't share connections across threads.
    """
    ensure_dirs()
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def tx(conn: sqlite3.Connection):
    """Tiny transaction helper that BEGIN/COMMITs around a block."""
    with _LOCK:  # serialized; CLI is single-threaded but be safe
        conn.execute("BEGIN")
        try:
            yield
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def init_db() -> None:
    """Create tables if they don't exist. Safe to call repeatedly."""
    conn = get_conn()
    try:
        conn.executescript(_SCHEMA)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Document operations
# ---------------------------------------------------------------------------

def add_document(
    *,
    filename: str,
    source_type: str,
    relative_path: str,
    file_hash: str,
    page_count: int | None,
    char_count: int,
    status: str,
    scan_pages: list[int] | None,
    ocr_pages: list[int] | None = None,
) -> int:
    """
    Insert a documents row. If the file_hash already exists, returns the
    existing row's id (idempotent re-ingestion of the same file).

    Returns the document id.
    """
    import json
    from datetime import datetime, timezone

    conn = get_conn()
    try:
        cur = conn.execute("SELECT id FROM documents WHERE file_hash = ?", (file_hash,))
        row = cur.fetchone()
        if row:
            return row["id"]

        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cur = conn.execute(
            """
            INSERT INTO documents
                (filename, source_type, relative_path, file_hash, page_count,
                 char_count, status, scan_pages, ocr_pages, ingested_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                filename,
                source_type,
                relative_path,
                file_hash,
                page_count,
                char_count,
                status,
                json.dumps(scan_pages) if scan_pages else None,
                json.dumps(ocr_pages) if ocr_pages else None,
                ts,
            ),
        )
        return cur.lastrowid
    finally:
        conn.close()


def replace_document(
    *,
    doc_id: int,
    filename: str,
    source_type: str,
    relative_path: str,
    page_count: int | None,
    char_count: int,
    status: str,
    scan_pages: list[int] | None,
    ocr_pages: list[int] | None = None,
) -> None:
    """Overwrite a document's metadata + reset its chunks/index."""
    import json
    from datetime import datetime, timezone

    conn = get_conn()
    try:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute(
            """
            UPDATE documents
               SET filename=?, source_type=?, relative_path=?, page_count=?,
                   char_count=?, status=?, scan_pages=?, ocr_pages=?, ingested_at=?
             WHERE id=?
            """,
            (
                filename,
                source_type,
                relative_path,
                page_count,
                char_count,
                status,
                json.dumps(scan_pages) if scan_pages else None,
                json.dumps(ocr_pages) if ocr_pages else None,
                ts,
                doc_id,
            ),
        )
        conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM index_term WHERE doc_id = ?", (doc_id,))
        conn.commit()
    finally:
        conn.close()


def list_documents() -> list[dict]:
    """Return all documents, newest first."""
    conn = get_conn()
    try:
        cur = conn.execute(
            """
            SELECT d.id, d.filename, d.source_type, d.relative_path, d.page_count,
                   d.char_count, d.status, d.scan_pages, d.ingested_at,
                   (SELECT COUNT(*) FROM chunks c WHERE c.doc_id = d.id) AS chunk_count
              FROM documents d
             ORDER BY d.ingested_at DESC, d.id DESC
            """
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_document(doc_id: int) -> dict | None:
    conn = get_conn()
    try:
        cur = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,))
        r = cur.fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def get_document_by_hash(file_hash: str) -> dict | None:
    conn = get_conn()
    try:
        cur = conn.execute("SELECT * FROM documents WHERE file_hash = ?", (file_hash,))
        r = cur.fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def get_document_by_name(filename: str) -> dict | None:
    """Find a document by exact filename (basename match)."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "SELECT * FROM documents WHERE filename = ? ORDER BY id DESC LIMIT 1",
            (filename,),
        )
        r = cur.fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def delete_document(doc_id: int) -> None:
    """Cascade-delete: chunks + index_term rows go too via FK + manual cleanup."""
    conn = get_conn()
    try:
        conn.execute("DELETE FROM index_term WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Chunk operations
# ---------------------------------------------------------------------------

def insert_chunks(doc_id: int, chunks: list[dict]) -> None:
    """
    Bulk-insert chunks for a document. Each chunk dict needs:
      - chunk_index: int
      - page_num: int | None
      - chunk_text: str
      - via_ocr: bool (optional, default False)
    """
    if not chunks:
        return
    conn = get_conn()
    try:
        rows = [
            (
                doc_id,
                c["chunk_index"],
                c["chunk_index"],
                c["page_num"],
                c["chunk_text"],
                1 if c.get("via_ocr") else 0,
            )
            for c in chunks
        ]
        conn.executemany(
            """
            INSERT OR REPLACE INTO chunks (doc_id, chunk_id, chunk_index, page_num, chunk_text, via_ocr)
            VALUES (?,?,?,?,?,?)
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def get_chunk_texts(doc_id: int) -> list[str]:
    conn = get_conn()
    try:
        cur = conn.execute(
            "SELECT chunk_text FROM chunks WHERE doc_id = ? ORDER BY chunk_id",
            (doc_id,),
        )
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Index helpers (consumed by bm25.py)
# ---------------------------------------------------------------------------

def iter_term_postings(term: str) -> Iterable[tuple[int, int, float]]:
    """
    Stream postings for a single term. Returns (doc_id, chunk_id, tf) tuples.
    """
    conn = get_conn()
    try:
        cur = conn.execute(
            "SELECT doc_id, chunk_id, tf FROM index_term WHERE term = ?",
            (term,),
        )
        for r in cur:
            yield (r[0], r[1], r[2])
    finally:
        conn.close()


def corpus_stats() -> dict:
    """
    Compute corpus-wide stats needed for BM25: number of chunks, average
    chunk length. Cheap because we use SQLite aggregate functions.
    """
    conn = get_conn()
    try:
        n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        if n_chunks == 0:
            return {"n_docs": 0, "n_chunks": 0, "avg_chunk_len": 1.0}
        avg_len = conn.execute("SELECT AVG(LENGTH(chunk_text)) FROM chunks").fetchone()[0] or 1.0
        n_docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        return {
            "n_docs": n_docs,
            "n_chunks": n_chunks,
            "avg_chunk_len": max(1.0, float(avg_len)),
        }
    finally:
        conn.close()