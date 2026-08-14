# AI Knowledge Cockpit

Personal, retrieval-only knowledge base + IM bot + MCQ practice app.

> **Status: 1:1 with source PDFs, retrieval-only (no LLM in the hot path)**
>
> The KB must match the user's original PDFs exactly: nothing lost, every
> citation is auditable. LLM calls are reserved for narrow jobs: OCR
> fallback for scanned pages, query rewriting for failed searches, and
> image-input synthesis. The text path is pure BM25.

---

## What's in here

| Component | What it does | Where |
|---|---|---|
| **KB engine** | SQLite + BM25, paragraph-aware chunking, section-aware re-chunking, layout-aware 2-col PDF extraction, OCR line-block dedup | `bm25.py`, `chunks.py`, `sections.py`, `pdf_extract.py`, `storage.py` |
| **Ingest** | PDF / Markdown → chunks → SQLite + inverted index. `--ocr` flag for scanned pages. Hash-dedup so re-ingest is a no-op. | `ingest.py`, `pdf_ocr.py` |
| **Retrieval** | BM25 + optional hybrid (BM25 + sentence embedding) | `search.py`, `embedding.py` |
| **IM bots** | DingTalk (Stream mode, WebSocket) · WeCom (HTTP callback) · Feishu (Stream). Shared `im_router`. | `dingtalk_server.py`, `wecom_server.py`, `feishu_server.py`, `im_router.py` |
| **Image answer** | LLM synthesis over BM25 hits, strict citation enforcement, "未检索到" fallback | `image_extract.py`, `answer_synth.py` |
| **MCQ practice** | 494 questions extracted from 4 综合测试 PDFs. Random / wrong-book modes. KB knowledge-point links. | `exam/app.py`, `exam/templates/index.html`, `tools/extract_questions.py` |
| **Audit / verify** | 1:1 KB↔PDF audit, 3-layer OCR accuracy check, side-by-side chunk vs PDF viewer | `tools/audit_kb.py`, `tools/verify_ocr.py`, `tools/show_chunk.py` |
| **Monitoring** | Hourly health check (QA spot-check + feedback summary) | `tools/health_check.py` |

## Quick start

```bash
# 1. Create venv + install
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Initialize
.venv/bin/python app.py init

# 3. Drop source files into inbox/, then ingest
.venv/bin/python app.py ingest inbox/your.pdf

# 4. Search
.venv/bin/python app.py search "PKI 数字证书" --top 5

# 5. Run the IM bot
.venv/bin/python app.py serve dingtalk    # or: serve wecom / serve feishu

# 6. Run the exam web app
.venv/bin/python exam/app.py               # then open http://127.0.0.1:5001
```

## Hard rules (do not relax)

1. **No LLM in the hot retrieval path.** Pure BM25 (and optional
   sentence-transformer embeddings). If a query has no good hit, return
   zero — never fall back to a generative answer.
2. **Scanned pages are not silently dropped.** A page with <`SCAN_PAGE_MIN_CHARS`
   chars is flagged in `status` as `partial`; `ingest --ocr` re-OCRs them
   via a vision-language model.
3. **Citation grounding is enforced.** `answer_synth` rejects syntheses
   whose answer text doesn't overlap the retrieved chunks. No fabrication.
4. **Source URLs are auditable.** Every `[来源: ...]` citation links to a
   real chunk with `file://` URI.
5. **Dedupe by content hash.** Re-ingesting the same bytes is a no-op.
   Re-ingesting changed bytes replaces the document's chunks + index.

## Layout-aware PDF handling

`pdf_extract._classify_layout` decides between single-column, two-column,
or mixed layouts per page. Two-column pages get sorted-block reading
order; single-column pages use plain pymupdf text. Mixed layouts fall
back to pymupdf's native order to avoid garbling wrapped paragraphs.

## OCR strategy

1. Native text layer (fast, free).
2. If too short → render page → MiniMax-VL OCR (M3 model).
3. `OcrError` and `image is sensitive` rejections are logged and the
   page is left in `partial` status. We do **not** retry on VL policy
   rejections.

## Tests

```bash
make test           # 49 unit tests (unittest, ~2s)
make test-cov       # + coverage report
```

CI runs on GitHub Actions (`.github/workflows/ci.yml`):

- `unit-tests` — syntax-check + run the full unit suite on macos-14.
- `exam-app-smoke` — boot the exam Flask app and hit `/api/health`.
- `release.yml` — on `v*` tag push, verify the question-bank shape +
  cut a GitHub release.

## CI/CD

| Workflow | Triggers | What it does |
|---|---|---|
| `ci.yml` | push to main, every PR | unit tests + lint + exam-app boot smoke |
| `release.yml` | `v*` tag push | full verify + GitHub release cut |
| `dependabot.yml` | weekly | auto-PR for pip + GitHub-Actions updates |

Release flow:
```bash
make push-tag TAG=v0.1.0
# → git tag + push, then CI verifies, then GitHub release is cut
```

## Operational scripts

| Script | Use |
|---|---|
| `scripts/add.sh <path>` | copy + ingest one file |
| `scripts/ask.sh "<q>"` | BM25 search from CLI |
| `scripts/rebuild.sh` | wipe + rebuild BM25 index |
| `scripts/backup.sh` | snapshot `data/kb.sqlite` to backups/ |

## Deploy

| What | Where |
|---|---|
| DingTalk bot | `deploy/com.mavis.knowledge-bot.plist` (launchd, UID 501) |
| KB rebuild scheduler | `deploy/com.mavis.kb-rebuild.plist` |
| Exam web app | `deploy/com.mavis.exam-app.plist` |

`launchctl load ~/Library/LaunchAgents/com.mavis.knowledge-bot.plist` to install. `KeepAlive.SuccessfulExit=false` so a clean shutdown stays down.

## License

Personal project. Not for redistribution.
