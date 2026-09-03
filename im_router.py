"""
im_router.py — Single chokepoint between IM platforms and the KB.

Both the WeCom and DingTalk adapters call `handle_message()` with raw
user text. We run BM25 against the KB and format a Markdown reply.

Hard rule: no LLM is called here... EXCEPT for query rewriting, which is
strictly opt-in per call (auto when BM25 is weak, force via /expand) and
strictly scoped to reformulating the user's query string into keywords.
The LLM never sees the KB and never produces the final answer — the user
still sees only KB excerpts with source citations.

Two entry points
---------------
- `handle_message(platform, raw_text)` — text input. 场景 1: raw mode only.
  LLM is NEVER allowed to "synthesize" / "summarize" / "answer" here —
  only keyword rewriting. The reply is always KB excerpts + source tags.

- `handle_image(platform, image_path)` — image input. 场景 2: synth mode.
  This is the ONLY place the LLM is allowed to produce a synthesized
  answer (per the "image-triggered + mandatory citation" exception in
  CLAUDE.md "Hard rules"). The synthesis MUST cite a source for every
  claim, and the raw hits are always included alongside the synthesis
  so the user can verify.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime

import paths

import config
import search as search_mod
import storage
import query_rewrite
import image_extract
import answer_synth


logger = logging.getLogger("im_router")


def _empty_help(platform: str) -> str:
    """Reply when the KB has nothing in it."""
    return (
        "知识库还是空的。请先把 PDF 或 Markdown 放进 `inbox/` 目录，\n"
        "然后在终端跑：`python app.py ingest inbox/ --recursive`\n\n"
        f"完成后向我发任何关键词即可（当前平台：{platform}）。"
    )


def _no_hits(query: str) -> str:
    """Reply when no relevant chunks are found."""
    return (
        f"未在知识库中找到与 **{query}** 相关的内容。\n\n"
        "可能的原因:\n"
        "- 用词太专业 / 太长 → 换个近义词试试\n"
        "- 该知识点还没入库 → 跑 `python app.py ingest ...`\n"
        "- 该知识点所在页面是扫描件 → 用 OCR 工具转 .md 后再入库"
    )


# ---------------------------------------------------------------------------
# Source-attribution rendering helpers (Day 5 — clickable file:// links)
# ---------------------------------------------------------------------------

def _source_link(hit: dict) -> str:
    """
    Render one source citation as a Markdown line, with a clickable
    `file://` link when we have a local PDF (and the platform renders
    Markdown links). Falls back to a code-formatted path so the user
    always sees the location even on platforms that strip Markdown links
    (some IM clients do).

    The link uses `#page=N` which is the de-facto convention for jumping
    to a specific page in a PDF — works in macOS Preview, most modern
    PDF readers, and many editors.
    """
    filename = hit.get("filename", "?")
    page = hit.get("page_num")
    score = hit.get("score", 0.0)
    relpath = hit.get("relative_path", "")
    if relpath:
        abs_path = paths.BASE / relpath
        if abs_path.exists():
            path_str = str(abs_path.resolve())
            if page is not None:
                # Clickable link (in Markdown-rendering clients) +
                # plain-text path right after, so the user always sees
                # the source even if the link is dropped.
                return (
                    f"**[{filename} p.{page}](file://{path_str}#page={page})** "
                    f"· `{path_str}`  ·  score={score:.2f}"
                )
            # Markdown source (no page number) — link to the file itself.
            return (
                f"**[{filename}](file://{path_str})** "
                f"· `{path_str}`  ·  score={score:.2f}"
            )
    # Fallback: no relative_path on the hit (shouldn't happen post-ingest,
    # but be defensive) — just show the filename + page.
    if page is not None:
        return f"`{filename}` · p.{page}  ·  score={score:.2f}"
    return f"`{filename}` (md)  ·  score={score:.2f}"


# ---------------------------------------------------------------------------
# User feedback loop (Day 4 — /good /bad /partial)
# ---------------------------------------------------------------------------
# Per-(platform, user_id) in-memory record of the LAST bot reply. The
# /good /bad /partial slash commands look this up to attach a verdict.
# Module-level (not in handle_message) so it survives across message
# calls but is cleared on bot restart. For our single-user-ish use case
# (one operator + maybe a few family members) this is sufficient;
# multi-tenant SaaS would need Redis.
_LAST_REPLIES: dict[tuple[str, str], dict] = {}
_LAST_REPLIES_TTL_S = 30 * 60  # 30 min — feedback must be timely


def _record_last_reply(platform: str, user_id: str, question: str, reply: str,
                       message_type: str = "text") -> None:
    """Stash the last reply so /good /bad /partial can attach to it."""
    if not user_id:
        return
    key = (platform, user_id)
    _LAST_REPLIES[key] = {
        "ts": time.time(),
        "question": (question or "")[:500],
        "reply": (reply or "")[:3000],
        "message_type": message_type,
    }
    # Opportunistic GC — drop entries older than TTL.
    cutoff = time.time() - _LAST_REPLIES_TTL_S
    for k in list(_LAST_REPLIES.keys()):
        if _LAST_REPLIES[k]["ts"] < cutoff:
            del _LAST_REPLIES[k]


def _pop_last_reply(platform: str, user_id: str) -> dict | None:
    """Return AND REMOVE the last reply record (one-shot feedback)."""
    key = (platform, user_id)
    return _LAST_REPLIES.pop(key, None)


def save_feedback(platform: str, user_id: str, verdict: str,
                  last_context: dict, note: str = "") -> tuple[bool, str]:
    """
    Append a user feedback record to `data/feedback/YYYY-MM-DD.jsonl`.

    Public API for the IM adapters' /good /bad /partial handlers. Each
    adapter maintains its own per-user "last reply" dict and calls this
    on feedback. The adapter may also have its own note (e.g. user
    typed `/bad answer was about the wrong chapter`); that's `note`.

    Returns (ok, message_for_user).
    """
    if verdict not in ("good", "bad", "partial"):
        return False, f"未知反馈类型: {verdict}"
    if not last_context:
        return False, "找不到上一条回复(可能 bot 刚重启,或反馈命令离上一条回复超过 30 分钟)"

    paths.ensure_dirs()
    feedback_dir = paths.DATA / "feedback"
    feedback_dir.mkdir(parents=True, exist_ok=True)
    fname = feedback_dir / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"

    record = {
        "ts": time.time(),
        "platform": platform,
        "user_id": user_id,
        "verdict": verdict,
        "note": (note or "")[:500],
        "last_reply": {
            "question": (last_context.get("question") or "")[:500],
            "reply": (last_context.get("reply") or "")[:3000],
            "message_type": last_context.get("message_type", "text"),
        },
    }
    with open(fname, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return True, f"📝 反馈已记录({verdict})。" + (f" 备注: {note}" if note else "")


def _handle_feedback(platform: str, user_id: str, verdict: str) -> str:
    """Handle /good /bad /partial — pop the last reply and save it."""
    if not user_id:
        return "⚠️ 该平台未传入 user_id,反馈功能不可用(联系开发者)。"
    last = _pop_last_reply(platform, user_id)
    if last is None:
        return (
            "⚠️ 找不到上一条回复。\n\n"
            "反馈命令必须跟在 bot 回复之后(< 30 分钟内)。\n"
            "先问一个问题 → 等回复 → 再发 `/good` `/bad` 或 `/partial`。"
        )
    ok, msg = save_feedback(platform, user_id, verdict, last)
    return msg if ok else f"⚠️ {msg}"


# ---------------------------------------------------------------------------
# /help text — single source of truth so all platforms stay in sync
# ---------------------------------------------------------------------------
_HELP_TEXT = (
    "**AI Knowledge Cockpit · 帮助**\n\n"
    "- 直接发送关键词,我会检索本地知识库并附来源\n"
    "- 输入模糊、口语化也没事——我会在 BM25 弱命中时**自动用 LLM 改写 query** 再查\n"
    "- 加 `/expand` 前缀强制改写(即使 BM25 强命中)\n"
    "- `/status` 查看知识库统计\n"
    "- `/good` `/bad` `/partial` 对上一条 bot 回复打分(30 分钟内有效)\n"
    "- `/help`  查看本帮助\n\n"
    "**硬规则**:\n"
    "- LLM 只用来改写 query 和(图片场景)综合,**绝不**凭空生成答案\n"
    "- 最终答案均来自 KB 原文 + 来源标注\n"
    "- 没找到就说没找到,不会编"
)


def _format_hits_markdown(
    query: str,
    hits: list[dict],
    *,
    via_rewrite: bool = False,
    rewritten_query: str | None = None,
    weak: bool = False,
) -> str:
    """Format hits as Markdown with source attribution per chunk.

    Day 5 (2026-08-06): source citations now use clickable `file://` links
    when we can resolve the absolute path of the source file. On platforms
    that render Markdown links (most modern IM clients), clicking jumps
    straight to the cited page in the PDF. On platforms that strip links,
    the full path is also shown as inline code so the user can copy and
    open it manually.
    """
    lines: list[str] = []
    if via_rewrite and rewritten_query and rewritten_query != query:
        lines.append(f"### 🔎 检索：{query}")
        lines.append(f"↳ *自动改写为*：**{rewritten_query}**")
    else:
        lines.append(f"### 🔎 检索：{query}")
    lines.append(f"命中 {len(hits)} 条（按相关度排序）：\n")
    for i, h in enumerate(hits, start=1):
        lines.append(f"**[{i}]** {_source_link(h)}")
        snippet = h["chunk_text"].strip().replace("\n", " ")
        if len(snippet) > 320:
            snippet = snippet[:320].rstrip() + "…"
        lines.append(f"> {snippet}")
        lines.append("")
    lines.append("---")
    if via_rewrite:
        lines.append(
            "💡 已通过 LLM 改写 query 关键词后检索；上面内容**仍来自你的本地知识库**，"
            "LLM 只做翻译、不生成答案。"
        )
    else:
        lines.append(
            "💡 以上内容均直接来自你的本地知识库，**未经任何 LLM 改写**。"
            "请按来源文件名 + 页码回原文核对。"
        )
    if weak:
        lines.append("")
        lines.append(
            "⚠️ 命中分数偏低，结果可能不相关。试试更具体的关键词，"
            "或加 `/expand` 前缀强制 LLM 改写。"
        )
    return "\n".join(lines)


def _should_auto_rewrite(query: str, hits: list[dict]) -> bool:
    """
    Decide whether to automatically invoke query rewriting.

    Heuristics:
      - Skip rewriting for very short queries — they're usually already optimal.
      - Skip if BM25 had no hits — rewriting still helps, but only attempt
        if LLM is configured. (Caller checks that.)
      - Skip if top score is comfortably above threshold — BM25 nailed it.
    """
    if len(query.strip()) < config.REWRITE_MIN_QUERY_LEN:
        return False
    if not hits:
        return True  # zero hits — rewrite might help find a match
    top_score = hits[0]["score"]
    return top_score < config.REWRITE_SCORE_THRESHOLD


def _status_text() -> str:
    """Render the /status reply. Pulled out so handle_message stays linear."""
    stats = storage.corpus_stats()
    docs = storage.list_documents()
    if not docs:
        return "知识库为空。"
    scan_warnings = [d["filename"] for d in docs if d.get("scan_pages")]
    out = [
        "**知识库状态**",
        f"- 文档数：{len(docs)}",
        f"- 切片数：{stats['n_chunks']}",
        f"- 平均切片长度：{stats['avg_chunk_len']:.0f} 字符",
    ]
    if scan_warnings:
        out.append(f"- 扫描页警告：{len(scan_warnings)} 个文档")
    out.append(f"- 自动改写阈值：score < {config.REWRITE_SCORE_THRESHOLD}")
    return "\n".join(out)


def _dispatch_query(platform: str, text: str, force_expand: bool) -> str:
    """
    Core query pipeline: BM25 → (optional) rewrite → re-search → render.
    Pulled out of handle_message so the main function can wrap it with
    a single record-last-reply call (no more "did I remember to record
    at every return point?" footgun).
    """
    # Empty KB short-circuit.
    if not storage.list_documents():
        return _empty_help(platform)

    # First-pass BM25.
    hits = search_mod.search(text, top_k=config.DEFAULT_TOP_K)

    # Decide whether to auto-rewrite.
    if force_expand or _should_auto_rewrite(text, hits):
        rw = query_rewrite.rewrite(text)
        if rw.used_rewrite:
            new_hits = search_mod.search(rw.rewritten, top_k=config.DEFAULT_TOP_K)
            if new_hits:
                return _format_hits_markdown(
                    text,
                    new_hits,
                    via_rewrite=True,
                    rewritten_query=rw.rewritten,
                )
            # Rewrite produced keywords that still don't hit anything.
            if hits:
                return _format_hits_markdown(
                    text, hits,
                    via_rewrite=True,
                    rewritten_query=rw.rewritten,
                    weak=hits[0]["score"] < config.WEAK_HINT_THRESHOLD,
                )
            return (
                f"未在知识库中找到与 **{text}** 相关的内容。\n\n"
                f"_（已尝试 LLM 改写为 `{rw.rewritten}`，仍未命中。）_"
            )
        # rw.used_rewrite == False: LLM didn't produce a different keyword set.
        if force_expand:
            if rw.error:
                return (
                    f"⚠️ `/expand` 改写失败：`{rw.error}`\n\n"
                    "下面是原始 query 的检索结果：\n"
                ) + (
                    _format_hits_markdown(
                        text, hits,
                        weak=hits[0]["score"] < config.WEAK_HINT_THRESHOLD,
                    )
                    if hits
                    else _no_hits(text)
                )
            # LLM ran fine but kept the original — tell the user.
            note = (
                "ℹ️ LLM 评估后认为这个 query 已经够准，未做改写。\n\n"
            )
            if hits:
                return note + _format_hits_markdown(
                    text, hits,
                    weak=hits[0]["score"] < config.WEAK_HINT_THRESHOLD,
                )
            return note + _no_hits(text)
        # Auto-triggered but LLM kept original — silent no-op, fall through.
        # (User didn't ask for expand explicitly, so don't be chatty.)

    # No rewrite path. If we have hits, return them.
    if hits:
        weak = hits[0]["score"] < config.WEAK_HINT_THRESHOLD
        return _format_hits_markdown(text, hits, weak=weak)

    return _no_hits(text)


def handle_message(platform: str, raw_text: str, user_id: str = "") -> str:
    """
    Run a query against the KB and return a Markdown reply.

    Args:
        platform: human label for the platform ('wecom' / 'dingtalk' / 'feishu');
                  only used in the empty-KB help message and feedback records.
        raw_text: the user's message — typically their query string.
                  Slash commands like `/help`, `/status`, `/expand`, `/good`,
                  `/bad`, `/partial` are handled here so all platforms share
                  the same surface.
        user_id:  the platform's user ID (sender_id). Required for
                  /good /bad /partial to work; can be empty for read-only
                  queries (the bot will still answer, just can't accept
                  feedback).

    Returns:
        A Markdown string safe to send back to the IM client.
    """
    text = (raw_text or "").strip()

    # --- Slash commands — keep all platforms consistent. -----------------
    if text in ("/help", "help", "?", "？"):
        return _HELP_TEXT
    if text == "/status":
        return _status_text()
    if text in ("/good", "/bad", "/partial"):
        return _handle_feedback(platform, user_id, text[1:])

    # /expand prefix — force LLM query rewrite, then search.
    force_expand = False
    if text.startswith("/expand "):
        force_expand = True
        text = text[len("/expand "):].strip()

    if not text:
        return "请发送需要检索的关键词，或输入 `/help` 查看帮助。"

    # --- Main query dispatch. Record the reply for /good /bad /partial
    # to attach to. Recording AFTER the reply is computed means the
    # single record site covers every code path (including the empty
    # short-circuit, the no-hits case, and the rewrite path). ---------
    reply = _dispatch_query(platform, text, force_expand)
    _record_last_reply(platform, user_id, text, reply, message_type="text")
    return reply


# ---------------------------------------------------------------------------
# 场景 2: image input — VL OCR + BM25 + LLM synthesis (the ONE place LLM
# is allowed to "synthesize"). Hard-coded rules in `answer_synth.SYSTEM_PROMPT`
# ensure citation + no external knowledge.
# ---------------------------------------------------------------------------

def _format_image_reply(
    extracted_text: str,
    synth_answer: str,
    synth_used: bool,
    hits: list[dict],
    domain: int | None = None,
    domain_name: str | None = None,
    archived_new: bool = False,
) -> str:
    """
    Build a Markdown reply for an image input.

    Layout (always shown, even on failure):
      1. Domain tag (域N · 中文名) if classification succeeded — also
         a small archive marker if the question was newly archived.
      2. The text the VL model extracted from the image (truncated).
      3. The LLM synthesis (if valid), or the standard "未检索到" line.
      4. The raw BM25 hits (DEDUPED by content fingerprint) so the user
         can always cross-check the synthesis against source material.
    """
    lines: list[str] = []
    if domain is not None and domain_name:
        archive_note = " · 已归档" if archived_new else " · (已存在)"
        lines.append(f"### 🏷️ 域{domain} · {domain_name}{archive_note}")
    else:
        lines.append("### 🏷️ 域:_(未识别)_")
    lines.append("")

    lines.append("### 📷 识别的内容")
    snippet = extracted_text.strip().replace("\n", " ")
    if len(snippet) > 600:
        snippet = snippet[:600].rstrip() + "…"
    lines.append(f"> {snippet or '_(空)_'}")
    lines.append("")

    lines.append("### 🤖 综合回答")
    if synth_used:
        lines.append(synth_answer.strip())
    else:
        lines.append("_（LLM 综合未启用或失败,见下方原始资料）_")
    lines.append("")

    lines.append("### 📚 原始资料(请按文件名 + 页码回原文核对)")
    deduped = _dedupe_hits_for_display(hits)
    if not deduped:
        lines.append("_(无命中)_")
    else:
        for i, h in enumerate(deduped, start=1):
            page = f"· p.{h['page_num']}" if h.get("page_num") is not None else "· md"
            src = f"`{h['filename']}` {page}  ·  score={h['score']:.2f}"
            lines.append(f"**[{i}]** {src}")
            chunk = h["chunk_text"].strip().replace("\n", " ")
            if len(chunk) > 220:
                chunk = chunk[:220].rstrip() + "…"
            lines.append(f"> {chunk}")
            lines.append("")

    lines.append("---")
    lines.append(
        "🔒 综合回答由 LLM 基于上方【原始资料】生成,**每条论断都应带 [来源: ...] 标注**;"
        "若 LLM 引用了资料外的内容,请忽略该部分并以【原始资料】为准。"
    )
    return "\n".join(lines)


def _dedupe_hits_for_display(hits: list[dict], max_display: int = 3) -> list[dict]:
    """
    Dedupe BM25 hits for cleaner display.

    Question-bank PDFs get chunked such that the same question +
    options + answer + explanation ends up in 3-5 overlapping chunks
    (because CHUNK_SIZE=400 < full question length ~600 chars).
    BM25 then scores all of them highly, and the user sees the same
    paragraph N times.

    Two-pass dedup:
      1. Keep the highest-scoring chunk per (filename, page_num). A
         question spanning 2 pages is still 2 distinct hits, which
         is what we want.
      2. Then dedup remaining by content prefix (first 200 chars),
         to collapse near-identical chunks that landed on the same
         page (e.g. from CHUNK_OVERLAP=60).
      3. Cap at `max_display` to keep the reply readable.

    NOTE: this is a display-layer fix, not a chunking fix. The chunks
    themselves are still duplicated in the KB; the right long-term
    answer is to bump CHUNK_SIZE in config.py + rebuild, but that
    affects all documents. Display dedup is safe and instant.
    """
    if not hits:
        return []

    # Pass 1: one hit per (file, page) — keep highest score
    by_page: dict[tuple, dict] = {}
    for h in hits:
        key = (h.get("filename", ""), h.get("page_num"))
        if key not in by_page or h["score"] > by_page[key]["score"]:
            by_page[key] = h

    # Pass 2: dedup by content prefix (200 chars) within page-grouped hits
    seen_prefixes: set[str] = set()
    final: list[dict] = []
    for h in sorted(by_page.values(), key=lambda x: -x["score"]):
        prefix = (h.get("chunk_text") or "").strip().replace("\n", " ")[:200]
        if prefix in seen_prefixes:
            continue
        seen_prefixes.add(prefix)
        final.append(h)
        if len(final) >= max_display:
            break

    return final


def _split_for_im(reply: str, max_len: int = 3800) -> list[str]:
    """
    Split a long Markdown reply into multiple IM-sendable chunks.

    Background
    ----------
    DingTalk Markdown messages are silently truncated by the SDK (or
    the gateway) at ~4000 chars. Feishu `text` msg_type similarly caps
    at 4000. A long image reply (synth + 3+ deduped hits) can easily
    exceed that.

    Strategy
    --------
    The image-reply Markdown has a clear separator:
        ...synth...
        ---
        🔒 disclaimer
    Split on the FIRST `\n---\n` if both halves are under max_len.
    Otherwise fall back to per-paragraph splitting (with a "..." marker
    if the result still exceeds max_len).

    Returns a list of 1+ strings. The bot caller sends each in order.
    """
    if len(reply) <= max_len:
        return [reply]

    # Try the natural split at the disclaimer separator.
    sep = "\n---\n"
    idx = reply.find(sep)
    if idx > 0:
        head = reply[: idx + 1].rstrip()      # keep the closing "---" on head
        tail = reply[idx + len(sep) :]
        if len(head) <= max_len and len(tail) <= max_len:
            # Add a "continued ↓" hint to the first chunk so the user
            # knows there's a follow-up.
            if not head.endswith("\n"):
                head += "\n"
            return [head + "\n_（续 ↓）_", tail]

    # Fallback: split at paragraph boundaries.
    chunks: list[str] = []
    current = ""
    for line in reply.split("\n"):
        if len(current) + len(line) + 1 > max_len and current:
            chunks.append(current)
            current = line
        else:
            current = current + "\n" + line if current else line
    if current:
        chunks.append(current)

    # Last-resort: if any single chunk is still too long, hard-truncate
    # it with a marker so the user knows it's been clipped.
    final: list[str] = []
    for c in chunks:
        if len(c) > max_len:
            final.append(c[: max_len - 30].rstrip() + "\n\n_…（已截断）_")
        else:
            final.append(c)
    return final


# ---------------------------------------------------------------------------
# Domain classification (added 2026-09-03)
# ---------------------------------------------------------------------------

_DOMAIN_NAMES: dict[int, str] = {
    1: "安全与风险管理",
    2: "资产安全",
    3: "安全架构与工程",
    4: "通信与网络安全",
    5: "身份与访问管理",
    6: "安全评估与测试",
    7: "安全运营",
    8: "软件开发安全",
}


def _classify_domain_via_llm(text: str) -> int | None:
    """Ask the configured LLM to map an English CISSP question to one
    of the 8 domains. Returns 1..8 or None on any failure.

    Why a separate LLM call instead of keyword matching: questions are
    paraphrased and cover many sub-topics; a small classifier prompt
    generalizes better than hand-tuned rules. We reuse the same Anthropic
    client + config as answer_synth so no new credentials needed.
    """
    if not text or not text.strip():
        return None
    try:
        import llm_config
        import anthropic
    except ImportError:
        return None
    if not llm_config.is_llm_configured():
        return None
    try:
        cfg = llm_config.load_llm_config()
        client = anthropic.Anthropic(
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            timeout=cfg.timeout_s,
            max_retries=0,
        )
        domain_list = "\n".join(f"{n}. {name}" for n, name in _DOMAIN_NAMES.items())
        resp = client.messages.create(
            model=cfg.model,
            max_tokens=8,
            messages=[{
                "role": "user",
                "content": (
                    "你是一个 CISSP 题目分类器。下面是 8 个域:\n"
                    f"{domain_list}\n\n"
                    "阅读用户给出的英文题目,只回复一个 1-8 的数字,"
                    "代表这道题最相关的域。不要任何解释、标点、换行。\n\n"
                    f"题目:\n{text[:1500]}"
                ),
            }],
        )
        # Concatenate all text blocks defensively.
        out = "".join(
            getattr(b, "text", "")
            for b in resp.content
            if getattr(b, "type", None) == "text"
        ).strip()
        # Pick the first digit 1-8 we see.
        for ch in out:
            if ch in "12345678":
                return int(ch)
        logger.info("classify_domain: LLM returned no digit, raw=%r", out[:60])
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("classify_domain failed: %s", e)
        return None


def _archive_if_new(question_text: str, domain: int | None, source: str) -> tuple[int | None, bool]:
    """Archive the (English) question text. Returns (row_id_or_none, archived_bool).
    - row_id_or_none: the new id if newly inserted, None if already existed
    - archived_bool: True if we actually inserted (False if dedup or no domain)
    """
    if domain is None or not question_text.strip():
        return None, False
    try:
        row_id = storage.archive_question(question_text, domain, source)
    except Exception as e:  # noqa: BLE001
        logger.warning("archive_question failed: %s", e)
        return None, False
    return row_id, row_id is not None


def _format_practice_reply(
    extracted_text: str,
    domain: int | None,
    domain_name: str | None,
    archive_result: dict,
) -> str:
    """Reply shown in the IM for the practice (no-answer) flow.

    Layout (kept minimal — the user wants to do the question themselves):
      1. Domain tag (域N · 中文名) + archive state
      2. The English question the bot extracted (so the user can sanity
         check the OCR before they start working on the saved file)
      3. The Chinese translation the bot archived (so the user can
         read it in-chat without opening the saved file)
    """
    lines: list[str] = []
    if domain is not None and domain_name:
        if archive_result.get("is_new"):
            archive_note = "已归档"
        elif archive_result.get("path"):
            archive_note = "已存在(未重复保存)"
        else:
            archive_note = "归档失败(LLM 不可用?)"
        lines.append(f"### 🏷️ 域{domain} · {domain_name} · {archive_note}")
    else:
        lines.append("### 🏷️ 域:_(未识别)_")
    lines.append("")

    # Show the file path so the user can find it on disk.
    path = archive_result.get("path")
    if path:
        lines.append(f"📁 `{path}`")
        lines.append("")

    snippet = extracted_text.strip().replace("\n", " ")
    if len(snippet) > 800:
        snippet = snippet[:800].rstrip() + "…"
    lines.append("### 📷 英文原题")
    lines.append(f"> {snippet or '_(空)_'}")
    lines.append("")

    zh_text = (archive_result.get("zh_text") or "").strip()
    if zh_text:
        lines.append("### 🀄 中文翻译")
        # Same 800-char cap on the in-chat snippet so we don't blow past
        # the IM message size limit on long questions.
        zh_snippet = zh_text.replace("\n", " ")
        if len(zh_snippet) > 800:
            zh_snippet = zh_snippet[:800].rstrip() + "…"
        lines.append(f"> {zh_snippet}")
        lines.append("")

    lines.append("---")
    lines.append("_已存档,自行练习(不发答案)。完整中文在文件里。_")
    return "\n".join(lines)


def handle_image(platform: str, image_path: str, user_id: str = "") -> str:
    """
    Image input entry point (场景 2 — practice mode, 2026-09-03 redesign).

    The user does NOT want the bot to give the answer. They want to
    self-study: read the English question, attempt it themselves, then
    verify against the Chinese translation that the bot archived.

    Pipeline:
      1. image_extract.extract_text()  → user's question as text
      2. classify_domain(text)         → CISSP domain 1..8 (best effort)
      3. question_archive.save_question → write data/questions/域N/<ts>-<hash>.md
                                          with English + Chinese translation,
                                          dedup by normalized text
      4. Format minimal reply: domain tag + saved file path + English snippet

    No BM25 search, no answer synthesis. The user gets nothing but
    the question and the path to the archived file.

    The reply is also recorded for /good /bad /partial feedback (so
    we can spot OCR vs domain-classification regressions).
    """
    # 1. OCR / VL understanding of the image.
    extracted = image_extract.extract_text(image_path)
    if not extracted:
        reply = (
            "📷 图片识别失败,可能原因:\n"
            "- 格式不支持(支持 jpg / png / gif / webp)\n"
            "- VL 凭证缺失或网络错误\n"
            "- 图片中无文字\n\n"
            "请改用文字直接发送,或换张图重试。"
        )
        _record_last_reply(
            platform, user_id, "[image]", reply, message_type="image",
        )
        return reply

    # 2. Classify the question into one of the 8 CISSP domains
    #    (best effort — LLM call may fail or be unconfigured).
    domain = _classify_domain_via_llm(extracted)
    domain_name = _DOMAIN_NAMES.get(domain) if domain else None
    if domain is None:
        logger.info("image: domain classification skipped/failed")

    # 3. Archive + translate + write to per-domain folder.
    #    save_question() handles dedup internally; is_new tells us
    #    whether to show "已归档" or "已存在".
    source = f"{platform}:{user_id}" if user_id else platform
    import question_archive
    archive_result = question_archive.save_question(
        en_text=extracted,
        domain=domain if domain is not None else -1,  # -1 = skip
        source=source,
    )
    logger.info(
        "image: archive result is_new=%s path=%s",
        archive_result.get("is_new"),
        archive_result.get("path"),
    )

    # 4. Minimal reply — no answer, no KB, no synthesis.
    reply = _format_practice_reply(
        extracted_text=extracted,
        domain=domain,
        domain_name=domain_name,
        archive_result=archive_result,
    )
    _record_last_reply(
        platform, user_id, extracted[:200], reply, message_type="image",
    )
    return reply