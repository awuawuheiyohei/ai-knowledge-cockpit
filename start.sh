#!/usr/bin/env bash
# start.sh — Run the KB app with .env automatically loaded.
#
# Usage:
#   ./start.sh serve dingtalk          # start the DingTalk bot
#   ./start.sh serve wecom             # start the WeCom bot
#   ./start.sh ingest inbox/ --ocr     # ingest PDFs with OCR
#   ./start.sh search "PKI"           # one-shot search
#
# Why: keys live in .env, but Python doesn't auto-load .env. This script
# `set -a; source .env; set +a`s the file so the app sees the variables
# without you having to remember to export them each time.
#
# Real shell exports always win over .env (the source happens first,
# then your existing env vars stay). To override a .env value, just
# export it before running this script.

set -euo pipefail

# Find project root: directory containing this script.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load .env into the current shell if present. `set -a` auto-exports
# every variable the source command defines.
if [[ -f "$ROOT/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$ROOT/.env"
    set +a
fi

# Use the project's venv python explicitly.
exec "$ROOT/.venv/bin/python" "$ROOT/app.py" "$@"