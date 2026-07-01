"""
query_rewrite.py — Turn colloquial user queries into retrieval-friendly
keyword strings via an LLM (default MiniMax-M3).

Why
---
BM25 is literal text matching. Users often type colloquially
("用户能用什么密码登录", "忘了那个认证的东西") which doesn't match KB
headings. We delegate query reformulation to an LLM, but **strictly**:

Hard rules
----------
- The LLM sees ONLY the user's raw query string. It never sees the KB.
- The LLM is instructed to output a short keyword list, not an answer.
- The LLM's output is fed back into BM25. The user still sees only KB
  excerpts with source citations — never the LLM's own text.
- If the LLM call fails or returns nothing useful, we fall back to the
  original query and surface a note that rewrite was skipped.

This means query rewriting is essentially a "translator" from natural
language → search terms. The LLM has zero opportunity to hallucinate
about KB content.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import llm_config


logger = logging.getLogger("query_rewrite")


SYSTEM_PROMPT = (
    "你是一个「检索关键词改写器」。你的唯一任务：把用户口语化或模糊的问题，"
    "重新表述为适合在中文技术知识库里做关键词检索的术语列表。\n\n"
    "严格规则：\n"
    "1. 只输出 3-7 个关键词或短术语，用空格分隔。\n"
    "2. 不要回答问题，不要解释，不要总结。\n"
    "3. 不要补充你已有的知识——你看不到任何文档，只能根据用户输入改写。\n"
    "4. 如果用户输入已经是精准关键词，原样输出即可。\n"
    "5. 使用中文；专有名词（如 PKI、RBAC、CIA）保留英文。\n\n"
    "示例：\n"
    "输入：「用户能用什么密码登录」\n"
    "输出：「认证 授权 密码 多因素 单因素 生物识别」\n\n"
    "输入：「忘了那个认证的东西叫什么」\n"
    "输出：「AAA 认证 身份验证 因素」\n\n"
    "输入：「PKI」\n"
    "输出：「PKI」"
)


@dataclass
class RewriteResult:
    original: str
    rewritten: str           # the keyword string used to re-search
    used_rewrite: bool       # False if we returned original unchanged
    error: str | None = None # set if LLM call failed


class RewriteError(Exception):
    """Raised when the rewrite call fails unrecoverably."""


def _build_client(cfg: llm_config.LlmConfig):
    try:
        import anthropic
    except ImportError as e:
        raise RewriteError("anthropic SDK not installed") from e
    return anthropic.Anthropic(
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        timeout=cfg.timeout_s,
        max_retries=0,
    )


def _call_llm(cfg: llm_config.LlmConfig, query: str) -> str:
    """Single LLM call → raw response text."""
    client = _build_client(cfg)
    response = client.messages.create(
        model=cfg.model,
        max_tokens=cfg.max_tokens,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": query},
                ],
            }
        ],
    )
    parts: list[str] = []
    for block in response.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "".join(parts).strip()


_KEYWORD_CLEAN_RE = re.compile(r"[^\w\s\-+#.]+", re.UNICODE)


def _sanitize(raw: str) -> str:
    """
    Clean the LLM's raw text into a usable keyword string.

    - Drop newlines, punctuation (except hyphen/underscore for terms like
      multi-factor), and quotes the LLM sometimes wraps the answer in.
    - Collapse whitespace.
    - Cap length to ~80 chars to avoid runaway output.
    """
    if not raw:
        return ""
    # Strip common chatty wrappers the LLM sometimes adds despite the prompt.
    for prefix in ("输出：", "答案：", "改写：", "keywords:", "Keywords:"):
        if raw.lower().startswith(prefix.lower()):
            raw = raw[len(prefix):].strip()
    # Drop everything in code-fence markers if any.
    raw = raw.strip("`").strip()
    # Drop surrounding quotes.
    raw = raw.strip().strip('"').strip("'").strip()
    # Collapse internal punctuation that BM25 doesn't care about.
    raw = _KEYWORD_CLEAN_RE.sub(" ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw[:120]


def rewrite(query: str) -> RewriteResult:
    """
    Rewrite `query` into keyword form via the configured LLM.

    On any failure, returns the original query unchanged with
    `used_rewrite=False` and `error` populated. Callers should treat that
    case as "rewrite skipped, use original".
    """
    q = (query or "").strip()
    if not q:
        return RewriteResult(original=q, rewritten=q, used_rewrite=False)

    if not llm_config.is_llm_configured():
        return RewriteResult(
            original=q,
            rewritten=q,
            used_rewrite=False,
            error="LLM not configured (LLM_API_KEY / VL_API_KEY missing)",
        )

    try:
        cfg = llm_config.load_llm_config()
        raw = _call_llm(cfg, q)
    except Exception as e:
        logger.warning("query rewrite failed for %r: %s", q, e)
        return RewriteResult(
            original=q,
            rewritten=q,
            used_rewrite=False,
            error=str(e),
        )

    cleaned = _sanitize(raw)
    if not cleaned or cleaned.lower() == q.lower():
        # LLM didn't produce anything different — treat as no-op.
        return RewriteResult(original=q, rewritten=q, used_rewrite=False)

    logger.info("query rewrite: %r -> %r", q, cleaned)
    return RewriteResult(original=q, rewritten=cleaned, used_rewrite=True)