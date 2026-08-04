#!/usr/bin/env bash
# scripts/backup.sh — snapshot data/ to a timestamped folder.
#
# Why this exists
# ---------------
# data/kb.sqlite (172 MB) + data/originals/ (~same) are in .gitignore
# and are NOT backed up anywhere. If the disk dies, or someone
# accidentally `rm -rf` mass/, the entire KB is gone — and re-ingesting
# 20k chunks from a missing inbox/ is impossible.
#
# Usage
# -----
#   ./scripts/backup.sh                       # default: ~/Downloads/kb_backups/<timestamp>/
#   ./scripts/backup.sh --to /path/           # custom destination
#   ./scripts/backup.sh --list                 # show existing backups
#
# To auto-backup daily, add to crontab (run `crontab -e`):
#   0 3 * * * /path/to/ai_knowledge_cockpit/scripts/backup.sh --to /Volumes/External/kb_backups
#
# The backup is just a copy (no compression, no dedup). ~180 MB per
# snapshot. Adjust retention by deleting old timestamps from
# ~/Downloads/kb_backups/ manually, or wrap with a `find -mtime +30 -delete`.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

DEFAULT_TO="$HOME/Downloads/kb_backups"
DEST="$DEFAULT_TO"
ACTION="backup"

# --- color helpers (TTY only) ---
if [[ -t 1 ]]; then
    C_OK="\033[32m"; C_ERR="\033[31m"; C_DIM="\033[2m"; C_RST="\033[0m"
else
    C_OK=""; C_ERR=""; C_DIM=""; C_RST=""
fi
ok()  { echo -e "${C_OK}  ✓${C_RST} $*"; }
err() { echo -e "${C_ERR}  ✗${C_RST} $*" >&2; }
dim() { echo -e "${C_DIM}    $*${C_RST}"; }

# --- arg parsing ---
for arg in "$@"; do
    case "$arg" in
        --to)        DEST="$2"; shift 2 ;;
        --to=*)      DEST="${arg#--to=}"; shift ;;
        --list)      ACTION="list"; shift ;;
        -h|--help)
            sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            err "Unknown arg: $arg"
            echo "Try: $0 --help"
            exit 2
            ;;
    esac
done

# --- preflight ---
if [ ! -d "$REPO_ROOT/data" ]; then
    err "no data/ directory at $REPO_ROOT"
    dim "Run ./quickstart.sh serve at least once to create the KB"
    exit 1
fi

# --- list mode ---
if [ "$ACTION" = "list" ]; then
    echo "Existing backups in $DEST:"
    if [ -d "$DEST" ]; then
        if [ -z "$(ls -1A "$DEST" 2>/dev/null)" ]; then
            dim "  (none)"
        else
            ls -1t "$DEST" 2>/dev/null | head -20 | while read -r d; do
                size=$(du -sh "$DEST/$d" 2>/dev/null | awk '{print $1}')
                echo "  $d  ($size)"
            done
        fi
    else
        dim "  (no backup dir yet)"
    fi
    exit 0
fi

# --- backup mode ---
mkdir -p "$DEST"
TS=$(date +%Y%m%d_%H%M%S)
TARGET="$DEST/$TS"

echo "📦 Backing up → $TARGET"
mkdir -p "$TARGET"

# Use rsync if available (faster, progress UI), else cp -a
if command -v rsync >/dev/null 2>&1; then
    if rsync -a "$REPO_ROOT/data/" "$TARGET/data/"; then
        :
    else
        err "rsync failed"
        rm -rf "$TARGET"
        exit 1
    fi
else
    if cp -a "$REPO_ROOT/data" "$TARGET/data"; then
        :
    else
        err "cp failed"
        rm -rf "$TARGET"
        exit 1
    fi
fi

# --- verify ---
if [ ! -f "$TARGET/data/kb.sqlite" ]; then
    err "Backup failed: kb.sqlite missing in target"
    rm -rf "$TARGET"
    exit 1
fi

SIZE=$(du -sh "$TARGET/data" | awk '{print $1}')
N_DOCS=$("$REPO_ROOT/.venv/bin/python" -c "
import sqlite3
c = sqlite3.connect('$TARGET/data/kb.sqlite')
print(c.execute('SELECT COUNT(*) FROM documents').fetchone()[0])
" 2>/dev/null || echo "?")

ok "Backup complete"
dim "  path:  $TARGET"
dim "  size:  $SIZE"
dim "  docs:  $N_DOCS"

# List recent backups so the user knows what they have
echo ""
echo "Recent backups (newest first):"
ls -1t "$DEST" 2>/dev/null | head -5 | while read -r d; do
    s=$(du -sh "$DEST/$d" 2>/dev/null | awk '{print $1}')
    echo "  $d  ($s)"
done
