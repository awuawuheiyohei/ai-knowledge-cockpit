"""
md_extract.py — Markdown ingestion.

Markdown is already a clean source format — just read it. We treat the
whole file as a single page (page_num=None) since we don't have page
boundaries. The chunker will split it on paragraph breaks.
"""
from __future__ import annotations

from pathlib import Path


def read_markdown(file_path: str) -> tuple[str, int]:
    """
    Read a markdown file. Returns (text, char_count).

    Strips a leading UTF-8 BOM if present. Keeps the rest verbatim —
    markdown is already structured text, no need to munge it.
    """
    p = Path(file_path)
    text = p.read_text(encoding="utf-8")
    if text.startswith("\ufeff"):
        text = text[1:]
    return text, len(text)