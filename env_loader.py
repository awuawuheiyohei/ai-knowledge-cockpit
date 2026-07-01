"""
env_loader.py — Minimal .env loader.

We don't depend on python-dotenv. .env files are 99% simple KEY=VALUE lines,
and a 20-line reader covers all real-world cases:

- Lines starting with `#` are comments.
- Blank lines are skipped.
- Each `KEY=VALUE` becomes `os.environ[KEY] = VALUE` (only if KEY is not
  already set in the real environment — shell exports win).
- Values may be wrapped in single or double quotes; quotes are stripped.
- No support for multi-line values, command substitution, or `export` prefix.

This is intentionally a soft loader: missing file → 0 vars loaded, no error.
That way a fresh checkout still works with explicit `KEY=value python app.py ...`.
"""
from __future__ import annotations

import os
from pathlib import Path


DEFAULT_ENV_PATH = Path(__file__).resolve().parent / ".env"


def load_dotenv(env_path: Path | str | None = None) -> int:
    """
    Load KEY=VALUE pairs from a .env file into `os.environ`.

    Existing real-environment values are NOT overwritten, so a shell
    `export FOO=bar` always wins over the .env file.

    Returns the number of variables newly loaded.
    """
    path = Path(env_path) if env_path is not None else DEFAULT_ENV_PATH
    if not path.exists():
        return 0

    count = 0
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        # Strip optional `export ` prefix users sometimes add.
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        # Strip surrounding quotes, single or double.
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if not key:
            continue
        if key not in os.environ:
            os.environ[key] = val
            count += 1
    return count