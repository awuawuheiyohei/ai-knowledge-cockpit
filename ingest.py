"""
ingest.py — Pull source files into the knowledge base.

Pipeline for one file:
  1. Detect type (PDF vs Markdown) by extension.
  2. Hash the file to dedupe.
  3. Extract text (PDF: per-page with scan detection; MD: whole file).
  4. If --ocr is set and the PDF has scanned pages, render each to PNG
     and call a VL model (default MiniMax-M3) to OCR them.
  5. Chunk the text (paragraph-aware, overlap).
  6. Persist document + chunks to SQLite (each chunk carries a via_ocr flag).
  7. Update BM25 inverted index.
  8. Print a summary — including scan-page warnings + OCR usage — never drop.

No LLM in the default path. LLM is used only for OCR fallback, and only
when --ocr is passed.
"""
from __future__ import annotations

import hashlib
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import config
import paths
import storage
import chunks as chunker
import bm25
import pdf_extract
import md_extract
import sections
import pdf_ocr
import vl_config


# Extensions we accept. Lowercase, with leading dot.
ACCEPTED_EXTS = {".pdf", ".md", ".markdown"}


@dataclass
class IngestSummary:
    file_path: str
    filename: str
    status: str             # 'ok' | 'partial' | 'failed' | 'duplicate'
    doc_id: int | None
    page_count: int | None
    chunk_count: int
    char_count: int
    scan_pages: list[int]
    ocr_pages: list[int] = None  # type: ignore[assignment]
    ocr_failed: list[int] = None  # type: ignore[assignment]
    message: str = ""


def _embed_chunks_for_doc(doc_id: int) -> int:
    """
    Compute and store embedding vectors for all chunks of a single doc.

    Best-effort: if the embedding model can't be loaded (network
    issue, broken install), we log and return 0 — the chunks are
    still searchable via BM25, just not via hybrid search.

    Returns the number of embeddings written.
    """
    import embedding
    import config

    if not embedding.is_available():
        return 0
    # Fetch the (chunk_id, chunk_text) pairs for this doc, skipping
    # any that already have an embedding (idempotent re-ingest).
    pending = [
        (cid, txt) for cid, txt in storage.get_chunks_needing_embeddings_for_doc(
            config.EMBEDDING_MODEL, doc_id,
        )
    ]
    if not pending:
        return 0
    texts = [txt for _, txt in pending]
    vectors = embedding.embed_texts(texts, batch_size=64)
    if vectors is None or len(vectors) == 0:
        return 0
    rows = [
        (cid, doc_id, config.EMBEDDING_MODEL, vectors.shape[1], vectors[i].tobytes())
        for i, (cid, _txt) in enumerate(pending)
    ]
    storage.insert_embeddings(rows)
    return len(rows)


def _file_hash(path: Path) -> str:
    """SHA1 of file bytes — fast dedupe key."""
    h = hashlib.sha1()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _mirror_original(src: Path) -> str:
    """Copy the source file into data/originals/. Returns the relative path."""
    paths.ensure_dirs()
    dest_dir = paths.ORIGINALS / src.stem
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    shutil.copy2(src, dest)
    return str(dest.relative_to(paths.BASE))


def _ingest_pdf(file_path: Path, use_ocr: bool = False) -> IngestSummary:
    rel = _mirror_original(file_path)
    file_hash = _file_hash(file_path)
    existing = storage.get_document_by_hash(file_hash)
    if existing:
        return IngestSummary(
            file_path=str(file_path),
            filename=file_path.name,
            status="duplicate",
            doc_id=existing["id"],
            page_count=existing["page_count"],
            chunk_count=0,
            char_count=existing["char_count"],
            scan_pages=[],
            ocr_pages=[],
            ocr_failed=[],
            message=f"Already ingested as doc_id={existing['id']}",
        )

    # Decide on OCR callback based on --ocr flag.
    ocr_callback = None
    ocr_usage = pdf_ocr.OcrUsage()
    if use_ocr:
        try:
            cfg = vl_config.load_vl_config()
        except ValueError as e:
            return IngestSummary(
                file_path=str(file_path),
                filename=file_path.name,
                status="failed",
                doc_id=None,
                page_count=None,
                chunk_count=0,
                char_count=0,
                scan_pages=[],
                ocr_pages=[],
                ocr_failed=[],
                message=f"--ocr requested but VL not configured: {e}",
            )

        def _cb(page, page_num):
            return pdf_ocr.ocr_page(page, page_num, cfg, ocr_usage)

        ocr_callback = _cb

    try:
        result = pdf_extract.extract_pdf(str(file_path), ocr_callback=ocr_callback)
    except Exception as e:
        return IngestSummary(
            file_path=str(file_path),
            filename=file_path.name,
            status="failed",
            doc_id=None,
            page_count=None,
            chunk_count=0,
            char_count=0,
            scan_pages=[],
            ocr_pages=[],
            ocr_failed=[],
            message=f"PDF extraction failed: {e}",
        )

    # Determine overall status.
    non_scan_pages = [p for p in result.pages if not p.is_scanned]
    if not non_scan_pages:
        status = "failed"
    elif result.scan_page_nums:
        status = "partial"
    else:
        status = "ok"

    # Build chunks, attaching page_num + via_ocr per chunk.
    # Day 2 (2026-08-06): when the PDF has a usable outline, we chunk
    # section-by-section and prefix each chunk with its section heading
    # so the chapter title becomes part of the indexable vocabulary.
    # This dramatically improves "what does chapter X cover" queries
    # that previously couldn't match because the chunk was a paragraph
    # deep in the chapter (with the title nowhere nearby).
    chunk_records: list[dict] = []
    chunk_texts: list[str] = []
    char_count_total = 0
    next_idx = 0

    pdf_sections = sections.parse_pdf_sections(str(file_path))
    if len(pdf_sections) == 1 and not pdf_sections[0].heading:
        # No outline (or empty outline) — fall back to plain per-page
        # chunking, exactly as before. No behavior change for these PDFs.
        for page in result.pages:
            if page.is_scanned:
                continue
            text = page.text
            if not text.strip():
                continue
            char_count_total += len(text)
            for piece in chunker.chunk_text(text):
                chunk_records.append(
                    {
                        "chunk_index": next_idx,
                        "page_num": page.page_num,
                        "chunk_text": piece,
                        "via_ocr": page.via_ocr,
                    }
                )
                chunk_texts.append(piece)
                next_idx += 1
    else:
        # Sectioned PDF — group pages by outline entry.
        page_by_num = {p.page_num: p for p in result.pages}
        last_page = max(page_by_num.keys()) if page_by_num else 0
        for i, sec in enumerate(pdf_sections):
            start = sec.page_num or 1
            if i + 1 < len(pdf_sections):
                next_start = pdf_sections[i + 1].page_num or (start + 1)
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
                    chunk_records.append(
                        {
                            "chunk_index": next_idx,
                            "page_num": pn,
                            "chunk_text": piece_with_ctx,
                            "via_ocr": page.via_ocr,
                        }
                    )
                    chunk_texts.append(piece_with_ctx)
                    next_idx += 1

    if status == "failed" or not chunk_records:
        doc_id = storage.add_document(
            filename=file_path.name,
            source_type="pdf",
            relative_path=rel,
            file_hash=file_hash,
            page_count=result.page_count,
            char_count=char_count_total,
            status="failed",
            scan_pages=result.scan_page_nums,
            ocr_pages=result.ocr_page_nums or None,
        )
        return IngestSummary(
            file_path=str(file_path),
            filename=file_path.name,
            status="failed",
            doc_id=doc_id,
            page_count=result.page_count,
            chunk_count=0,
            char_count=char_count_total,
            scan_pages=result.scan_page_nums,
            ocr_pages=list(result.ocr_page_nums),
            ocr_failed=list(ocr_usage.failed_page_nums),
            message="PDF has no extractable text"
            + (" (OCR failed)" if use_ocr and ocr_usage.pages_failed else " — looks like a scanned document."),
        )

    doc_id = storage.add_document(
        filename=file_path.name,
        source_type="pdf",
        relative_path=rel,
        file_hash=file_hash,
        page_count=result.page_count,
        char_count=char_count_total,
        status=status,
        scan_pages=result.scan_page_nums or None,
        ocr_pages=result.ocr_page_nums or None,
    )

    storage.insert_chunks(doc_id, chunk_records)
    bm25.index_document(doc_id, chunk_texts)

    # Day 6 (2026-08-06): also compute embedding vectors for the new
    # chunks. Best-effort — if the model can't load (network / install
    # issue) we just log and move on; search.hybrid_search will fall
    # back to BM25 for these chunks.
    _embed_chunks_for_doc(doc_id)

    return IngestSummary(
        file_path=str(file_path),
        filename=file_path.name,
        status=status,
        doc_id=doc_id,
        page_count=result.page_count,
        chunk_count=len(chunk_records),
        char_count=char_count_total,
        scan_pages=list(result.scan_page_nums),
        ocr_pages=list(result.ocr_page_nums),
        ocr_failed=list(ocr_usage.failed_page_nums),
    )


def _ingest_markdown(file_path: Path) -> IngestSummary:
    rel = _mirror_original(file_path)
    file_hash = _file_hash(file_path)
    existing = storage.get_document_by_hash(file_hash)
    if existing:
        return IngestSummary(
            file_path=str(file_path),
            filename=file_path.name,
            status="duplicate",
            doc_id=existing["id"],
            page_count=None,
            chunk_count=0,
            char_count=existing["char_count"],
            scan_pages=[],
            message=f"Already ingested as doc_id={existing['id']}",
        )

    try:
        text, char_count = md_extract.read_markdown(str(file_path))
    except Exception as e:
        return IngestSummary(
            file_path=str(file_path),
            filename=file_path.name,
            status="failed",
            doc_id=None,
            page_count=None,
            chunk_count=0,
            char_count=0,
            scan_pages=[],
            message=f"Markdown read failed: {e}",
        )

    # Day 2 (2026-08-06): section-aware chunking. Each markdown
    # section's chunks get the section heading prefixed so BM25 can
    # match "what does <section name> cover" queries even when the
    # chunk's body text doesn't include the heading.
    chunk_records: list[dict] = []
    chunk_texts: list[str] = []
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

    doc_id = storage.add_document(
        filename=file_path.name,
        source_type="markdown",
        relative_path=rel,
        file_hash=file_hash,
        page_count=None,
        char_count=char_count,
        status="ok",
        scan_pages=None,
    )
    storage.insert_chunks(doc_id, chunk_records)
    bm25.index_document(doc_id, chunk_texts)

    # Day 6: also compute embedding vectors for the new chunks.
    _embed_chunks_for_doc(doc_id)

    return IngestSummary(
        file_path=str(file_path),
        filename=file_path.name,
        status="ok",
        doc_id=doc_id,
        page_count=None,
        chunk_count=len(chunk_records),
        char_count=char_count,
        scan_pages=[],
    )


def ingest_file(file_path: str | Path, use_ocr: bool = False) -> IngestSummary:
    """Ingest a single file. Auto-detect type by extension."""
    p = Path(file_path)
    if not p.exists():
        return IngestSummary(
            file_path=str(p),
            filename=p.name,
            status="failed",
            doc_id=None,
            page_count=None,
            chunk_count=0,
            char_count=0,
            scan_pages=[],
            ocr_pages=[],
            ocr_failed=[],
            message=f"File not found: {p}",
        )
    ext = p.suffix.lower()
    if ext == ".pdf":
        return _ingest_pdf(p, use_ocr=use_ocr)
    if ext in (".md", ".markdown"):
        return _ingest_markdown(p)
    return IngestSummary(
        file_path=str(p),
        filename=p.name,
        status="failed",
        doc_id=None,
        page_count=None,
        chunk_count=0,
        char_count=0,
        scan_pages=[],
        ocr_pages=[],
        ocr_failed=[],
        message=f"Unsupported extension: {ext} (supported: .pdf, .md, .markdown)",
    )


def ingest_path(target: str | Path, recursive: bool = False, use_ocr: bool = False) -> list[IngestSummary]:
    """
    Ingest a file OR a directory. For directories, walks (optionally recursively)
    and ingests every PDF/Markdown found.
    """
    paths.ensure_dirs()
    storage.init_db()
    p = Path(target)
    summaries: list[IngestSummary] = []

    if p.is_file():
        summaries.append(ingest_file(p, use_ocr=use_ocr))
        return summaries

    if not p.is_dir():
        return [
            IngestSummary(
                file_path=str(p),
                filename=p.name,
                status="failed",
                doc_id=None,
                page_count=None,
                chunk_count=0,
                char_count=0,
                scan_pages=[],
                ocr_pages=[],
                ocr_failed=[],
                message=f"Not a file or directory: {p}",
            )
        ]

    pattern = "**/*" if recursive else "*"
    for child in sorted(p.glob(pattern)):
        if not child.is_file():
            continue
        if child.suffix.lower() not in ACCEPTED_EXTS:
            continue
        summaries.append(ingest_file(child, use_ocr=use_ocr))
    return summaries


def print_summaries(summaries: list[IngestSummary], stream=sys.stdout) -> None:
    """Print a compact report. No ANSI color so it's friendly to logs."""
    if not summaries:
        print("(nothing to ingest)", file=stream)
        return
    for s in summaries:
        line = f"[{s.status:9s}] {s.filename}"
        line += f"  pages={s.page_count}" if s.page_count is not None else "  pages=-"
        line += f"  chunks={s.chunk_count}  chars={s.char_count}"
        if s.ocr_pages:
            line += f"  OCR={len(s.ocr_pages)}p"
        if s.status == "duplicate":
            line += f"  ({s.message})"
        elif s.status == "failed":
            line += f"  ({s.message})"
        print(line, file=stream)
        if s.scan_pages:
            print(
                f"           ⚠ scanned-looking pages (no text): {s.scan_pages}",
                file=stream,
            )
            if s.ocr_failed:
                print(
                    f"           ⚠ OCR failed on pages: {s.ocr_failed}",
                    file=stream,
                )
            print(
                "             Re-run with `--ocr` to OCR these pages (VL API key required).",
                file=stream,
            )