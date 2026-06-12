"""
knowledge.py — Note loading, storage, and file utilities.

Handles reading/writing Markdown notes under notes/ and
generic output files under data/.
"""

import json
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent
NOTES = BASE / 'notes'
DATA = BASE / 'data'


def slugify(name: str) -> str:
    """Convert a string to a filesystem-safe slug."""
    name = str(name).strip().lower()
    name = re.sub(r'[^\w\-]+', '-', name, flags=re.UNICODE)
    name = re.sub(r'-+', '-', name).strip('-')
    return name or 'untitled'


def load_note(path: str) -> str:
    """Read a note file. Only allows paths inside the notes/ directory."""
    # Resolve to absolute path
    p = (NOTES / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    notes_root = NOTES.resolve()

    # Security: prevent path traversal
    if not str(p).startswith(str(notes_root)):
        raise ValueError(f'Path outside notes directory: {path}')

    if not p.exists():
        raise FileNotFoundError(f'Note not found: {path}')

    return p.read_text(encoding='utf-8')


def list_notes() -> list[str]:
    """List all .md files under notes/ relative to project root."""
    result = []
    for p in NOTES.rglob('*.md'):
        if p.name == '.gitkeep':
            continue
        result.append(str(p.relative_to(BASE)))
    return sorted(result)


def save_output(name: str, content: str) -> Path:
    """Save a generated output file to data/ (with timestamped archive)."""
    DATA.mkdir(exist_ok=True)

    # Archive a timestamped copy
    from datetime import datetime
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    stem, suffix = Path(name).stem, Path(name).suffix
    archived = DATA / f'{stem}_{ts}{suffix}'
    archived.write_text(content, encoding='utf-8')

    # Also write the latest version
    p = DATA / name
    p.write_text(content, encoding='utf-8')
    return p


def save_json(name: str, obj) -> Path:
    """Save a JSON object to data/."""
    DATA.mkdir(exist_ok=True)
    p = DATA / name
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
    return p


def save_note(subject: str, topic: str, content: str) -> Path:
    """Save a note under notes/{subject}/{topic}.md."""
    subject_dir = NOTES / slugify(subject)
    subject_dir.mkdir(parents=True, exist_ok=True)
    p = subject_dir / f'{slugify(topic)}.md'
    p.write_text(content, encoding='utf-8')
    return p


def load_input_file(path: str) -> str:
    """Read a raw input file (any path, e.g. extracted PDF text)."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f'Input not found: {path}')
    return p.read_text(encoding='utf-8')


def chunk_text(text: str, max_chars: int = 6000) -> list[str]:
    """Split text into chunks at paragraph boundaries for LLM refinement."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for para in text.split('\n\n'):
        para_len = len(para) + 2
        if current and current_len + para_len > max_chars:
            chunks.append('\n\n'.join(current))
            current = [para]
            current_len = para_len
        elif len(para) > max_chars:
            if current:
                chunks.append('\n\n'.join(current))
                current = []
                current_len = 0
            for i in range(0, len(para), max_chars):
                chunks.append(para[i:i + max_chars])
        else:
            current.append(para)
            current_len += para_len

    if current:
        chunks.append('\n\n'.join(current))
    return chunks


def save_note_file(name: str, content: str) -> Path:
    """Save directly under notes/{name}. Path must stay inside notes/."""
    NOTES.mkdir(exist_ok=True)
    p = (NOTES / name).resolve()
    notes_root = NOTES.resolve()
    if not str(p).startswith(str(notes_root)):
        raise ValueError(f'Path outside notes directory: {name}')
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding='utf-8')
    return p
