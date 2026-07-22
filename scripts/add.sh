#!/usr/bin/env bash
# scripts/add.sh — drop a PDF / Markdown file (or folder) into the KB in one shot.
#
# Usage:
#   ./scripts/add.sh path/to/file.pdf
#   ./scripts/add.sh path/to/folder/                 # recursive
#   ./scripts/add.sh path/to/file.pdf --ocr          # force OCR (scanned PDFs; costs VL API)
#   ./scripts/add.sh path/to/file.pdf --demo         # run a top-3 search using the filename as query
#
# What it does:
#   1. Picks python (.venv first, fall back to system python3)
#   2. Inits KB if not yet initialized
#   3. Copies source into inbox/ (folder → recursive copy)
#   4. Runs `app.py ingest` (dedup by file hash, unchanged files are a no-op)
#   5. Prints a 5-line status summary
#   6. If --demo, runs a top-3 search using the basename as the query

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# ---------- arg parsing ----------
TARGET=""
DO_OCR=""
DEMO=""
for arg in "$@"; do
  case "$arg" in
    --ocr) DO_OCR="--ocr" ;;
    --demo) DEMO=1 ;;
    -h|--help)
      sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      if [ -n "$TARGET" ]; then
        echo "❌ Only one target at a time. Got: $TARGET and $arg"
        exit 1
      fi
      TARGET="$arg"
      ;;
  esac
done

if [ -z "$TARGET" ]; then
  echo "Usage: $0 <file-or-folder> [--ocr] [--demo]"
  echo "Try:  $0 --help"
  exit 1
fi

if [ ! -e "$TARGET" ]; then
  echo "❌ not found: $TARGET"
  exit 1
fi

# ---------- python ----------
if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
else
  echo "❌ no python found (need .venv/bin/python or python3 on PATH)"
  exit 1
fi

# ---------- KB init ----------
if [ ! -f "data/kb.sqlite" ]; then
  echo "📦 KB not initialized — running init..."
  "$PY" app.py init
fi

# ---------- copy + ingest ----------
if [ -d "$TARGET" ]; then
  echo "📂 Copying folder: $TARGET → inbox/ (recursive)"
  cp -R "$TARGET"/. inbox/
  echo "🔄 Ingesting inbox/ recursively..."
  "$PY" app.py ingest inbox/ --recursive $DO_OCR
elif [ -f "$TARGET" ]; then
  filename="$(basename "$TARGET")"
  if [ -e "inbox/$filename" ]; then
    echo "📄 inbox/$filename already exists — re-ingesting (dedup by hash if unchanged)"
  else
    echo "📄 Copying: $TARGET → inbox/$filename"
    cp "$TARGET" "inbox/$filename"
  fi
  echo "🔄 Ingesting: inbox/$filename"
  "$PY" app.py ingest "inbox/$filename" $DO_OCR
fi

# ---------- post status ----------
echo ""
echo "📊 KB summary:"
"$PY" app.py status 2>&1 | sed -n '1,8p' | tail -6

# ---------- optional demo ----------
if [ -n "$DEMO" ] && [ -f "$TARGET" ]; then
  # Use the first 1-2 meaningful words from the filename as the query
  query="$(basename "$TARGET" .pdf | sed -E 's/^第[0-9]+章-//; s/-知识点//' | head -c 24)"
  if [ -n "$query" ]; then
    echo ""
    echo "🔍 Demo search: \"$query\""
    "$PY" app.py search "$query" --top 3 2>&1 | head -14
  fi
fi
