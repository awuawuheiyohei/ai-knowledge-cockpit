# IM 接入指南 · 企业微信 + 钉钉

两个平台都做了。下面是完整的接入步骤。

## 总览

| 平台 | 模式 | 是否需要公网 URL | 推荐程度 |
|------|------|------------------|----------|
| **钉钉** | Stream 模式（WebSocket 长连接） | **不需要** | ★★★★★ 强烈推荐先做这个 |
| **企业微信** | 智能机器人回调（HTTP POST） | **需要**（可 ngrok 兜底） | ★★★★ 完整可用，需要做内网穿透 |

**先做钉钉**——不依赖任何公网配置，跑通最快。

---

## 钉钉（推荐先做）

### 原理
钉钉官方提供了一个叫"Stream 模式"的长连接方案：你跑一个 Python 进程，钉钉服务器主动连过来推送消息。不需要公网回调地址，不需要 HTTPS 证书，不需要 ngrok。

### 步骤

1. **去钉钉开放平台注册应用**
   - 打开 https://open-dev.dingtalk.com
   - 用钉钉扫一扫登录
   - 进入「应用开发」→「企业内部应用」→「创建应用」
   - 应用名随便起，比如「我的知识库」
   - 应用类型选「机器人」也行，普通 H5 微应用也行（只要开启机器人能力即可）

2. **开启机器人能力 + 获取凭证**
   - 进应用详情页，找到「机器人」卡片，点击「开启」
   - 配置机器人名称（用户看到的名字）和消息回调方式：**必须选 Stream 模式**
   - 添加机器人消息接收范围（哪些人能用）
   - 发布应用（企业内部应用通常需要管理员审批）
   - 在「基础信息 > 凭证信息」页拿到：
     - **AppKey**（也叫 ClientId）
     - **AppSecret**（也叫 ClientSecret）

3. **配置环境变量**
   ```bash
   export DINGTALK_APP_KEY='你的AppKey'
   export DINGTALK_APP_SECRET='你的AppSecret'
   ```
   如果想持久化，把这两行加到 `~/.zshrc` 或 shell profile 里，**不要**写进 `.env`（避免泄露）。

4. **先确保 KB 里有内容**
   ```bash
   cd /path/to/ai_knowledge_cockpit
   python app.py ingest your-pdf-or-md-dir/ --recursive
   ```

5. **启动 Stream bot**
   ```bash
   python app.py serve dingtalk
   ```
   看到 `DingTalk bot starting (Stream mode). Press Ctrl+C to stop.` 就跑起来了。

6. **客户端测试**
   - 在钉钉里找到刚创建的机器人，发一条消息
   - 应该会收到 Markdown 格式的回复，带文件来源

### 常见坑（已规避）

- ✅ **不要用自定义机器人 webhook**——它只能推送，不能接收用户消息。
- ✅ Stream 模式消息回调路径固定（`/v1.0/im/bot/messages/get`），SDK 帮你处理了，不用关心。
- ✅ 消息内容字段在 `msgtype=='text'` 时才会被 SDK 解析成 `incoming.text.content`，其他类型我们做了容错。
- ⚠️ **企业内部应用需要管理员审批才能上线**，如果你只是个人测试，可以让同企业的管理员快速过一下。

---

## 企业微信（需要公网 URL）

### 原理
企微的智能机器人走 HTTP POST 回调：你的服务必须暴露一个 HTTPS（或 HTTP）公网 URL，企微服务器把加密的 XML POST 过来，你解密后处理，加密返回。

### 步骤

1. **创建企业微信智能机器人**
   - 打开 https://developer.work.weixin.qq.com
   - 用企业微信管理员账号登录
   - 进入「应用管理」→「自建」或「智能机器人」（注意：不是"群机器人"，那个不能接收消息）
   - 创建应用，记下：
     - **CorpID**（企业 ID）
     - **AgentID**（应用 ID）
   - 在「接收消息」或「API 接收消息」配置页：
     - **URL**：`https://你的公网域名/wecom/callback`（**先填个占位符保存失败也行，等 ngrok 拿到再回来填**）
     - **Token**：自己随便编一个字符串，记住它
     - **EncodingAESKey**：点「随机生成」，会给你一个 43 位的字符串，**这就是 AES 加密密钥，复制下来**

2. **把凭证写到环境变量**
   ```bash
   export WECOM_CORP_ID='你的CorpID'
   export WECOM_AGENT_ID='你的AgentID'
   export WECOM_TOKEN='刚才自己编的Token'
   export WECOM_ENCODING_AES_KEY='43位的EncodingAESKey'
   export WECOM_PORT=9001
   ```
   默认监听 `0.0.0.0:9001`。

3. **公网 URL：ngrok 方案（本地开发用）**
   ```bash
   # 安装 ngrok（macOS）
   brew install ngrok
   # 或去 https://ngrok.com/download 下载

   # 注册 ngrok 账号拿 authtoken
   ngrok config add-authtoken 你的token

   # 启动公网隧道
   ngrok http 9001
   ```
   ngrok 会显示一行：`https://xxxx-xxx-xxx-xxx-xxx.ngrok-free.app -> http://localhost:9001`
   把这个 ngrok 地址填回企微后台的 URL 输入框，后面接 `/wecom/callback`：
   ```
   https://xxxx-xxx-xxx-xxx-xxx.ngrok-free.app/wecom/callback
   ```
   点企微后台的「保存」，它会 GET 这个 URL 验证。如果服务还没起来，**先启动 app.py serve wecom** 再保存。

4. **启动企微回调服务**
   ```bash
   python app.py serve wecom
   ```
   看到 `WeCom bot listening on http://0.0.0.0:9001` 就跑起来了。

5. **客户端测试**
   - 在企业微信里找到这个应用，发条消息
   - 应该会收到文本格式的回复，带文件来源

### 常见坑（已规避）

- ✅ **不要用群机器人 webhook**——同钉钉，自定义机器人只能推不能收。
- ✅ 企微要求所有消息 AES 加密（接收 + 回复都要），所有签名 SHA1 校验。本项目自带完整 crypto 实现（`wecom_server.py`），不依赖 wechatpy。
- ✅ HTTP 回调支持 http 或 https；企微智能机器人对自签名证书通常不挑剔，但生产建议用真实证书。
- ⚠️ **ngrok 免费版每次重启 URL 会变**，每次都要去企微后台重新配置 URL。稳定方案是：
  - 升级 ngrok 付费版（保留固定子域名，约 $8/月）
  - 或用自己的云服务器 + 域名 + nginx + Let's Encrypt 证书
- ⚠️ 如果你看到 `signature mismatch` 报错，八成是 `WECOM_TOKEN` 跟企微后台填的不一致，或者时间戳格式有问题。
- ⚠️ **编码**：企微收发都是 UTF-8，已确认无 BOM。

---

## 端到端测试（不依赖真实 IM）

不进 IM 也能验证服务能正常处理消息：

```bash
# 1. 准备临时 KB
python app.py init
python app.py ingest some-markdown-file.md

# 2. 启动服务（建议另一个终端）
WECOM_TOKEN=test WECOM_ENCODING_AES_KEY=$(python -c "import secrets,string; print(''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(43)))") WECOM_CORP_ID=wxabc WECOM_AGENT_ID=1 python app.py serve wecom

# 3. 跑 im_router 单元测试（不需要服务）
python -c "
from im_router import handle_message
print(handle_message('wecom', 'PKI'))
print(handle_message('dingtalk', '/help'))
print(handle_message('wecom', '/status'))
print(handle_message('wecom', 'xyz乱码查询'))
"
```

---

## 同时跑两个平台

```bash
# 终端 1：钉钉
DINGTALK_APP_KEY=xxx DINGTALK_APP_SECRET=yyy python app.py serve dingtalk

# 终端 2：企微
WECOM_CORP_ID=xxx WECOM_AGENT_ID=xxx WECOM_TOKEN=xxx WECOM_ENCODING_AES_KEY=xxx python app.py serve wecom
```

两个进程共享同一个 `data/kb.sqlite`，互不干扰。

---

## 硬规则（这条不会变）

无论走哪个 IM：
- **不调任何 LLM**
- 检索无命中 → 直接说"未在知识库中找到"
- 每条回复都带 source（文件名 + 页码 + 原文片段）
- 用户能用 `/help` 和 `/status` 查 KB 状态

如果哪天想加 LLM 摘要能力（可选，默认关），那是另一个 feature flag 的事，不是默认行为。