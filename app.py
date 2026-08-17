"""
app.py — CLI entry point for the AI Knowledge Cockpit (v2).

Design philosophy
-----------------
- One command per verb. No flags hidden inside subcommands.
- All output is plain text — friendly to terminals, logs, and future IM bridges.
- No LLM calls anywhere. This is a retrieval tool, not a chatbot.

Commands
--------
  ingest  <path> [--recursive]   add PDF/MD files into the KB
  list                           show all ingested sources
  search <query> [--top N] [--doc NAME]
                                 keyword search over the KB
  remove <filename|id>           remove a source from the KB
  rebuild                        rebuild the BM25 index from scratch
  status                         show KB statistics
  serve wecom                    start WeCom (Enterprise WeChat) callback server
  serve dingtalk                 start DingTalk Stream-mode bot
"""
from __future__ import annotations

import argparse
import json
import re
import sys

import config
import paths
import storage
import bm25
import ingest
import search as search_mod


# ---------------------------------------------------------------------------
# CISSP CBK domain classification (for `status` coverage report)
# ---------------------------------------------------------------------------
# The 8 domains of (ISC)² CISSP Common Body of Knowledge. Used only for
# rendering the `status` coverage map — does not affect retrieval.

CISSP_DOMAINS: list[tuple[int, str]] = [
    (1, "安全与风险管理"),
    (2, "资产安全"),
    (3, "安全架构与工程"),
    (4, "通信与网络安全"),
    (5, "身份与访问管理"),
    (6, "安全评估与测试"),
    (7, "安全运营"),
    (8, "软件开发安全"),
]

# OSG chapter -> CBK domain. From the (ISC)² official CBK domain weighting.
# Source mapping: chapters 1-4 -> D1, ch5 -> D2, ch6-10 -> D3, ch11-12 -> D4,
# ch13-14 -> D5, ch15 -> D6, ch16-19 -> D7, ch20-21 -> D8.
CHAPTER_TO_DOMAIN: dict[int, int] = {
    1: 1, 2: 1, 3: 1, 4: 1,
    5: 2,
    6: 3, 7: 3, 8: 3, 9: 3, 10: 3,
    11: 4, 12: 4,
    13: 5, 14: 5,
    15: 6,
    16: 7, 17: 7, 18: 7, 19: 7,
    20: 8, 21: 8,
}


def classify_cissp_domain(filename: str) -> int | None:
    """
    Map a document filename to a CISSP CBK domain (1-8) if possible.

    Recognized patterns:
      - "域N：..."  -> N  (the per-domain PDFs)
      - "第N章-..." -> domain via CHAPTER_TO_DOMAIN

    Returns None for general references (OSG9/10), 综合测试, mocks, etc.
    """
    m = re.match(r"^域(\d+)", filename)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 8:
            return n
    m = re.match(r"^第(\d+)章", filename)
    if m:
        return CHAPTER_TO_DOMAIN.get(int(m.group(1)))
    return None


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def cmd_init(args) -> int:
    """Make sure the DB exists and dirs are present. Always safe to re-run."""
    paths.ensure_dirs()
    storage.init_db()
    print(f"KB ready at {paths.DB_PATH}")
    return 0


def cmd_ingest(args) -> int:
    paths.ensure_dirs()
    storage.init_db()
    summaries = ingest.ingest_path(
        args.path,
        recursive=args.recursive,
        use_ocr=args.ocr,
    )
    ingest.print_summaries(summaries)
    failures = [s for s in summaries if s.status == "failed"]
    return 1 if failures else 0


def cmd_list(args) -> int:
    storage.init_db()
    docs = storage.list_documents()
    if not docs:
        print("(no documents ingested yet)")
        return 0
    print(f"{'ID':>4}  {'STATUS':<8}  {'TYPE':<8}  {'PAGES':>5}  {'CHUNKS':>6}  {'CHARS':>7}  FILENAME")
    for d in docs:
        pages = d["page_count"] if d["page_count"] is not None else "-"
        print(
            f"{d['id']:>4}  {d['status']:<8}  {d['source_type']:<8}  "
            f"{pages!s:>5}  {d['chunk_count']:>6}  {d['char_count']:>7}  {d['filename']}"
        )
    return 0


def cmd_search(args) -> int:
    storage.init_db()
    if not storage.list_documents():
        print("(no documents ingested yet — run `ingest` first)")
        return 1
    hits = search_mod.search(args.query, top_k=args.top, filename=args.doc)
    search_mod.render(hits)
    return 0


def cmd_search_hybrid(args) -> int:
    """
    Run the same query through the hybrid (BM25 + embedding) pipeline,
    ignoring `USE_HYBRID_SEARCH_WHEN_READY`. Use this to A/B test
    hybrid against pure BM25 (`search`) and decide whether to flip
    the config flag.
    """
    storage.init_db()
    if not storage.list_documents():
        print("(no documents ingested yet — run `ingest` first)")
        return 1
    n_emb = storage.count_embeddings(config.EMBEDDING_MODEL)
    if n_emb == 0:
        print("(no embeddings — run `rebuild --with-embeddings` first)")
        return 1
    hits = search_mod.hybrid_search(
        args.query, top_k=max(args.top * 3, 30),  # overfetch so doc filter still has hits
    )
    if args.doc:
        hits = [h for h in hits if h["filename"] == args.doc]
    hits = hits[: args.top]
    search_mod.render(hits)
    return 0


def cmd_remove(args) -> int:
    storage.init_db()
    target = args.target

    # Try as id first if it looks numeric.
    doc = None
    if target.isdigit():
        doc = storage.get_document(int(target))
    if doc is None:
        doc = storage.get_document_by_name(target)

    if doc is None:
        print(f"No document matched: {target}")
        return 1

    storage.delete_document(doc["id"])
    print(f"Removed: {doc['filename']} (id={doc['id']})")
    return 0


def cmd_rebuild(args) -> int:
    """
    Wipe the index tables and rebuild from the chunks table.

    Use this if index stats get out of sync (rare, but possible if you
    poke the DB directly). Chunks and documents are preserved.

    With --with-embeddings: also compute (or recompute) the embedding
    vectors for every chunk. First run is slow (~30-60s for 20K chunks
    on M-series CPU) because it downloads the sentence-transformer
    model on first call; subsequent runs are deltas-only.
    """
    import time
    import embedding

    storage.init_db()
    conn = storage.get_conn()
    try:
        n = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        if n == 0:
            print("(no documents — nothing to rebuild)")
            return 0
        conn.execute("DELETE FROM index_term")
        conn.commit()
    finally:
        conn.close()

    # Optional: re-run the extract+chunk pipeline on every doc, so
    # changes to chunks.py / pdf_extract.py / sections.py take effect.
    if args.rechunk:
        if args.only_ocr:
            # Strip non-OCR docs from the work list. We re-load the
            # list here because the original `docs` variable above
            # was the FULL list; cmd_rebuild_rechunk does its own
            # list_documents() so we just narrow via a kwarg.
            pass  # handled inside cmd_rebuild_rechunk
        return cmd_rebuild_rechunk(args)

    # Re-index every document.
    docs = storage.list_documents()
    rebuilt = 0
    for d in docs:
        texts = storage.get_chunk_texts(d["id"])
        if texts:
            bm25.index_document(d["id"], texts)
            rebuilt += 1
    print(f"Rebuilt BM25 index for {rebuilt}/{len(docs)} document(s).")

    # Optional: rebuild embeddings too.
    if args.with_embeddings:
        if not embedding.is_available():
            print("⚠️  Embedding model not loadable — skipping. "
                  "(Check network + sentence-transformers install.)")
            if embedding.last_load_error():
                print(f"   last error: {embedding.last_load_error()}")
            return 0

        # Drop existing rows for this model so we get a clean rebuild.
        if args.drop_embeddings:
            n_drop = storage.drop_embeddings(config.EMBEDDING_MODEL)
            print(f"Dropped {n_drop} old embeddings for {config.EMBEDDING_MODEL}.")

        pending = storage.get_chunks_needing_embeddings(config.EMBEDDING_MODEL)
        if not pending:
            print(f"All chunks already have embeddings for {config.EMBEDDING_MODEL}.")
            return 0

        print(f"Computing embeddings for {len(pending)} chunks "
              f"(model={config.EMBEDDING_MODEL})…")
        t0 = time.time()
        # Embed in batches so the encode() progress is bounded.
        BATCH = 256
        for i in range(0, len(pending), BATCH):
            batch = pending[i:i + BATCH]
            texts = [c[2] for c in batch]
            vectors = embedding.embed_texts(texts, batch_size=64)
            if vectors is None:
                print(f"⚠️  embed_texts returned None at batch {i}; stopping.")
                break
            rows = [
                (cid, did, config.EMBEDDING_MODEL, vectors.shape[1],
                 vectors[j].tobytes())
                for j, (cid, did, _txt) in enumerate(batch)
            ]
            storage.insert_embeddings(rows)
            done = min(i + BATCH, len(pending))
            print(f"  {done}/{len(pending)} embedded "
                  f"({(time.time() - t0):.1f}s elapsed)")
        total = time.time() - t0
        print(f"Done. {len(pending)} embeddings stored in {total:.1f}s.")
    return 0


def cmd_rebuild_rechunk(args) -> int:
    """
    Re-run the full extract+chunk pipeline on every document.

    Use this after changing the chunker / PDF extractor / section
    parser — the BM25 index and embedding table are still consistent
    with the chunks table, but the chunks themselves are stale.

    The original files are read from the `data/originals/` mirror
    written by ingest. If a file is missing from the mirror, that
    document is skipped (logged) and left as-is.

    Implementation note: we can't just call `ingest._ingest_pdf` /
    `_ingest_markdown` because they do a file-hash dedupe that
    short-circuits re-chunking. Instead we inline the per-doc
    extract + chunk + persist steps, leaving the `documents` row
    intact (so doc_id stays the same and downstream indexes don't
    need to be re-keyed).
    """
    import time
    import ingest
    import bm25
    import embedding
    import paths
    import pdf_extract
    import md_extract
    import chunks as chunker
    import sections

    storage.init_db()
    all_docs = storage.list_documents()
    if not all_docs:
        print("(no documents — nothing to rechunk)")
        return 0
    # If --only-ocr, narrow to docs that have ocr_pages set.
    if getattr(args, "only_ocr", False):
        import json as _json
        docs = [
            d for d in all_docs
            if d.get("ocr_pages") and d["ocr_pages"] != "[]"
        ]
        skipped = len(all_docs) - len(docs)
        print(f"--only-ocr: skipping {skipped} non-OCR docs, "
              f"processing {len(docs)} OCR doc(s).")
    else:
        docs = all_docs

    # If --doc is given, narrow to that one filename.
    if getattr(args, "doc", None):
        wanted = args.doc
        docs = [d for d in docs if d["filename"] == wanted or d["relative_path"] == wanted]
        if not docs:
            print(f"--doc {wanted!r}: no matching document found")
            return 1
        print(f"--doc: narrowed to {len(docs)} doc(s) matching {wanted!r}.")

    no_clear = getattr(args, "no_clear", False)
    # Drop ALL chunks + embeddings + index (rebuild from scratch) —
    # unless --no-clear is set (per-doc restart-safe mode).
    conn = storage.get_conn()
    try:
        if no_clear:
            # Only drop the chunks for the docs we're about to process.
            for d in docs:
                conn.execute("DELETE FROM index_term WHERE doc_id = ?", (d["id"],))
                conn.execute("DELETE FROM chunk_embeddings WHERE doc_id = ?", (d["id"],))
                conn.execute("DELETE FROM chunks WHERE doc_id = ?", (d["id"],))
            conn.commit()
            print(f"Cleared chunks/embeddings/index for {len(docs)} target doc(s) (no-clear mode).")
        else:
            conn.execute("DELETE FROM index_term")
            conn.execute("DELETE FROM chunk_embeddings")
            conn.execute("DELETE FROM chunks")
            conn.commit()
            print(f"Cleared all chunks/embeddings/index for {len(docs)} docs.")
    finally:
        conn.close()

    started = time.time()
    ok = 0
    skipped = 0

    # If the source has ocr_pages set, we MUST re-OCR — these pages
    # are scans with no text layer; the default extract path returns
    # empty text. We dispatch to a path that supplies an OCR
    # callback so the re-ingest is faithful to the original.
    import vl_config
    import pdf_ocr
    cfg = None
    ocr_needed = any(
        d.get("ocr_pages") and d["ocr_pages"] != "[]"
        for d in docs if d["source_type"] == "pdf"
    )
    if ocr_needed:
        try:
            cfg = vl_config.load_vl_config()
            print(f"OCR: VL config OK (model={cfg.model}). "
                  f"Will re-OCR any docs that need it.")
        except Exception as e:
            print(f"⚠️  OCR-needed docs present but VL config unavailable: {e}")
            print("    Will skip OCR'd docs unless you set VL_API_KEY.")
            cfg = None

    for d in docs:
        rel = d["relative_path"]
        src = paths.BASE / rel
        if not src.is_file():
            print(f"  skip {d['filename']}: original not found at {src}")
            skipped += 1
            continue
        doc_id = d["id"]
        # Build per-doc OCR callback if this is an OCR'd doc.
        ocr_callback = None
        if (
            d["source_type"] == "pdf"
            and d.get("ocr_pages")
            and d["ocr_pages"] != "[]"
            and cfg is not None
        ):
            ocr_usage = pdf_ocr.OcrUsage()
            def _cb(page, page_num, _cfg=cfg, _u=ocr_usage):
                return pdf_ocr.ocr_page(page, page_num, _cfg, _u)
            ocr_callback = _cb
        try:
            if d["source_type"] == "pdf":
                result = pdf_extract.extract_pdf(str(src), ocr_callback=ocr_callback)
                non_scan = [p for p in result.pages if not p.is_scanned]
                # Reuse the existing section-aware chunker logic from
                # ingest._ingest_pdf (copy it inline so we don't trigger
                # the hash-dedupe short-circuit).
                pdf_secs = sections.parse_pdf_sections(str(src))
                chunk_records: list[dict] = []
                chunk_texts: list[str] = []
                char_count_total = 0
                next_idx = 0
                page_by_num = {p.page_num: p for p in result.pages}
                last_page = max(page_by_num.keys()) if page_by_num else 0
                if len(pdf_secs) == 1 and not pdf_secs[0].heading:
                    for page in result.pages:
                        if page.is_scanned:
                            continue
                        text = page.text
                        if not text.strip():
                            continue
                        char_count_total += len(text)
                        for piece in chunker.chunk_text(text):
                            chunk_records.append({
                                "chunk_index": next_idx,
                                "page_num": page.page_num,
                                "chunk_text": piece,
                                "via_ocr": page.via_ocr,
                            })
                            chunk_texts.append(piece)
                            next_idx += 1
                else:
                    for i, sec in enumerate(pdf_secs):
                        start = sec.page_num or 1
                        if i + 1 < len(pdf_secs):
                            next_start = pdf_secs[i + 1].page_num or (start + 1)
                            end = next_start - 1
                        else:
                            end = last_page
                        for pn in range(start, end + 1):
                            page = page_by_num.get(pn)
                            if page is None or page.is_scanned:
                                continue
                            text = page.text
                            if not text.strip():
                                continue
                            char_count_total += len(text)
                            for piece in chunker.chunk_text(text):
                                piece_with_ctx = sections.prefix_chunk(piece, sec.heading)
                                chunk_records.append({
                                    "chunk_index": next_idx,
                                    "page_num": pn,
                                    "chunk_text": piece_with_ctx,
                                    "via_ocr": page.via_ocr,
                                })
                                chunk_texts.append(piece_with_ctx)
                                next_idx += 1
                if not chunk_records:
                    skipped += 1
                    print(f"  rechunk {d['filename']}: no text extracted")
                    continue
            else:
                # Markdown
                text, char_count = md_extract.read_markdown(str(src))
                chunk_records = []
                chunk_texts = []
                next_idx = 0
                for sec in sections.parse_markdown_sections(text):
                    for piece in chunker.chunk_text(sec.text):
                        piece_with_ctx = sections.prefix_chunk(piece, sec.heading)
                        chunk_records.append({
                            "chunk_index": next_idx,
                            "page_num": None,
                            "chunk_text": piece_with_ctx,
                        })
                        chunk_texts.append(piece_with_ctx)
                        next_idx += 1
                if not chunk_records:
                    skipped += 1
                    print(f"  rechunk {d['filename']}: empty markdown")
                    continue

            storage.insert_chunks(doc_id, chunk_records)
            bm25.index_document(doc_id, chunk_texts)
            ok += 1
            print(f"  [{ok:3d}/{len(docs)}] {d['filename']}  →  "
                  f"{len(chunk_records)} chunks")
        except Exception as e:
            skipped += 1
            print(f"  rechunk {d['filename']}: {type(e).__name__}: {e}")
    elapsed = time.time() - started

    # Re-compute embeddings for everything that needs them.
    if args.with_embeddings and embedding.is_available():
        pending = storage.get_chunks_needing_embeddings(config.EMBEDDING_MODEL)
        if pending:
            print(f"Computing embeddings for {len(pending)} rechunked chunks…")
            for i in range(0, len(pending), 256):
                batch = pending[i:i + 256]
                texts = [c[2] for c in batch]
                vectors = embedding.embed_texts(texts, batch_size=64)
                if vectors is None:
                    break
                rows = [
                    (cid, did, config.EMBEDDING_MODEL, vectors.shape[1],
                     vectors[j].tobytes())
                    for j, (cid, did, _txt) in enumerate(batch)
                ]
                storage.insert_embeddings(rows)
                done = min(i + 256, len(pending))
                print(f"  embeddings: {done}/{len(pending)}")

    new_stats = storage.corpus_stats()
    print(f"Rechunked {ok}/{len(docs)} documents "
          f"({skipped} skipped) in {elapsed:.1f}s. "
          f"New total: {new_stats['n_chunks']} chunks.")

    # Defensive: rebuild the BM25 inverted index after rechunk.
    # We have observed cases where --no-clear mode left the index in a
    # partial state (very few terms) even though chunks looked correct.
    # This step is idempotent and runs in O(chunks); ~5-30s for 20K chunks.
    if not getattr(args, "skip_index_rebuild", False):
        print("Rebuilding BM25 index (defensive)…")
        bm25.rebuild_index()
        n_terms = storage.count_index_terms()
        print(f"  index_terms: {n_terms}")
    return 0


def cmd_status(args) -> int:
    storage.init_db()
    docs = storage.list_documents()
    stats = storage.corpus_stats()
    n_total = len(docs)
    n_ok = sum(1 for d in docs if d["status"] == "ok")
    n_partial = sum(1 for d in docs if d["status"] == "partial")
    n_failed = sum(1 for d in docs if d["status"] == "failed")
    n_pdf = sum(1 for d in docs if d["source_type"] == "pdf")
    n_md = sum(1 for d in docs if d["source_type"] == "markdown")

    # Documents with scan-page warnings.
    scan_warnings: list[tuple[str, list[int]]] = []
    ocr_totals = {"docs": 0, "pages": 0}
    for d in docs:
        if d.get("scan_pages"):
            try:
                sp = json.loads(d["scan_pages"])
            except (TypeError, ValueError):
                sp = []
            if sp:
                scan_warnings.append((d["filename"], sp))
        if d.get("ocr_pages"):
            try:
                op = json.loads(d["ocr_pages"])
            except (TypeError, ValueError):
                op = []
            if op:
                ocr_totals["docs"] += 1
                ocr_totals["pages"] += len(op)

    print(f"DB path        : {paths.DB_PATH}")
    print(f"Documents      : {n_total}  (ok={n_ok}, partial={n_partial}, failed={n_failed})")
    print(f"  - PDFs       : {n_pdf}")
    print(f"  - Markdown   : {n_md}")
    print(f"Chunks         : {stats['n_chunks']}")
    print(f"Avg chunk len  : {stats['avg_chunk_len']:.1f} chars")
    print(f"Chunk size cfg : {config.CHUNK_SIZE} chars  (overlap {config.CHUNK_OVERLAP})")
    # BM25 inverted-index health. If this is ~0 while chunks > 0, the
    # index is degraded — run `app.py rebuild` (no args) to fix.
    n_terms = stats.get("n_index_terms", 0)
    if stats["n_chunks"] > 0 and n_terms < 1000:
        flag = " ⚠️  DEGRADED — run `app.py rebuild` to fix"
    else:
        flag = ""
    print(f"Index terms    : {n_terms}{flag}")

    # Embedding / hybrid-search status (Day 6).
    try:
        n_emb = storage.count_embeddings(config.EMBEDDING_MODEL)
        if n_emb == 0:
            print("Embeddings     : 0 (run `rebuild --with-embeddings` to enable hybrid search)")
        elif n_emb < stats["n_chunks"]:
            print(f"Embeddings     : {n_emb}/{stats['n_chunks']} "
                  f"({100*n_emb//max(1,stats['n_chunks'])}%, partial — run `rebuild --with-embeddings`)")
        else:
            print(f"Embeddings     : {n_emb}/{stats['n_chunks']} (full, hybrid search active)")
    except Exception as e:
        print(f"Embeddings     : (error reading: {e})")
    if scan_warnings:
        print("Scan warnings  :")
        for fn, pages in scan_warnings:
            print(f"  - {fn}: pages {pages}")
    else:
        print("Scan warnings  : none")
    if ocr_totals["pages"]:
        print(
            f"OCR'd          : {ocr_totals['pages']} pages across {ocr_totals['docs']} docs"
        )
    else:
        print("OCR'd          : 0 pages (use `ingest --ocr` to OCR scanned PDFs)")

    # CISSP CBK 8-domain coverage map.
    classified: dict[int, int] = {n: 0 for n, _ in CISSP_DOMAINS}
    unclassified_chunks = 0
    failed_in_classified = 0
    for d in docs:
        domain = classify_cissp_domain(d["filename"])
        if domain is not None:
            classified[domain] += d["chunk_count"]
            if d["status"] == "failed":
                failed_in_classified += 1
        else:
            unclassified_chunks += d["chunk_count"]

    print()
    print("CISSP CBK domain coverage (8 domains):")
    max_cls = max(classified.values()) if classified else 0
    if max_cls == 0:
        print("  (no per-domain docs yet — chapter PDFs/markdown drive this map)")
    else:
        for n, name in CISSP_DOMAINS:
            cnt = classified[n]
            bar_len = (20 * cnt) // max_cls if max_cls else 0
            bar = "=" * bar_len + " " * (20 - bar_len)
            pct = (100 * cnt // max_cls) if max_cls else 0
            print(f"  域{n} {name:<10}  [{bar}] {cnt:>5} chunks  ({pct:>3}%)")
        if unclassified_chunks:
            print(
                f"  (+ {unclassified_chunks} chunks in OSG9/10 general references — not per-domain classified)"
            )
        if failed_in_classified:
            print(
                f"  ! {failed_in_classified} classified doc(s) failed to extract — re-ingest with `--ocr` to fill coverage"
            )
    print(
        "  Tip: search within a domain ->  search \"...\" --doc \"第13章-...pdf\""
    )
    return 0


# ---------------------------------------------------------------------------
# IM server commands
# ---------------------------------------------------------------------------

def cmd_serve_wecom(args) -> int:
    """
    Start the WeCom (Enterprise WeChat) bot callback server.

    Reads config from environment (see im_config.load_wecom_config).
    Reaches a public URL via either:
      - your own reverse proxy / public server
      - a tunnel like ngrok (recommended for local dev)
    """
    try:
        from wecom_server import main as wecom_main
    except ImportError as e:
        print(f"wecom_server not available: {e}", file=sys.stderr)
        return 2
    try:
        wecom_main()
        return 0
    except ValueError as e:
        print(f"WeCom config error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0


def cmd_serve_dingtalk(args) -> int:
    """
    Start the DingTalk Stream-mode bot.

    No public callback URL needed — DingTalk opens a WebSocket to us.
    """
    try:
        from dingtalk_server import run as dt_run
    except ImportError as e:
        print(f"dingtalk_server not available: {e}", file=sys.stderr)
        return 2
    try:
        dt_run()
        return 0
    except ValueError as e:
        print(f"DingTalk config error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0


def cmd_serve_feishu(args) -> int:
    """
    Start the Feishu (Lark) bot webhook server.

    Needs a public HTTPS callback URL (use ngrok for local dev).
    The webhook URL is configured in the Feishu developer console's
    "Event Subscription" section. See FEISHU_SETUP.md.
    """
    try:
        from feishu_server import serve as fs_serve
    except ImportError as e:
        print(f"feishu_server not available: {e}", file=sys.stderr)
        return 2
    try:
        fs_serve()
        return 0
    except ValueError as e:
        print(f"Feishu config error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="app.py",
        description="AI Knowledge Cockpit — local, retrieval-only, no LLM.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="initialize DB and folders")
    p_init.set_defaults(func=cmd_init)

    p_ingest = sub.add_parser("ingest", help="ingest a file or directory")
    p_ingest.add_argument("path", help="PDF file, MD file, or directory")
    p_ingest.add_argument(
        "-r", "--recursive", action="store_true", help="recurse into subdirectories"
    )
    p_ingest.add_argument(
        "--ocr",
        action="store_true",
        help="OCR scanned pages via VL API (requires VL_API_KEY; uses token)",
    )
    p_ingest.set_defaults(func=cmd_ingest)

    p_list = sub.add_parser("list", help="list ingested documents")
    p_list.set_defaults(func=cmd_list)

    p_search = sub.add_parser("search", help="BM25 search")
    p_search.add_argument("query", help="search query string")
    p_search.add_argument("--top", type=int, default=config.DEFAULT_TOP_K,
                          help=f"number of hits (default {config.DEFAULT_TOP_K}, max {config.MAX_TOP_K})")
    p_search.add_argument("--doc", default=None,
                          help="restrict to a single source filename")
    p_search.set_defaults(func=cmd_search)

    p_search_hybrid = sub.add_parser(
        "search-hybrid", help="hybrid BM25 + embedding search (A/B test against 'search')",
    )
    p_search_hybrid.add_argument("query", help="search query string")
    p_search_hybrid.add_argument("--top", type=int, default=config.DEFAULT_TOP_K,
                                 help=f"number of hits (default {config.DEFAULT_TOP_K}, max {config.MAX_TOP_K})")
    p_search_hybrid.add_argument("--doc", default=None,
                                 help="restrict to a single source filename")
    p_search_hybrid.set_defaults(func=cmd_search_hybrid)

    p_remove = sub.add_parser("remove", help="remove a document by id or filename")
    p_remove.add_argument("target", help="document id or exact filename")
    p_remove.set_defaults(func=cmd_remove)

    p_rebuild = sub.add_parser("rebuild", help="rebuild the BM25 index from chunks")
    p_rebuild.add_argument(
        "--with-embeddings", action="store_true",
        help="also (re)compute embedding vectors for hybrid search",
    )
    p_rebuild.add_argument(
        "--drop-embeddings", action="store_true",
        help="with --with-embeddings, drop existing rows first (clean rebuild)",
    )
    p_rebuild.add_argument(
        "--rechunk", action="store_true",
        help="re-run extract + chunk on every document (applies any "
             "changes to chunks.py / pdf_extract.py / sections.py). "
             "Drops and regenerates all chunks + embeddings.",
    )
    p_rebuild.add_argument(
        "--only-ocr", action="store_true",
        help="with --rechunk: skip non-OCR documents (useful when "
             "OSG10/9-style giant PDFs block the rebuild). "
             "OCR'd docs (ocr_pages != '[]') are still re-extracted "
             "and re-chunked via the OCR callback.",
    )
    p_rebuild.add_argument(
        "--doc", default=None,
        help="with --rechunk: only process this filename (e.g. 'foo.pdf'). "
             "Use with --no-clear for restart-safe per-doc rebuilds.",
    )
    p_rebuild.add_argument(
        "--no-clear", action="store_true",
        help="with --rechunk: only delete chunks/embeddings for the docs "
             "being processed, not the whole corpus. Use with --doc for "
             "restart-safe per-doc rebuilds that survive a process crash.",
    )
    p_rebuild.set_defaults(func=cmd_rebuild)

    p_status = sub.add_parser("status", help="show KB statistics")
    p_status.set_defaults(func=cmd_status)

    p_serve = sub.add_parser("serve", help="start an IM bridge")
    serve_sub = p_serve.add_subparsers(dest="platform", required=True)
    p_serve_wecom = serve_sub.add_parser(
        "wecom",
        help="start the Enterprise WeChat (WeCom) callback server (HTTP, needs public URL)",
    )
    p_serve_wecom.set_defaults(func=cmd_serve_wecom)
    p_serve_dt = serve_sub.add_parser(
        "dingtalk",
        help="start the DingTalk Stream-mode bot (WebSocket, no public URL needed)",
    )
    p_serve_dt.set_defaults(func=cmd_serve_dingtalk)
    p_serve_fs = serve_sub.add_parser(
        "feishu",
        help="start the Feishu (Lark) webhook bot (HTTP, needs public URL or ngrok)",
    )
    p_serve_fs.set_defaults(func=cmd_serve_feishu)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())