"""
dedupe_chunks.py — One-shot cleanup for chunks that have internally-
duplicated paragraphs.

When this matters
-----------------
On 2026-08-07 the user reported 'the source-material chunks I see in
bot replies are sometimes garbled / duplicate'. Investigation showed
OSG10 中英对照版.pdf (an OCR'd PDF) had been chunked with internal
duplication — each chunk contained a paragraph, the SAME paragraph
again, then the next paragraph. Root cause: the underlying OCR
engine (MiniMax-M3) emitted each term pair twice in a row for the
'中英对照' glossary pages.

What this script does
---------------------
Walks every chunk in the database and applies the same
dedupe-consecutive-paragraphs rule that `chunks.chunk_text` now uses
on ingest. Updates the chunk_text in place. Reports how many chunks
were shortened and by how much.

After running, you also need to:
    python app.py rebuild              # rebuild BM25 index from new chunks
    python app.py rebuild --with-embeddings  # also re-embed
(both are safe to run any time)
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chunks as chunker  # noqa: E402
import storage  # noqa: E402


logger = logging.getLogger("dedupe_chunks")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument(
        "--dry-run", action="store_true",
        help="report what would change without writing",
    )
    args = p.parse_args()

    storage.init_db()
    conn = storage.get_conn()
    try:
        cur = conn.execute("SELECT id, chunk_text FROM chunks")
        all_chunks = list(cur)
    finally:
        conn.close()

    print(f"Scanning {len(all_chunks)} chunks for internal line-block duplication…")
    to_update: list[tuple[int, str, int, int]] = []  # (id, new_text, old_len, new_len)
    for r in all_chunks:
        text = r["chunk_text"]
        new_text = chunker._dedupe_repeated_line_blocks(text, min_block=2)
        if new_text == text:
            continue
        to_update.append((r["id"], new_text, len(text), len(new_text)))
    print(f"Found {len(to_update)} chunks with internal duplication.")
    if not to_update:
        return 0

    # Show worst offenders
    to_update.sort(key=lambda x: x[2] - x[3], reverse=True)
    print()
    print("Top 10 worst (by chars removed):")
    for cid, new_text, old, new in to_update[:10]:
        print(f"  chunk_id={cid}  {old}→{new} chars (removed {old - new})")

    if args.dry_run:
        print()
        print("--dry-run: not writing changes.")
        return 0

    # Apply updates
    storage.init_db()
    conn = storage.get_conn()
    try:
        with storage.tx(conn):
            conn.executemany(
                "UPDATE chunks SET chunk_text = ? WHERE id = ?",
                [(t, cid) for cid, t, _o, _n in to_update],
            )
    finally:
        conn.close()
    total_removed = sum(o - n for _, _, o, n in to_update)
    print()
    print(f"Updated {len(to_update)} chunks, removed {total_removed} chars total.")
    print()
    print("Next steps:")
    print("  python app.py rebuild                    # rebuild BM25 index")
    print("  python app.py rebuild --with-embeddings  # also re-embed (slower)")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
