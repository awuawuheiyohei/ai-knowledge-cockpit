#!/usr/bin/env bash
# scripts/rebuild.sh — rebuild the BM25 inverted index, with before/after diff.
#
# Usage:
#   ./scripts/rebuild.sh           # interactive: asks for confirmation
#   ./scripts/rebuild.sh --yes     # skip confirmation (CI / scripted use)
#   ./scripts/rebuild.sh --dry-run # show what would change, don't run rebuild
#
# What it does:
#   1. Reads `app.py status` (chunks + document count).
#   2. Asks for confirmation (unless --yes / --dry-run).
#   3. Runs `app.py rebuild` — destructive: wipes the BM25 inverted index
#      and rebuilds it from the existing chunks table. Documents and
#      original files are NOT touched.
#   4. Reads `app.py status` again, shows the diff.
#
# When to use this:
#   - You changed config.py (CHUNK_SIZE, BM25_K1, BM25_B, bigram toggle).
#   - You suspect index drift after manual SQL edits.
#   - After upgrading bm25.py / storage.py with a schema change.
#   - The status output looks wrong (e.g., a search returns 0 hits but the
#     chunk count says 20000).

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# ---------- python ----------
if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
else
  echo "❌ no python found (need .venv/bin/python or python3 on PATH)"
  exit 1
fi

SKIP_CONFIRM=""
DRY_RUN=""
for arg in "$@"; do
  case "$arg" in
    --yes|-y) SKIP_CONFIRM=1 ;;
    --dry-run) DRY_RUN=1; SKIP_CONFIRM=1 ;;
    -h|--help)
      sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
  esac
done

# ---------- before ----------
echo "📊 Before rebuild:"
BEFORE=$("$PY" app.py status 2>&1 | awk -F'[: ]+' '/^Chunks/ {print $3; exit}')
echo "  chunks: ${BEFORE:-?}"
echo "  (documents and original files are NOT touched — only the inverted index)"

if [ -n "$DRY_RUN" ]; then
  echo ""
  echo "🔍 --dry-run: would run: $PY app.py rebuild"
  echo "Aborted (no changes made)."
  exit 0
fi

# ---------- confirm ----------
if [ -z "$SKIP_CONFIRM" ]; then
  echo ""
  read -rp "Proceed with rebuild? [y/N] " ans
  case "$ans" in
    y|Y|yes|YES) ;;
    *) echo "Aborted."; exit 0 ;;
  esac
fi

# ---------- rebuild ----------
echo ""
echo "🔄 Running app.py rebuild..."
"$PY" app.py rebuild

# ---------- after ----------
echo ""
echo "📊 After rebuild:"
AFTER=$("$PY" app.py status 2>&1 | awk -F'[: ]+' '/^Chunks/ {print $3; exit}')
echo "  chunks: ${AFTER:-?}"

if [ -n "$BEFORE" ] && [ -n "$AFTER" ] && [ "$BEFORE" = "$AFTER" ]; then
  echo "  ✅ Index chunk count matches — no drift detected"
elif [ -n "$BEFORE" ] && [ -n "$AFTER" ] && [ "$BEFORE" != "$AFTER" ]; then
  echo "  ⚠️  Chunk count changed: $BEFORE → $AFTER (expected if config.py changed)"
fi
