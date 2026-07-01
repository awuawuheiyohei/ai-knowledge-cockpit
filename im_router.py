"""
im_router.py — Single chokepoint between IM platforms and the KB.

Both the WeCom and DingTalk adapters call `handle_message()` with raw
user text. We run BM25 against the KB and format a Markdown reply.

Hard rule: no LLM is called here... EXCEPT for query rewriting, which is
strictly opt-in per call (auto when BM25 is weak, force via /expand) and
strictly scoped to reformulating the user's query string into keywords.
The LLM never sees the KB and never produces the final answer — the user
still sees only KB excerpts with source citations.
"""
from __future__ import annotations

import config
import search as search_mod
import storage
import query_rewrite


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
        "可能的原因：\n"
        "- 用词太专业 / 太长 → 换个近义词试试\n"
        "- 该知识点还没入库 → 跑 `python app.py ingest ...`\n"
        "- 该知识点所在页面是扫描件 → 用 OCR 工具转 .md 后再入库"
    )


def _format_hits_markdown(
    query: str,
    hits: list[dict],
    *,
    via_rewrite: bool = False,
    rewritten_query: str | None = None,
    weak: bool = False,
) -> str:
    """Format hits as Markdown with source attribution per chunk."""
    lines: list[str] = []
    if via_rewrite and rewritten_query and rewritten_query != query:
        lines.append(f"### 🔎 检索：{query}")
        lines.append(f"↳ *自动改写为*：**{rewritten_query}**")
    else:
        lines.append(f"### 🔎 检索：{query}")
    lines.append(f"命中 {len(hits)} 条（按相关度排序）：\n")
    for i, h in enumerate(hits, start=1):
        page = f"· p.{h['page_num']}" if h.get("page_num") is not None else "· md"
        src = f"`{h['filename']}` {page}  ·  score={h['score']:.2f}"
        lines.append(f"**[{i}]** {src}")
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


def handle_message(platform: str, raw_text: str) -> str:
    """
    Run a query against the KB and return a Markdown reply.

    Args:
        platform: human label for the platform ('wecom' / 'dingtalk'); only
                  used in the empty-KB help message.
        raw_text: the user's message — typically their query string.
                  Slash commands like `/help`, `/status`, `/expand` are
                  handled here so both platforms share the same surface.

    Returns:
        A Markdown string safe to send back to the IM client.
    """
    text = (raw_text or "").strip()

    # Slash commands — keep both platforms consistent.
    if text in ("/help", "help", "?", "？"):
        return (
            "**AI Knowledge Cockpit · 帮助**\n\n"
            "- 直接发送关键词，我会检索本地知识库并附来源\n"
            "- 输入模糊、口语化也没事——我会在 BM25 弱命中时**自动用 LLM 改写 query** 再查\n"
            "- 加 `/expand` 前缀强制改写（即使 BM25 强命中）\n"
            "- `/status` 查看知识库统计\n"
            "- `/help`  查看本帮助\n\n"
            "**硬规则**：\n"
            "- LLM 只用来改写 query，**绝不**生成答案\n"
            "- 最终答案仍来自 KB 原文 + 来源标注\n"
            "- 没找到就说没找到，不会编"
        )
    if text == "/status":
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

    # /expand prefix — force LLM query rewrite, then search.
    force_expand = False
    if text.startswith("/expand "):
        force_expand = True
        text = text[len("/expand "):].strip()

    if not text:
        return "请发送需要检索的关键词，或输入 `/help` 查看帮助。"

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