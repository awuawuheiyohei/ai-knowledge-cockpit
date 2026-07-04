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
    """
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

    # Re-index every document.
    docs = storage.list_documents()
    rebuilt = 0
    for d in docs:
        texts = storage.get_chunk_texts(d["id"])
        if texts:
            bm25.index_document(d["id"], texts)
            rebuilt += 1
    print(f"Rebuilt index for {rebuilt}/{len(docs)} document(s).")
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

    p_remove = sub.add_parser("remove", help="remove a document by id or filename")
    p_remove.add_argument("target", help="document id or exact filename")
    p_remove.set_defaults(func=cmd_remove)

    p_rebuild = sub.add_parser("rebuild", help="rebuild the BM25 index from chunks")
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

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())