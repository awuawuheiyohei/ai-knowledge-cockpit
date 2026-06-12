"""
review.py — Wrong-question tracking and review-progress persistence.

All data stored as JSON files under data/.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / 'data'
WRONG = DATA / 'wrong_questions.json'
PROGRESS = DATA / 'progress.json'


def _read_json(path: Path, default):
    """Read JSON with corruption recovery."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError:
        # Back up the corrupted file instead of losing it
        backup = path.with_suffix('.json.bak')
        path.rename(backup)
        print(f'Warning: {path.name} corrupted, backed up to {backup.name}', file=sys.stderr)
        return default


def _atomic_write(path: Path, content: str):
    """Write content atomically via temp file + os.replace."""
    DATA.mkdir(exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA, suffix='.tmp')
    try:
        os.write(fd, content.encode('utf-8'))
        os.close(fd)
        os.replace(tmp, path)  # atomic on POSIX
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_wrong_questions() -> list:
    return _read_json(WRONG, [])


def add_wrong_question(record: dict) -> Path:
    data = load_wrong_questions()
    data.append(record)
    _atomic_write(WRONG, json.dumps(data, ensure_ascii=False, indent=2))
    return WRONG


def load_progress() -> dict:
    """Load progress with dynamic subject discovery from notes/ directory."""
    NOTES = BASE / 'notes'
    default = {
        'subjects': {},
        'last_review': None,
    }
    progress = _read_json(PROGRESS, default)

    # Auto-discover subjects from notes/ subdirectories
    if 'subjects' not in progress:
        # Migrate old format (cissp_domains, grad_topics)
        progress['subjects'] = {}
        if NOTES.exists():
            for d in NOTES.iterdir():
                if d.is_dir():
                    progress['subjects'][d.name] = progress.get('subjects', {}).get(d.name, {})
    else:
        if NOTES.exists():
            for d in NOTES.iterdir():
                if d.is_dir() and d.name not in progress['subjects']:
                    progress['subjects'][d.name] = {}

    return progress


def save_progress(progress: dict) -> Path:
    _atomic_write(PROGRESS, json.dumps(progress, ensure_ascii=False, indent=2))
    return PROGRESS
