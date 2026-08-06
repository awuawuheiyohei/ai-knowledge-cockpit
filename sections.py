"""
sections.py — Parse source documents into (section_heading, text) pairs.

The point of preserving section context (Day 2, 2026-08-06):
When a user asks "安全架构讲什么", BM25 has to match that query to a
chunk that contains the *words* "安全架构". If the chunk's text is a
paragraph deep in that chapter, it might not contain the chapter title
(especially after chunking cuts it). By prefixing each chunk with its
section heading, the chapter title becomes part of the chunk's
indexable vocabulary — boosting recall for "what does chapter X
cover" questions dramatically.

Why not store section separately and index it as a second field?
- That would let us boost by section, but it doubles the schema
  complexity and forces every caller to think about section vs body.
- Just prefixing into chunk_text keeps BM25 simple: one index, one
  score, no special-case. The trade-off is a small per-chunk size
  overhead (~20-80 chars for the heading line), which is fine within
  our CHUNK_SIZE=400 budget.

Two input formats:

- Markdown: split on `# / ## / ### / ...` headers. A section is
  everything from one header down to the next header of equal or
  higher level. Pre-amble (text before the first header) is its own
  anonymous section.

- PDF: use the document's outline / bookmarks via `fitz.get_toc()`.
  If the PDF has no outline, returns a single anonymous section
  covering all pages — chunking proceeds exactly as before, no
  prefix injected.

Each Section carries `page_num` for PDFs (1-indexed, the start page
of this outline entry) and `None` for markdown. This is propagated
to chunks so the "p.N" shown in bot replies still matches the page
the content is actually on.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


_MD_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


@dataclass
class Section:
    """One logical section of a document."""
    heading: str            # e.g. "第二章 安全架构" or "2.1 对称加密"; "" for preamble
    level: int              # 1 for "#", 2 for "##", etc. (markdown) — outline level for PDF
    text: str               # the body text under this section
    page_num: int | None    # 1-indexed for PDF; None for markdown


def parse_markdown_sections(md_text: str) -> list[Section]:
    """
    Split a markdown document into sections by ATX header level.

    The result preserves order: Section 0 is everything before the
    first header (preamble), then one Section per header. The preamble
    gets heading="" so the chunker knows not to add a prefix.

    Setext-style headers (=== / ---) are NOT supported — our notes
    consistently use ATX (#) headers, and supporting both would
    double the regex complexity for marginal value.

    Headers inside code blocks (indented 4+ spaces or fenced ```) are
    ignored. We don't currently detect fenced code blocks (a more
    involved parser), but the practical impact is near-zero — code
    blocks rarely contain `# something` patterns that look like real
    headers at the start of a line.
    """
    if not md_text or not md_text.strip():
        return []

    matches = list(_MD_HEADER_RE.finditer(md_text))
    if not matches:
        # No headers — whole doc is one anonymous section.
        return [Section(heading="", level=0, text=md_text.strip(), page_num=None)]

    sections: list[Section] = []

    # Preamble: text before the first header.
    preamble = md_text[: matches[0].start()].strip()
    if preamble:
        sections.append(Section(heading="", level=0, text=preamble, page_num=None))

    for i, m in enumerate(matches):
        level = len(m.group(1))
        heading = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md_text)
        body = md_text[start:end].strip()
        if body:
            sections.append(Section(
                heading=heading, level=level, text=body, page_num=None,
            ))

    return sections


def parse_pdf_sections(pdf_path: str) -> list[Section]:
    """
    Split a PDF into sections using its outline (bookmarks).

    If the PDF has no outline (many academic / scan PDFs don't), this
    returns a single anonymous section covering all pages — chunking
    proceeds exactly as before, no prefix injected, no behavior
    change. That's the safe default: a section prefix with a wrong
    heading would be worse than no prefix at all.

    Each outline entry is followed by the next entry at the same or
    higher level. Text is the concatenation of pages spanned by the
    entry. Each section carries the start page so chunks from that
    section get the right `page_num` in the bot reply.

    Lazy import of fitz so this module is importable in markdown-only
    contexts (e.g. the qa test runner never needs pymupdf).
    """
    try:
        import fitz  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "pymupdf is required to parse PDF sections. "
            "Install with: pip install pymupdf"
        ) from e

    doc = fitz.open(pdf_path)
    try:
        toc = doc.get_toc()  # list of [level, title, page_1indexed]
        if not toc:
            # No outline — single anonymous section, no prefix.
            all_text = "\n\n".join(
                (p.get_text("text") or "").strip() for p in doc
            ).strip()
            return [Section(
                heading="", level=0, text=all_text, page_num=None,
            )]

        sections: list[Section] = []
        for i, (level, title, start_page) in enumerate(toc):
            if i + 1 < len(toc):
                end_page = toc[i + 1][2] - 1
            else:
                end_page = doc.page_count

            page_texts: list[str] = []
            for pn in range(start_page - 1, end_page):
                if 0 <= pn < doc.page_count:
                    t = (doc[pn].get_text("text") or "").strip()
                    if t:
                        page_texts.append(t)
            body = "\n\n".join(page_texts).strip()
            if body:
                sections.append(Section(
                    heading=title, level=level, text=body, page_num=start_page,
                ))

        if not sections:
            # Outline exists but no section produced text (e.g. all
            # pages are scanned and the OCR didn't run). Fall back.
            all_text = "\n\n".join(
                (p.get_text("text") or "").strip() for p in doc
            ).strip()
            return [Section(
                heading="", level=0, text=all_text, page_num=None,
            )]

        return sections
    finally:
        doc.close()


def prefix_chunk(chunk_text: str, heading: str) -> str:
    """
    Prepend a section heading to a chunk's text.

    If `heading` is empty, returns chunk_text unchanged. Otherwise
    prepends a "章节: <heading>\n\n" line so BM25 indexes the heading
    as part of this chunk.

    The prefix is intentionally short (heading only, no numbering or
    decoration) so it doesn't bloat each chunk's size budget too much.
    """
    if not heading:
        return chunk_text
    # Sanitize: strip any embedded newlines so the prefix stays on one
    # line (the chunker joins with "\n\n" and we want to keep the
    # structure predictable for the human reader too).
    safe_heading = " ".join(heading.split())
    return f"章节: {safe_heading}\n\n{chunk_text}"
