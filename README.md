# AI Knowledge Cockpit (v2)

A local, retrieval-only knowledge base. Drop your PDFs (and Markdown files)
into `inbox/`, ingest, then ask from CLI **or chat surface**.
Every answer cites its source.

**No LLM is involved in retrieval.** This is BM25 over the chunks of your
own files. We never paraphrase, never infer, never invent.

**Two narrow LLM escape hatches**, both opt-in or narrowly scoped:

1. **`ingest --ocr`** — scanned PDF pages go through MiniMax-M3 for OCR.
2. **Query rewriting** — when BM25 gives zero / weak hits, the user's
   query is reformulated by MiniMax-M3 into keyword form, then BM25
   re-runs. The LLM **never sees the KB** and **never produces the final
   answer** — only translates natural language → keywords.

Both share the same `VL_API_KEY` (MiniMax-M3 via the Anthropic SDK).

## Why v2

The previous version was a CLI quiz/study copilot for CISSP. This rewrite
turns it into a generic private knowledge base you can query from any
chat surface — terminal, WeCom, or DingTalk.

## Quickstart

> 第一次上手?**一行命令**就够了:
> ```bash
> ./quickstart.sh check     # 看环境缺什么(venv / .env / IM 凭证)
> ./quickstart.sh serve     # init + ingest inbox/ + ingest notes/ + 起 DingTalk bot
> ```
> 详细步骤和故障排查看 **[COMMANDS.md](COMMANDS.md)**。

### 常见场景(一行命令)

| 你想干嘛 | 命令 |
|---|---|
| 只想本地跑跑,不上 IM | `./quickstart.sh serve cli` |
| 起 DingTalk bot(推荐,免域名) | `./quickstart.sh serve dingtalk` |
| 起企业微信 bot(需公网域名) | `./quickstart.sh serve wecom` |
| 扫描版 PDF 一起 OCR 进去 | `./quickstart.sh serve --ocr dingtalk`(花 VL API 钱) |

下面这套是"知道自己在干嘛"时的逐步版本:

```bash
# 1. Initialize (creates db.sqlite, inbox/, data/originals/)
python app.py init

# 2. Drop your files into inbox/, then ingest:
python app.py ingest inbox/foo.pdf
python app.py ingest inbox/                 # whole directory
python app.py ingest inbox/ --recursive     # walk subfolders
python app.py ingest notes/ --recursive     # existing markdown notes work too

# 2b. Scanned PDFs? Add --ocr to OCR them (requires VL_API_KEY; costs ~0.005 元/page):
python app.py ingest inbox/ --recursive --ocr

# 3. Search from CLI:
python app.py search "PKI 数字证书"
python app.py search "risk assessment" --top 10
python app.py search "什么" --doc "第1章-实现安全治理的原则和策略-知识点.md"

# 4. Or query from chat:
python app.py serve dingtalk     # DingTalk Stream bot (recommended — no public URL)
python app.py serve wecom        # Enterprise WeChat bot (needs public URL or ngrok)
```

See **[IM_SETUP.md](IM_SETUP.md)** for the full WeCom / DingTalk onboarding
and **[OCR_SETUP.md](OCR_SETUP.md)** for scanned-PDF auto-OCR.

## Commands

| Command | What it does |
| --- | --- |
| `init` | Create DB and folders |
| `ingest <path> [-r] [--ocr]` | Ingest files; `--ocr` OCRs scanned pages via MiniMax-M3 |
| `list` | Show all ingested sources |
| `search <query> [--top N] [--doc NAME]` | BM25 search |
| `remove <id-or-filename>` | Drop a document from the KB |
| `rebuild` | Wipe and rebuild the inverted index |
| `status` | Show KB statistics, scan warnings, OCR usage |
| `serve dingtalk` | Start DingTalk Stream-mode bot |
| `serve wecom` | Start WeCom callback server |

## Operator scripts

Three shell helpers in `scripts/` for the day-to-day loop. They wrap
`app.py` so you don't have to remember the subcommand grammar.

| Script | What it does |
| --- | --- |
| `scripts/add.sh <file-or-folder> [--ocr] [--demo]` | Copy into `inbox/`, run `app.py ingest`, print 5-line status. `--ocr` for scanned PDFs (costs VL tokens); `--demo` runs a top-3 search using the filename. |
| `scripts/ask.sh "<query>" [--rewrite] [--top N] [--doc FILE]` | BM25 search. With `--rewrite`, the query first goes through `query_rewrite.rewrite()` so "用户能用什么密码登录" becomes keyword form before BM25 sees it. |
| `scripts/rebuild.sh [--yes] [--dry-run]` | Wipe and rebuild the BM25 inverted index, showing chunk-count before/after. Use this after editing `config.py` or suspecting index drift. |

All three:
- Live in `scripts/` (pure bash, no Python package)
- `set -euo pipefail`, pick `.venv/bin/python` first (fall back to system `python3`)
- Use absolute paths only (a hook for the `mavis-trash` tool refuses to expand shell variables like `~` or `$HOME`)

### When to use which

| You want to… | Run this |
| --- | --- |
| Just downloaded a new PDF | `./scripts/add.sh ~/Downloads/new.pdf` |
| Drop a whole folder of PDFs in | `./scripts/add.sh ~/Downloads/cissp_week5/ --recursive` |
| Force OCR on a scanned PDF | `./scripts/add.sh inbox/scanned.pdf --ocr` |
| Quick keyword search | `./scripts/ask.sh "PKI 数字证书" --top 5` |
| Ask in plain Chinese | `./scripts/ask.sh "忘了那个认证的东西叫什么" --rewrite` |
| I changed `config.py` and things look off | `./scripts/rebuild.sh --yes` |
| Preview rebuild without doing it | `./scripts/rebuild.sh --dry-run` |

### Adjacent tools (not in `scripts/`)

| Path | Purpose |
| --- | --- |
| `quickstart.sh` (root) | `check` / `serve [dingtalk\|wecom\|cli]` — the day-one launcher. |
| `start.sh` (root) | Internal launcher: loads `.env` then runs `app.py ...`. |
| `tools/batch_ocr_inbox.sh` | Batch-OCR every failed PDF in `inbox/` (run `--dry-run` first; ~¥0.01-0.03/page). |

## Hard rules

1. **No LLM.** Retrieval is BM25 over character bigrams (CJK) + Latin words.
   If a query has no relevant terms in the index, the answer is "no hits"
   — never an LLM fallback.
2. **Every hit cites its source.** Each result row carries the filename,
   source type, and page number (for PDFs) so you can verify it.
3. **Scanned PDFs are not silently dropped.** Pages that look like scanned
   images (no extractable text) are flagged with `partial` status and
   reported in `status`. The fix is OCR → Markdown → re-ingest.
4. **Dedupe by file hash.** Re-ingesting the same file (same bytes) is a
   no-op. Re-ingesting a *changed* file replaces chunks + index.

## Layout

```
ai_knowledge_cockpit/
├── app.py              # CLI entry
├── paths.py            # paths
├── config.py           # tunables (chunk size, BM25 k1/b, ...)
├── storage.py          # SQLite schema + CRUD
├── bm25.py             # inverted index + BM25 scoring
├── chunks.py           # paragraph-aware text chunking
├── pdf_extract.py      # pymupdf-based extraction + scan-page detection
├── md_extract.py       # plain Markdown reader
├── ingest.py           # file → chunks → SQLite → index pipeline
├── search.py           # query + render
├── inbox/              # ← drop your PDFs/MD here
├── data/
│   ├── kb.sqlite       # the KB itself (single file)
│   └── originals/      # mirrored copies of ingested files
└── notes/              # ← existing markdown notes also ingestable
```

## Tuning

Edit `config.py`:

- `CHUNK_SIZE` / `CHUNK_OVERLAP` — chunk granularity vs context per hit.
- `BM25_K1` / `BM25_B` — BM25 hyperparameters (defaults from Robertson).
- `SCAN_PAGE_MIN_CHARS` — threshold for "this page is probably scanned".
- `USE_BIGRAM_FOR_CJK` — toggle CJK bigram tokenization.

## Roadmap (NOT in v2)

- WeCom / DingTalk bridge — pending. CLI comes first; once that feels
  right, wrap `search.format_compact()` in a webhook.
- Optional local embedding for semantic retrieval — would not violate the
  "no LLM" rule but is opt-in.
- Optional summary call — strictly opt-in, only over retrieved chunks,
  with mandatory source citation in the prompt.