# 🧠 Project Context

This project is a **personal, retrieval-only knowledge base**.

It is NOT a chatbot and does NOT call any LLM during retrieval.

The KB is built from the user's own files (PDF + Markdown). Each
document is split into chunks and indexed in a local SQLite database
using BM25 over Latin words + CJK character bigrams. Every search
hit carries its source (filename + page) so answers are auditable.

---

# 🏗️ Project Structure

- `app.py`              → CLI entry point (all commands, including `serve wecom` / `serve dingtalk`)
- `paths.py`            → filesystem paths (inbox/, data/, db)
- `config.py`           → tunables (chunk size, BM25 hyperparameters, scan threshold, rewrite threshold)
- `storage.py`          → SQLite schema + CRUD (documents, chunks, index_term)
- `bm25.py`             → inverted index + BM25Okapi scoring
- `chunks.py`           → paragraph-aware text chunking with overlap
- `pdf_extract.py`      → pymupdf-based PDF extraction + scan-page detection + OCR fallback
- `pdf_ocr.py`          → VL OCR engine (renders scanned pages, calls MiniMax-M3)
- `vl_config.py`        → load VL credentials (VL_API_KEY etc.)
- `llm_config.py`       → load LLM credentials for query rewriting (falls back to VL_API_KEY)
- `query_rewrite.py`    → LLM-based query reformulation (strictly KB-blind)
- `md_extract.py`       → plain Markdown reader
- `ingest.py`           → file → chunks → SQLite → index pipeline (with --ocr support)
- `search.py`           → BM25 query + pretty rendering
- `im_router.py`        → unified message → KB → Markdown reply (auto-rewrite + /expand)
- `im_config.py`        → load WeCom / DingTalk credentials from environment
- `env_loader.py`       → minimal .env file loader (no python-dotenv dep)
- `wecom_server.py`     → WeCom smart-bot callback server (Flask + AES + SHA1, no wechatpy)
- `dingtalk_server.py`  → DingTalk Stream-mode bot (WebSocket long connection)
- `start.sh`            → launcher that auto-loads .env then runs `app.py ...`
- `inbox/`              → drop source files (PDF / .md / .markdown) here
- `data/kb.sqlite`      → the KB (single SQLite file)
- `data/originals/`     → mirrored copies of ingested files (audit trail)
- `notes/`              → existing user markdown notes (also ingestable)
- `IM_SETUP.md`         → onboarding guide for both IM platforms
- `OCR_SETUP.md`        → OCR (scanned PDFs) + query rewrite config
- `requirements.txt`    → `pymupdf`, `flask`, `pycryptodome`, `dingtalk-stream`, `anthropic`

---

# ✅ Core Commands

```bash
python app.py init                                # create db and folders
python app.py ingest inbox/foo.pdf                # ingest one PDF
python app.py ingest notes/ --recursive           # ingest a directory
python app.py list                                # list ingested sources
python app.py search "PKI 数字证书" --top 5       # BM25 search
python app.py search "..." --doc "filename.pdf"   # restrict to one source
python app.py remove "filename.pdf"               # remove by name
python app.py remove 12                           # or by id
python app.py rebuild                             # rebuild index from chunks
python app.py status                              # stats + scan warnings
python app.py serve dingtalk                      # start DingTalk Stream bot
python app.py serve wecom                         # start WeCom callback server
```

---

# 🚧 Hard Rules

### 🚫 No LLM in retrieval

Retrieval is pure BM25 over locally-tokenized text. We never call an
LLM to "fill in the gaps" or rephrase results. If a query has no
relevant terms in the index, we return **zero hits** — we do not
fall back to generative answers.

### 🚫 Scanned pages are not silently dropped

A PDF page that has fewer than `SCAN_PAGE_MIN_CHARS` of meaningful
text is flagged as `partial` and listed in `status`. The ingest
report tells the user which pages need OCR.

### ⚠️ LLM is allowed ONLY for OCR fallback (opt-in)

When `ingest --ocr` is passed, scanned pages are sent to a vision-language
model (default MiniMax-M3) to extract text. The LLM is used as a
*replacement for OCR software*, not for reasoning or paraphrasing:

- Prompt explicitly forbids summarizing / rewriting / translating
- Output is treated as raw page text and goes through the same
  chunking + indexing pipeline as native text
- Every chunk carries a `via_ocr` flag in SQLite for audit
- `documents.ocr_pages` records which pages were OCR'd
- This is **opt-in** (`--ocr`); default ingest never calls any LLM

### ⚠️ LLM is allowed ONLY for query rewriting (auto, narrow scope)

When BM25 fails to find good hits (zero hits or top score below
`REWRITE_SCORE_THRESHOLD = 2.0`), `im_router` invokes an LLM to
reformulate the user's natural-language query into keywords, then
re-runs BM25 with the reformulated query.

The LLM is used strictly as a "natural language → keywords" translator:

- LLM sees ONLY the user's raw query string — never the KB
- LLM is instructed to output 3-7 keywords, not an answer
- User still sees only KB excerpts with source citations
- `query_rewrite.py` records every invocation via the logger
- Can be force-triggered with `/expand <query>` from any IM
- Triggered automatically when BM25 top-1 score < threshold
- Falls back to original-query results on any LLM error
- Requires `VL_API_KEY` (or `LLM_API_KEY`); same MiniMax-M3 model

### 🚫 No embeddings by default

Adding a local sentence-transformer embedding is allowed but is opt-in
(via `config.py`). It does not violate "no LLM" because it's a
vector-only model with no generative capability.

### ✅ Dedupe by file hash

Re-ingesting the same bytes is a no-op. Re-ingesting changed bytes
replaces the document's chunks + index rows.

---

# 🧪 Validation Rules

After any change to `bm25.py`, `chunks.py`, `pdf_extract.py`, `pdf_ocr.py`,
or `storage.py`, run:

```bash
python app.py init
python app.py ingest notes --recursive   # smoke test on real content
python app.py search "安全治理" --top 5  # should return relevant hits
python app.py search "abcdefxyz垃圾词"   # should return NO hits
python app.py status                    # stats should look sane

# OCR path (mock anthropic so no real API call):
python -c "
import os; os.environ['VL_API_KEY']='fake'
from unittest.mock import patch
from pdf_ocr import _call_vl
with patch('pdf_ocr._call_vl', return_value='mock OCR text'):
    import fitz, ingest
    doc = fitz.open()
    p = doc.new_page()
    doc.save('/tmp/_t.pdf'); doc.close()
    ingest.ingest_path('/tmp/_t.pdf', use_ocr=True)
"

---

# 🛣️ Roadmap

- ~~**WeCom / DingTalk bridge**~~ — DONE. Both adapters live in
  `wecom_server.py` and `dingtalk_server.py`, sharing `im_router.py`.
- **Local embedding fallback** — only if user finds BM25 too brittle
  for noisy queries.
- **Strict LLM summary mode** — *only* over retrieved chunks, with
  mandatory source citation in the prompt. Off by default.

# 💬 IM Integration

See `IM_SETUP.md` for the full onboarding. Key invariants:

- **No LLM in IM responses either.** Both adapters route through
  `im_router.handle_message()`, which is BM25-only.
- **WeCom**: needs a public URL (use ngrok for local dev).
  All crypto (AES-256-CBC + SHA1) is implemented in
  `wecom_server.WeComCrypto` — no wechatpy dependency.
- **DingTalk**: Stream mode (WebSocket). No public URL needed.
  Uses the official `dingtalk-stream` SDK.
- Slash commands `/help` and `/status` work on both platforms.

---

# 🔒 Safety Rules

- Do NOT access or expose `.env`.
- Do NOT print API keys.
- Do NOT introduce a network call to any LLM provider unless the user
  explicitly opts in via a feature flag.