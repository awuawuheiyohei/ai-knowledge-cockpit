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