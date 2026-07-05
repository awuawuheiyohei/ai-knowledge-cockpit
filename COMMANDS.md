# COMMANDS.md — AI Knowledge Cockpit 命令速查

> 给"备考 CISSP 的自己"用的命令手册。打开终端,照着抄就行。
> 项目哲学:**不喧宾夺主** —— 命令都设计成 ≤3 秒能跑完,不消耗注意力。

---

## 0. 一行启动(懒人入口)

> 不读手册也能上手。两个子命令搞定:

```bash
./quickstart.sh check      # 环境体检:venv / .env / IM 凭证 / KB 状态都给你看一眼
./quickstart.sh serve      # init + ingest inbox/ + ingest notes/ + 起 DingTalk bot
./quickstart.sh serve cli  # 不起 bot,跑完 ingest 后直接打印 status
```

规则(快速版):
- `serve` 默认起 **DingTalk**(免公网,推荐)。想换 `./quickstart.sh serve wecom`。
- **绝不自动 OCR** —— `--ocr` 才会调 VL API(花钱)。需要时:`./quickstart.sh serve --ocr dingtalk`。
- **绝不自动改 `.env`** —— 凭证缺了就告诉你填哪个变量。
- 想了解每一步在干嘛 → 继续往下读"1. 一次性启动"。

---

## 1. 一次性启动(只需要做一次)

```bash
# 1. 装依赖(已经做过就跳过)
python -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. 初始化 DB 和目录
.venv/bin/python app.py init

# 3. 第一次入库(整个 inbox 和 notes 全进)
.venv/bin/python app.py ingest inbox/ --recursive
.venv/bin/python app.py ingest notes/ --recursive
```

> 之后只要 KB 文件没坏,就不用再 init。

---

## 2. 来了新 PDF 怎么办(常用)

```bash
# 1. 丢进 inbox/
cp /path/to/new-chapter.pdf inbox/

# 2. 入库
.venv/bin/python app.py ingest inbox/new-chapter.pdf

# 3. 看有没有扫描页失败
.venv/bin/python app.py status

# 4. 抽检能不能搜到
.venv/bin/python app.py search "那个章节的关键概念" --top 3
```

**批量入库**(适合"我攒了一堆 PDF"):

```bash
.venv/bin/python app.py ingest inbox/ --recursive
```

> 已经入库的文件会被自动跳过(`file_hash` dedupe),所以反复跑没成本。

---

## 3. 扫描版 PDF(全是图片,要 OCR)

`status` 里的 `Scan warnings: pages [...]` → 那些页文字提取失败,得 OCR 才行。

```bash
# 单文件 OCR —— 调 VL API,会花 token / 钱
.venv/bin/python app.py ingest inbox/scanned.pdf --ocr
```

**不要默认开 `--ocr`**:OCR 既花钱又花时间。先 `status` 看页数和扫描比例,再决定要不要。

域 1-8 的 PDF 和 4 套综合测试卷在仓库里基本都是扫描版,过 `--ocr` 才能进 KB。

---

## 4. 日常检索

```bash
# 全 KB 搜
.venv/bin/python app.py search "PKI 数字证书" --top 5

# 限定到某文档
.venv/bin/python app.py search "Kerberos" --doc "OSG9中文版-上册.pdf" --top 3

# 按域搜:先决定想找哪个域,看 status 的 8 域覆盖,再挑文件名
.venv/bin/python app.py search "安全评估方法" --doc "第15章-安全评估与测试-知识点.pdf" --top 5
```

`search` 出来的每条都带文件名 + 页码,直接对照书看就行。

---

## 5. 用钉钉 / 企微(更省事)

```bash
# 推荐:DingTalk Stream 模式,不要公网
.venv/bin/python app.py serve dingtalk

# 或:企微,需要公网回调(ngrok)
.venv/bin/python app.py serve wecom
```

直接发消息:
- `PKI 是什么` → BM25 命中直接回
- `忘了认证框架叫什么` → 自动 query rewrite
- `/expand 用户能用什么密码登录` → 强制走 LLM 改写
- `/help` / `/status` → 看命令清单 / KB 状态

见 `IM_SETUP.md` 配钉钉机器人 / 企微回调。

---

## 6. 维护

```bash
.venv/bin/python app.py list              # 列已入库的所有文档
.venv/bin/python app.py status            # DB 状态 + 扫描警告 + 8 域覆盖度
.venv/bin/python app.py remove "xxx.pdf"  # 按文件名删
.venv/bin/python app.py remove 23         # 也可按 ID 删
.venv/bin/python app.py rebuild           # 重建 BM25 索引(出问题才用)
```

**什么时候需要 `rebuild`?**
- 看到 `search` 明显不灵、但 `list` 显示有文档
- 直接改过 DB(很少见)
- 平时**不要**天天跑。

---

## 7. 排错

| 现象 | 看啥 | 解决 |
|------|------|------|
| ingest 跑了但文档数没变 | 文件 hash 一样 = 同一份 | 改文件名 / `remove` 旧的再 ingest |
| `Scan warnings` 一大堆,0 chunks | PDF 是扫描版,文本提取不出来 | 加 `--ocr` 重 ingest |
| search 啥都不返回 | query 太偏 / 拼错 | IM 里加 `/expand` 改写;终端试更短/更长的 query |
| search 分数普遍很低 | BM25 对口语化 query 弱 | 短 query 直接命中;口语用 `/expand` |
| OSG9/10 完全没入库 | 文件名带空格 / 中文编码 | `ls inbox/ | cat` 先确认文件名 |
| WebSocket 起不来 | Im 端口被占 / 缺 .env | 看启动报错;`.env` 漏配会让 bot 直接退 |

---

## 8. 速查表

```text
app.py init                  初始化 DB + 目录(只做一次)
app.py ingest X              入库单个文件
app.py ingest X --recursive  递归入库整个目录
app.py ingest X --ocr        入库时跑 VL OCR(扫描页才用)
app.py list                  列所有已入库文档
app.py search "<query>"      关键词检索
    --top N                  top N 条,默认 5
    --doc NAME               限定到单个文档
app.py remove NAME|ID        按文件名或 ID 删
app.py rebuild               重建 BM25 索引(出问题时)
app.py status                DB 状态 + 扫描警告 + 8 域覆盖度
app.py serve dingtalk        起钉钉 Stream 模式 bot
app.py serve wecom           起企微回调(需要 ngrok)
```

---

## 9. 不喧宾夺主(本项目最重要的原则)

> **KB 是备考的辅助工具,不是备考本身。**

- ❌ 不用 LLM 帮你"总结"PDF —— 绕过了输入,得不偿失
- ❌ 不用 AI 自动出模考题 —— 题源用 OSG 配套、Sybex 模考软件、ISC2 官方题
- ❌ 不做花哨的 dashboard / 进度条 —— 维护它的时间属于"刷 CISSP 题"
- ✅ **每天 5 分钟写 `notes/weak-points.md`** —— 你学习时易错的术语,记进来,自动入 KB
- ✅ **`status` 看一眼 8 域覆盖度** —— 弱域去补料
- ✅ **钉钉随口问一句忘了的概念** —— 比翻 OSG 快 10 倍

### 推荐的每日流程

1. 看书 / 看视频(主菜)
2. 写几条 `notes/weak-points.md`(事后 5 分钟)
3. 偶尔钉钉搜一个忘了的概念
4. 综合测试卷模考按周来,不依赖 KB
