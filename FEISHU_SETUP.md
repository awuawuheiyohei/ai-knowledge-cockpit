# Feishu (Lark) Bot Setup

Add a Feishu / Lark chat surface to the KB. After this, sending a
keyword to the bot replies with `app.py search` results; sending an
image gets OCR'd and the bot replies with a synthesis that cites its
sources.

## 1. Create the app (one-time)

1. Open https://open.feishu.cn/app
2. **Create enterprise app → Custom app**
3. Fill in name (e.g. "知识库机器人") + description
4. On the app's **权限管理 (Permissions)** page, add:
   - `im:message` — send / receive messages
   - `im:message.group_at_msg` — receive @-mentions in groups (optional)
   - `im:message.p2p_msg` — receive direct messages
   - `im:resource` — download images / files
5. On **事件订阅 (Event Subscription)**:
   - Choose **使用长连接接收** is NOT for us — pick **将事件发送至开发者服务器**
   - Request URL: your public HTTPS URL pointing at `/feishu/event`
     (see step 3 for ngrok)
   - Encrypt key: optional, leave blank to start
6. On **机器人 (Bot)** capability:
   - Enable it
   - Save
7. On **版本管理与发布 (Version & Release)**:
   - Create a version
   - Request admin approval if your tenant requires it
   - Wait for the version to take effect

## 2. Get credentials

After the app is created, go to **凭证与基础信息 (Credentials)** and copy:

- **App ID** (looks like `cli_xxxxxxxxxxxxxx`)
- **App Secret**

Add to `.env` (in the project root):

```bash
FEISHU_APP_ID=cli_xxxxxxxxxxxxxx
FEISHU_APP_SECRET=your_app_secret_here
# Optional:
FEISHU_PORT=9002           # webhook port (default 9002)
FEISHU_VERIFICATION_TOKEN=  # only if you set Encrypt Token in console
FEISHU_ENCRYPT_KEY=        # only if you set Encrypt Key in console
```

## 3. Expose the webhook (local dev)

Feishu's event subscription needs a **public HTTPS** URL. For local
development, use ngrok:

```bash
# Start the bot in one terminal
./quickstart.sh serve feishu
# It listens on 0.0.0.0:9002

# In another terminal
ngrok http 9002
```

ngrok prints a line like `https://xxxx-xxx-xxx-xxx-xxx.ngrok-free.app`.
Paste that + `/feishu/event` into the **请求网址 (Request URL)** field
in the developer console:

```
https://xxxx-xxx-xxx-xxx-xxx.ngrok-free.app/feishu/event
```

Click **保存 (Save)**. Feishu will send a verification challenge; the
bot replies with the right `challenge` and Feishu marks the URL as
verified.

For production, replace ngrok with a real domain + reverse proxy.

## 4. Add the bot to a chat

- **Direct message**: search for the bot by name in Feishu, send
  anything. The bot can reply.
- **Group chat**: in the target group, **设置 (Settings) → 群机器人
  (Group Bots) → 添加机器人 (Add Bot)**, pick your bot.

## 5. Test the two scenarios

**场景 1 — keyword search** (text mode, no LLM synthesis):

```
你: PKI 数字证书
机器人: ### 🔎 检索: PKI 数字证书
        命中 N 条(按相关度排序)
        [1] `第7章-pki和密码应用-知识点.md` · md · score=35.71
        > ...
```

**场景 2 — image question** (image mode, LLM synthesis with mandatory
citation):

```
你: [sends a screenshot of a CISSP practice question]
机器人: ### 📷 识别的内容
        > 关于 PKI 的以下说法,哪一项是正确的?A...B...C...D...

        ### 🤖 综合回答
        正确答案是 C ...[来源: 第7章-pki和密码应用-知识点.md]...

        ### 📚 原始资料(请按文件名 + 页码回原文核对)
        [1] `第7章-pki和密码应用-知识点.md` · score=39.43
        ...
```

If the synthesis fails its citation check (e.g. the LLM rambles
without a `[来源: ...]` tag), the bot falls back to a "未在资料中
检索到" reply and still shows you the raw BM25 hits so you can verify
manually.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `Feishu config error: ... is not set` | Missing `.env` entries | `cp .env.example .env`, fill `FEISHU_APP_ID` / `FEISHU_APP_SECRET` |
| Webhook URL won't verify | ngrok URL changed, or bot not running | Restart ngrok, re-paste URL, ensure `./quickstart.sh serve feishu` is up |
| `📷 收到图片但无法获取 download_code` | Missing `im:resource` permission | Add the permission in the developer console, re-publish the app |
| Image replies never arrive | Webhook timeout? But the bot processes in a background thread | Check `feishu_bot` log for "image handling failed" — usually a VL call error |
| Bot replies, but markdown shows as raw `###` | Feishu text msg type doesn't render markdown | Switch to `interactive` card msg_type (TODO; for now text-only) |

## What's NOT in scope (yet)

- Encrypted event payloads (`FEISHU_ENCRYPT_KEY`): the current code
  only handles unencrypted events. The Encrypt Key support is TODO.
- Group @-mention routing: the bot currently replies to any message
  in a group. If you only want to reply when @-mentioned, filter on
  `event.message.mentions`.
- Interactive card messages: replies are sent as `text` msg_type. To
  get full markdown rendering, switch to `interactive` (rich card).
