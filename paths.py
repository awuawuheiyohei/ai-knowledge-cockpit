"""
paths.py — Centralized filesystem paths.

All other modules import paths from here so the layout is in one place.
"""
from pathlib import Path

BASE = Path(__file__).resolve().parent

# User uploads — drop PDF/MD files here, then run `ingest`
INBOX = BASE / "inbox"

# Knowledge base SQLite database
DATA = BASE / "data"
DB_PATH = DATA / "kb.sqlite"

# Originals mirror — copies of uploaded files kept verbatim for audit
ORIGINALS = DATA / "originals"


def ensure_dirs() -> None:
    """Create all required directories if they don't exist."""
    for d in (INBOX, DATA, ORIGINALS):
        d.mkdir(parents=True, exist_ok=True)