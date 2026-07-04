#!/usr/bin/env bash
# batch_ocr_inbox.sh — OCR all scanned PDFs in inbox/ that haven't been OCR'd yet.
#
# Usage:
#   ./tools/batch_ocr_inbox.sh           # OCR every failed PDF in inbox/
#   ./tools/batch_ocr_inbox.sh --dry-run # just list what would be OCR'd + estimated cost
#   ./tools/batch_ocr_inbox.sh --only "域1：安全与风险管理.pdf"  # just one file
#
# Cost note
# ---------
# OCR charges the VL model per page. At ~¥0.01-0.03/page (image input + text output),
# 12 failed PDFs × ~83 pages avg ≈ 999 pages ≈ ¥10-30 per full run.
# Run --dry-run first to see what you'd spend.
#
# Requires:
#   - .env with VL_API_KEY set
#   - .venv activated (or python3 with all deps)
#   - KB initialized (python3 app.py status works)

set -euo pipefail

# Resolve repo root (parent of tools/).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# Activate venv if it exists.
if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

# Load .env into current shell so VL_API_KEY is visible to python.
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

DRY_RUN=0
ONLY_FILE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --only)    ONLY_FILE="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,20p' "$0"
      exit 0
      ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Find candidates: failed PDFs already in DB, or all PDFs in inbox if not in DB.
PY_GET_CANDIDATES='
import sys, fitz
from pathlib import Path
import storage
storage.init_db()
inbox = Path("inbox")
# All PDFs in inbox.
pdfs = sorted(p.name for p in inbox.glob("*.pdf"))
candidates = []
for name in pdfs:
    p = inbox / name
    d = fitz.open(p)
    total_chars = sum(len(pg.get_text("text").strip()) for pg in d)
    n_pages = len(d)
    d.close()
    # No text at all -> definitely needs OCR.
    needs_ocr = total_chars < 30 * n_pages
    candidates.append((name, n_pages, total_chars, needs_ocr))
# Print as TSV: name\tpages\tchars\tneeds_ocr
for name, n, c, o in candidates:
    print(f"{name}\t{n}\t{c}\t{int(o)}")
'

echo "==> Scanning inbox/ for failed PDFs..."

# Filter to those needing OCR (or match --only).
TARGETS=()
TOTAL_PAGES=0
echo
printf "%-40s %6s %8s  %s\n" "filename" "pages" "chars" "needs_ocr"
printf "%-40s %6s %8s  %s\n" "----------------------------------------" "------" "--------" "----------"
while IFS=$'\t' read -r name pages chars needs; do
  [[ -z "$name" ]] && continue
  if [[ -n "$ONLY_FILE" && "$name" != "$ONLY_FILE" ]]; then
    continue
  fi
  printf "%-40s %6d %8d  %s\n" "$name" "$pages" "$chars" "$needs"
  if [[ "$needs" == "1" ]]; then
    TARGETS+=("$name")
    TOTAL_PAGES=$((TOTAL_PAGES + pages))
  fi
done < <(python3 -c "$PY_GET_CANDIDATES")

echo
echo "Summary:"
echo "  ${#TARGETS[@]} PDF(s) need OCR, $TOTAL_PAGES total pages"
echo "  Estimated cost: ~¥$(awk "BEGIN { printf \"%.0f\", $TOTAL_PAGES * 0.02 }") (rough; depends on actual VL pricing)"

if [[ "$DRY_RUN" == "1" ]]; then
  echo
  echo "--dry-run: not running OCR. Remove --dry-run to execute."
  exit 0
fi

if [[ ${#TARGETS[@]} -eq 0 ]]; then
  echo "Nothing to do. ✅"
  exit 0
fi

echo
echo "Press Enter to start, or Ctrl-C to abort."
read -r _

# Run ingest --ocr on each. Sequential — VL API has its own rate limits.
FAILED=()
for name in "${TARGETS[@]}"; do
  echo
  echo "==> OCR'ing: $name"
  if ! python3 app.py ingest "inbox/$name" --ocr; then
    FAILED+=("$name")
    echo "  ⚠ failed: $name"
  fi
done

echo
echo "==> Done."
if [[ ${#FAILED[@]} -gt 0 ]]; then
  echo "  Failed (${#FAILED[@]}):"
  for f in "${FAILED[@]}"; do echo "    - $f"; done
  exit 1
fi
echo "  All ${#TARGETS[@]} PDF(s) OCR'd successfully."