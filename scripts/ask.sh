#!/usr/bin/env bash
# scripts/ask.sh — search the KB with optional LLM query rewriting.
#
# Usage:
#   ./scripts/ask.sh "<query>"
#   ./scripts/ask.sh "<query>" --top 5
#   ./scripts/ask.sh "<query>" --doc "filename.pdf"
#   ./scripts/ask.sh "什么是 BIA 业务影响分析" --rewrite   # force LLM keyword rewrite (costs VL API)
#
# What it does:
#   - Default: passes through to `app.py search` (BM25 only, no LLM).
#   - --rewrite: first calls query_rewrite.rewrite() to turn the colloquial
#     query into keyword form, then re-runs BM25 with those keywords.
#     The LLM only sees the raw query string (never the KB) and only
#     outputs 3-7 keywords, not an answer.
#
# Requires: .venv with anthropic installed (for --rewrite); VL_API_KEY in .env.

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

# ---------- arg parsing ----------
QUERY=""
REWRITE=""
PASSTHRU=()
for arg in "$@"; do
  case "$arg" in
    --rewrite|-r) REWRITE=1 ;;
    -h|--help)
      sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      # First non-flag arg = query, the rest are passed to app.py search
      if [ -z "$QUERY" ]; then
        QUERY="$arg"
      else
        PASSTHRU+=("$arg")
      fi
      ;;
  esac
done

if [ -z "$QUERY" ]; then
  echo "Usage: $0 <query> [--rewrite] [--top N] [--doc filename]"
  echo "Try:  $0 --help"
  exit 1
fi

# ---------- run ----------
if [ -n "$REWRITE" ]; then
  if [ ! -f ".env" ] && [ -z "${VL_API_KEY:-}${LLM_API_KEY:-}" ]; then
    echo "❌ --rewrite needs VL_API_KEY (or LLM_API_KEY) in .env"
    exit 1
  fi
  echo "🤖 Rewriting query via LLM (uses VL_API_KEY tokens)..."
  # Use python heredoc to call rewrite() and pull .rewritten out.
  # The function never raises — on failure it returns used_rewrite=False with
  # the original query intact, so we just fall through.
  REWRITTEN=$("$PY" - "$QUERY" <<'PYEOF' 2>/dev/null
import sys
from query_rewrite import rewrite
r = rewrite(sys.argv[1])
print(r.rewritten)
if not r.used_rewrite and r.error:
    print(f"  (rewrite skipped: {r.error})", file=sys.stderr)
PYEOF
  ) || REWRITTEN=""
  if [ -z "$REWRITTEN" ]; then
    echo "❌ rewrite failed, falling back to raw query"
    REWRITTEN="$QUERY"
  else
    echo "🔄 Rewritten: $REWRITTEN"
  fi
  echo ""
  "$PY" app.py search "$REWRITTEN" "${PASSTHRU[@]}"
else
  "$PY" app.py search "$QUERY" "${PASSTHRU[@]}"
fi
