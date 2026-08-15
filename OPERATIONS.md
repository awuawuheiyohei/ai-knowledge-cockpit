# Operations — 一键手册

> 这份手册只讲"怎么开 / 怎么关 / 怎么看",不讲原理。
> 原理看 [`README.md`](./README.md) + `IM_SETUP.md` + `OCR_SETUP.md`。

---

## 🚀 一键开 / 关 / 看

| 你想 | 命令 | 备注 |
|---|---|---|
| **开** bot + 刷题 app | `make up` | 走 launchd,后台,关终端不挂 |
| **关** | `make down` | 停 launchd |
| **看状态** | `make status` | 服务存活 + exam `/api/health` + KB 概况 |
| **看日志** | `make logs` | 最近 30 行 bot + exam |
| **重启** | `make restart` | bot + exam 都重新拉起 |

> 跑 `make up` 之后:钉钉里发消息就开始用了;浏览器开 **http://127.0.0.1:5001/** 刷题。

---

## 🛠 一次性安装(新机器 / 重建后)

```bash
# 1. 进入项目
cd /Users/jiangwenrui/Downloads/mass/ai_knowledge_cockpit

# 2. 装依赖
make install

# 3. 初始化 KB
.venv/bin/python app.py init

# 4. (可选)把 PDF / Markdown 丢进 inbox/ 再 ingest
.venv/bin/python app.py ingest inbox/your.pdf

# 5. 一键起服务
make up
```

---

## 🎛 单独组件

| 想跑哪个 | 命令 | 用途 |
|---|---|---|
| 钉钉 bot(前台) | `make run-bot` | 调试用,Ctrl+C 退出 |
| 飞书 bot(前台) | `make run-feishu` | 同上 |
| 企微 bot(前台) | `make run-wecom` | 同上 |
| 刷题 app(前台) | `make run-exam` | 跑在 5001 |

> 前台模式适合调试。生产用 `make up` 走 launchd。

---

## 🔍 排错 / 体检

| 你想 | 命令 |
|---|---|
| 看 KB 状态 | `.venv/bin/python app.py status` |
| 跑单元测试 | `make test` |
| 跑 + coverage | `make test-cov` |
| 安全审计 | `.venv/bin/python tools/security_audit.py` |
| KB 1:1 audit | `.venv/bin/python tools/audit_kb.py` |
| OCR 准确率抽检 | `.venv/bin/python tools/verify_ocr.py --samples 3` |
| 重建 KB 索引 | `.venv/bin/python app.py rebuild` |

---

## 🩹 常见操作

### 装新文档进 KB
```bash
# 单文件
.venv/bin/python app.py ingest inbox/foo.pdf

# 整个目录
.venv/bin/python app.py ingest notes/ --recursive
```

### 删除某条记录
```bash
# 按文件名
.venv/bin/python app.py remove "foo.pdf"
# 按 ID
.venv/bin/python app.py remove 42
```

### OCR 失败的页重新跑
```bash
.venv/bin/python app.py rebuild --rechunk --only-ocr
```

### 备份 KB
```bash
bash scripts/backup.sh    # 把 data/kb.sqlite 拷到 backups/
```

---

## ⌨️ Bot 里的命令(在 IM 客户端对 bot 发)

| 命令 | 作用 |
|---|---|
| 任意关键词 / 短句 | BM25 检索,返回带 source 链接的 hits |
| `/good` | 给上一条回答点赞(写到 `data/feedback/`) |
| `/bad` | 给上一条回答点踩,触发排查 |
| `/partial` | 部分对,只对一半 |
| `/expand <query>` | 强制走 LLM query rewrite,再查 |
| `/help` | 看可用命令 |
| `/status` | bot 端的小状态页 |

发图片 → 走 VL OCR 提取文字 → 拿文字去 KB 搜 → LLM 总结(带 `[来源: ...]` 引用)。

---

## 📍 关键路径

| 是什么 | 在哪 |
|---|---|
| KB 数据库 | `data/kb.sqlite` |
| 源文件镜像 | `data/originals/` |
| 错题 + 用户反馈 | `data/feedback/YYYY-MM-DD.jsonl` |
| 刷题题库 | `exam/questions.json` |
| 刷题进度 | `exam/exam.sqlite` |
| 日志 | `logs/*.log` |
| 配置(密钥) | `~/.zshrc` 或 `~/.bashrc`(不要放项目 `.env`) |

---

## 🔄 升级依赖(发现 CVE 时)

```bash
# 1. 看现在哪些包有 CVE
.venv/bin/pip-audit --strict

# 2. 升一个跑一次测试
.venv/bin/pip install --upgrade "pillow>=12.3.0"
.venv/bin/python -m unittest discover -s tests -p "test_*.py" -v

# 3. 全部升完再 audit 一次
.venv/bin/pip-audit --strict
```

---

## 🆘 救命

| 症状 | 第一步 |
|---|---|
| Bot 不响应 | `launchctl list \| grep mavis` 找进程;`make logs` 看错误 |
| KB 查不到东西 | `.venv/bin/python app.py status` 看 chunk 数是不是 0 |
| OCR 后内容乱 | 跑 `.venv/bin/python tools/verify_ocr.py --samples 3` 看准确率 |
| exam app 起不来 | `lsof -i :5001` 看是不是端口被占;`make restart` |
| 钉钉消息没到 | 钉钉开放平台 → 应用 → 机器人,确认安全域名 + 消息接收 |
| 完整跑挂了 | `make restart`,不行就 `make down && make up` |
