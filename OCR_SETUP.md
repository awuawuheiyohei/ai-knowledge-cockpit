# OCR 配置指南 · 扫描页自动识别

**适用场景**：你丢进 `inbox/` 的 PDF 里有扫描件（图片型 PDF，pymupdf 提取不到文字）。

没有 OCR 之前，扫描页会被标记为 `partial` 并报警告。
开启 OCR 后，扫描页会被自动识别成文本，正常入库、可检索。

---

## 工作原理

```
扫描页 PDF 入库
  ↓
pdf_extract 提取文字 → 该页没文字 → 标记 scanned
  ↓
如果 --ocr：渲染扫描页为 PNG (DPI 200)
  ↓
调 MiniMax-M3（多模态 LLM）识图，返回原文
  ↓
识别出来的文本当作该页内容入库
  ↓
chunks 表标记 via_ocr=1（用于审计）
documents 表记录 ocr_pages=[N, ...]
```

**审计不变量**：
- LLM **只用来** 把图片转成文字，不做任何总结或改写
- OCR prompt 显式禁止总结、改写、翻译、评论
- 每条 chunk 都带 `via_ocr` 标记，方便审计哪些内容是 LLM 介入产生的

---

## 配置步骤

### 1. 申请 MiniMax API Key

去 https://platform.minimaxi.com 注册 / 登录。

注意 MiniMax 现在的多模态 API 走 **Anthropic 兼容格式**，不是 OpenAI 格式：
- 端点：`https://api.minimaxi.com/anthropic`
- 模型：`MiniMax-M3`（M2.x **不支持**图片）
- 用 `anthropic` Python SDK 调用

### 2. 配置环境变量

```bash
export VL_API_KEY='eyJhbGciOiJSUzI1NiI...'  # 你的 MiniMax API key
# 以下都有默认值，一般不用改
export VL_BASE_URL='https://api.minimaxi.com/anthropic'
export VL_MODEL='MiniMax-M3'
export VL_DPI='200'           # 渲染 DPI
export VL_TIMEOUT_S='60'      # 单页超时
export VL_MAX_TOKENS='2000'   # 单页输出上限
```

把上面 export 写进 `~/.zshrc`（macOS）或 `~/.bashrc` 持久化。
**不要**写进项目 `.env`（避免泄露）。

### 3. 安装新依赖

```bash
.venv/bin/pip install -r requirements.txt
```

新加了 `anthropic`。

### 4. 跑带 OCR 的入库

```bash
# 不带 --ocr：扫描页标记 partial + 警告（不消耗 token）
python app.py ingest inbox/foo.pdf

# 带 --ocr：扫描页自动 OCR 入库（消耗 token）
python app.py ingest inbox/foo.pdf --ocr

# 目录递归
python app.py ingest inbox/ --recursive --ocr
```

### 5. 查看 OCR 用量

```bash
python app.py status
```

会显示：
```
OCR'd          : 12 pages across 3 docs
```

---

## 默认行为：**OCR 默认关**

`ingest --ocr` 是**显式开启**的，不会默默烧 token。理由：
- OCR 一页 MiniMax-M3 约 0.001-0.005 元（输入 1元/百万 tokens，输出 8元/百万 tokens）
- 一本 500 页扫描书大约 0.5-2.5 元
- 大多数人只想先看 KB 里有什么，再决定要不要 OCR 剩余的扫描页

不传 `--ocr` 的行为：扫描页标记 `partial`，提示用户**用 `--ocr` 重跑**。

---

## VL_API_KEY 同时启用 query 改写

同一个 `VL_API_KEY` 还驱动一个功能：**query 改写**（`query_rewrite.py`）。

当你在钉钉/企微发了一条**口语化/模糊的查询**（比如"用户能用什么密码登录"），系统会自动用 LLM 把这句话改写为检索关键词（"认证 多因素 密码 因素"），再用 BM25 查。改写后的结果会标注 `↳ 自动改写为`。

**触发条件**（自动）：
- BM25 零命中 或 top-1 score < **2.0**（可在 `config.py` 调）
- query 长度 ≥ 4 字符（避免"PKI"这种已经精准的词被改写）

**强制改写**：发消息时加 `/expand` 前缀，无视 BM25 命中多强都改写：
```
/expand 用户能用什么密码登录
```

**硬规则**（这条不会破）：
- LLM **只**用来改写 query 关键词，**绝不**生成答案
- LLM **看不到 KB**，只能根据用户输入改写
- 用户看到的最终答案仍是 KB 原文 + source 标注
- `LLM_API_KEY` > `VL_API_KEY` > 报错，优先级明确

---

## 常见问题

**Q：MiniMax-VL-01 不是专门做 OCR 吗？为什么用 M3？**
A：M3 是 MiniMax 当前最新的"原生多模态"模型，OCR 质量很好。M2.x 不支持图片。VL-01 是 2025 年 1 月发布的旧模型，已被 M3 取代。

**Q：OCR 失败的页怎么办？**
A：保留为扫描页警告（`scan_pages` 列表里），不影响其他页。你可以稍后重跑 `ingest --ocr`，或者检查 VL API key 是不是有问题。

**Q：能换其他 VL 模型吗？**
A：当前实现默认是 MiniMax-M3 via Anthropic SDK。要换其他模型（比如 Qwen-VL），需要改 `pdf_ocr._call_vl` 的实现。架构上预留了 `vl_config.VlConfig`，加新引擎不复杂。

**Q：会不会很慢？**
A：每页串行调用 MiniMax API，约 2-5 秒/页。一本 500 页的书大约 15-40 分钟。可以接受。

**Q：OCR 出来的文本质量怎么样？**
A：印刷体 PDF（不是手写）的识别率 95%+。表格、特殊符号也能识别。扫描质量差（倾斜、有水印）会下降。

**Q：可以批量并发吗？**
A：当前实现是串行（稳定优先）。如果你的 KB 量大，可以加并发——改 `pdf_extract.extract_pdf` 里的 OCR 循环。**注意**：并发会加速消耗 token。

---

## 价格参考（2025 年 MiniMax 标准定价）

| 模型 | 输入 | 输出 |
|------|------|------|
| MiniMax-M3 | 1元/百万 tokens | 8元/百万 tokens |

一张扫描页（DPI 200）约 1500-3000 输入 tokens，识别出 500-2000 字符。
**单页成本**：约 0.001-0.005 元。
**500 页书**：约 0.5-2.5 元。

比人工 OCR / 商业 OCR 服务（按页收费几毛钱）便宜几个数量级。

---

## 钉钉 / 企微里 OCR 文档会自动用

OCR 入库后的文本和原生文本**完全等同对待**——钉钉和企微的查询路径走 `im_router.handle_message()`，里面调 BM25 检索，不区分文本来源。

用户不会感知到内容是 OCR 来的，除非用 CLI 看 `documents.ocr_pages` 字段。