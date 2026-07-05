#!/usr/bin/env bash
# quickstart.sh — One-shot launcher for AI Knowledge Cockpit.
#
# Why this exists: COMMANDS.md has the canonical step-by-step, but new users
# (and tired users) want one line that does the right thing. This script
# wraps the common pipeline without hiding anything — every step is logged,
# nothing is auto-OCR'd, no IM credentials are invented.
#
# Usage:
#   ./quickstart.sh check                       # inspect environment, print what's missing
#   ./quickstart.sh serve [dingtalk|wecom|cli]  # init + ingest inbox + ingest notes + serve
#                                                # default = dingtalk
#   ./quickstart.sh serve --ocr dingtalk        # also OCR scanned pages (uses VL_API_KEY)
#   ./quickstart.sh help                        # this message
#
# Design rules (do not silently violate):
#   - Never auto-edit .env. If keys are missing, print what to set and where.
#   - Never run --ocr unless the user explicitly passed --ocr. OCR costs tokens.
#   - Never pick the IM platform for the user. If missing, ask.
#   - Every step prints its name before running, so failures are diagnosable.
#   - All real work goes through start.sh → app.py. This script is just orchestration.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------- color helpers (only if stdout is a TTY) ----------
if [[ -t 1 ]]; then
    C_OK="\033[32m"; C_WARN="\033[33m"; C_ERR="\033[31m"; C_DIM="\033[2m"; C_RST="\033[0m"
else
    C_OK=""; C_WARN=""; C_ERR=""; C_DIM=""; C_RST=""
fi
ok()   { echo -e "${C_OK}  ✓${C_RST} $*"; }
warn() { echo -e "${C_WARN}  !${C_RST} $*"; }
err()  { echo -e "${C_ERR}  ✗${C_RST} $*" >&2; }
dim()  { echo -e "${C_DIM}    $*${C_RST}"; }
hdr()  { echo -e "\n${C_OK}== $* ==${C_RST}"; }

# ---------- env loader (mirrors start.sh, kept local so quickstart is self-contained) ----------
load_env() {
    if [[ -f "$ROOT/.env" ]]; then
        set -a
        # shellcheck disable=SC1091
        source "$ROOT/.env"
        set +a
    fi
}

# ---------- environment check ----------
check() {
    hdr "Environment check"
    local fail=0

    # Python
    if [[ -x "$ROOT/.venv/bin/python" ]]; then
        ok "Python venv: .venv/bin/python"
        dim "$($ROOT/.venv/bin/python --version 2>&1)"
    else
        err "Python venv missing: .venv/"
        dim "Fix: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
        fail=1
    fi

    # .env
    if [[ -f "$ROOT/.env" ]]; then
        ok ".env file: present"
    else
        warn ".env file: missing"
        dim "Fix: cp .env.example .env  (then fill the values you need)"
        dim "You can still use CLI search/ingest without .env — only OCR + IM bots need it."
        # not fatal
    fi

    # Load .env into current shell for the rest of the check
    load_env

    # VL_API_KEY (needed only for OCR + query rewrite)
    if [[ -n "${VL_API_KEY:-}" && "${VL_API_KEY}" != "your_vl_api_key_here" ]]; then
        ok "VL_API_KEY: set (OCR + query rewrite enabled)"
    else
        warn "VL_API_KEY: empty"
        dim "Needed only if you want --ocr on scanned PDFs or query rewriting."
        dim "Get one: https://platform.minimaxi.com → Token Plan"
        dim "Add to .env: VL_API_KEY=eyJhbGc..."
    fi

    # DingTalk credentials
    local dt_ok=0
    if [[ -n "${DINGTALK_APP_KEY:-}" && "${DINGTALK_APP_KEY}" != "your_app_key_here" \
       && -n "${DINGTALK_APP_SECRET:-}" && "${DINGTALK_APP_SECRET}" != "your_app_secret_here" ]]; then
        dt_ok=1
    fi
    if (( dt_ok )); then
        ok "DingTalk credentials: set (./quickstart.sh serve dingtalk will work)"
    else
        warn "DingTalk credentials: missing (DINGTALK_APP_KEY / DINGTALK_APP_SECRET)"
        dim "See IM_SETUP.md → DingTalk section to create a Stream-mode bot."
    fi

    # WeCom credentials
    local wc_ok=0
    if [[ -n "${WECOM_TOKEN:-}" && "${WECOM_TOKEN}" != "your_token_here" \
       && -n "${WECOM_ENCODING_AES_KEY:-}" && "${WECOM_ENCODING_AES_KEY}" != "your_aes_key_here" \
       && -n "${WECOM_CORP_ID:-}" && "${WECOM_CORP_ID}" != "wwxxxxxxxxxxxxxxxx" ]]; then
        wc_ok=1
    fi
    if (( wc_ok )); then
        ok "WeCom credentials: set (./quickstart.sh serve wecom will work)"
    else
        warn "WeCom credentials: missing (WECOM_CORP_ID / WECOM_AGENT_ID / WECOM_TOKEN / WECOM_ENCODING_AES_KEY)"
        dim "See IM_SETUP.md → WeCom section. Needs a public callback URL (e.g. ngrok)."
    fi

    # KB state
    if [[ -f "$ROOT/data/kb.sqlite" ]]; then
        local n_docs n_chunks
        n_docs=$("$ROOT/.venv/bin/python" -c "
import sqlite3
c = sqlite3.connect('$ROOT/data/kb.sqlite')
print(c.execute('SELECT COUNT(*) FROM documents').fetchone()[0])
" 2>/dev/null || echo "?")
        n_chunks=$("$ROOT/.venv/bin/python" -c "
import sqlite3
c = sqlite3.connect('$ROOT/data/kb.sqlite')
print(c.execute('SELECT COUNT(*) FROM chunks').fetchone()[0])
" 2>/dev/null || echo "?")
        ok "KB state: $n_docs docs / $n_chunks chunks"
    else
        warn "KB not initialized yet (no data/kb.sqlite)"
        dim "Will be created on first ./quickstart.sh serve."
    fi

    hdr "Summary"
    if (( fail )); then
        err "One or more blockers above. Fix the red items before serving."
        exit 1
    fi
    ok "Environment is workable. Pick a command:"
    dim "  ./quickstart.sh serve          # init + ingest + start DingTalk"
    dim "  ./quickstart.sh serve wecom    # init + ingest + start WeCom"
    dim "  ./quickstart.sh serve cli      # init + ingest + drop into a search prompt"
    exit 0
}

# ---------- serve ----------
serve() {
    local want_ocr=0
    local platform="dingtalk"

    # parse args: --ocr flag + positional platform
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --ocr) want_ocr=1; shift ;;
            dingtalk|wecom|cli) platform="$1"; shift ;;
            -h|--help)
                echo "Usage: ./quickstart.sh serve [--ocr] [dingtalk|wecom|cli]"
                exit 0
                ;;
            *)
                err "Unknown arg: $1"
                echo "Usage: ./quickstart.sh serve [--ocr] [dingtalk|wecom|cli]"
                exit 2
                ;;
        esac
    done

    load_env

    # 1. venv must exist
    if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
        err "venv missing. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
        exit 1
    fi

    # 2. init DB + folders
    hdr "Step 1/3: init DB and folders"
    "$ROOT/.venv/bin/python" "$ROOT/app.py" init

    # 3. ingest inbox/ + notes/ (idempotent — file_hash dedupe makes reruns cheap)
    hdr "Step 2/3: ingest inbox/ + notes/"
    if [[ -d "$ROOT/inbox" ]]; then
        if (( want_ocr )); then
            warn "--ocr enabled: scanned pages will be sent to MiniMax-M3 (costs tokens)."
            "$ROOT/.venv/bin/python" "$ROOT/app.py" ingest "$ROOT/inbox" --recursive --ocr
        else
            "$ROOT/.venv/bin/python" "$ROOT/app.py" ingest "$ROOT/inbox" --recursive
        fi
    else
        warn "inbox/ not found, skipping."
    fi
    if [[ -d "$ROOT/notes" ]]; then
        "$ROOT/.venv/bin/python" "$ROOT/app.py" ingest "$ROOT/notes" --recursive
    else
        warn "notes/ not found, skipping."
    fi

    # 4. start the platform
    hdr "Step 3/3: start platform = $platform"
    case "$platform" in
        cli)
            dim "Dropping into a quick status + search demo."
            dim "After this, run any ./start.sh search ... command yourself."
            "$ROOT/.venv/bin/python" "$ROOT/app.py" status
            ;;
        dingtalk)
            if [[ -z "${DINGTALK_APP_KEY:-}" || -z "${DINGTALK_APP_SECRET:-}" ]]; then
                err "DingTalk credentials missing in .env (DINGTALK_APP_KEY / DINGTALK_APP_SECRET)."
                dim "Either fill them in and rerun, or use: ./quickstart.sh serve wecom|cli"
                exit 1
            fi
            exec "$ROOT/.venv/bin/python" "$ROOT/app.py" serve dingtalk
            ;;
        wecom)
            local missing=()
            [[ -z "${WECOM_CORP_ID:-}" ]]         && missing+=("WECOM_CORP_ID")
            [[ -z "${WECOM_AGENT_ID:-}" ]]        && missing+=("WECOM_AGENT_ID")
            [[ -z "${WECOM_TOKEN:-}" ]]           && missing+=("WECOM_TOKEN")
            [[ -z "${WECOM_ENCODING_AES_KEY:-}" ]] && missing+=("WECOM_ENCODING_AES_KEY")
            if (( ${#missing[@]} > 0 )); then
                err "WeCom credentials missing in .env: ${missing[*]}"
                dim "Either fill them in and rerun, or use: ./quickstart.sh serve dingtalk|cli"
                exit 1
            fi
            exec "$ROOT/.venv/bin/python" "$ROOT/app.py" serve wecom
            ;;
    esac
}

# ---------- dispatch ----------
case "${1:-}" in
    ""|help|-h|--help)
        sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
        exit 0
        ;;
    check)  shift; check ;;
    serve)  shift; serve "$@" ;;
    *)
        err "Unknown command: $1"
        echo "Run: ./quickstart.sh help"
        exit 2
        ;;
esac