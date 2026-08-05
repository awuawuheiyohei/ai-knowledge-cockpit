# macOS launchd service for the KB bot

This directory holds the `launchd` plist for running the DingTalk
bot as a system service — auto-start on login, auto-restart on crash,
fully detached from any terminal.

## One-time install

```bash
# 1. Make sure logs/ exists at the repo root (the plist logs there)
mkdir -p /Users/jiangwenrui/Downloads/mass/ai_knowledge_cockpit/logs

# 2. Copy the plist into your per-user LaunchAgents
cp /Users/jiangwenrui/Downloads/mass/ai_knowledge_cockpit/deploy/com.mavis.knowledge-bot.plist \
   ~/Library/LaunchAgents/

# 3. Load it. This starts the bot RIGHT NOW and also on every
#    subsequent login. No sudo needed (it's in your per-user dir).
launchctl load ~/Library/LaunchAgents/com.mavis.knowledge-bot.plist
```

## Day-to-day

```bash
# Is it running?
launchctl list | grep knowledge
# 期望: <pid>   0   com.mavis.knowledge-bot

# Live logs
tail -f /Users/jiangwenrui/Downloads/mass/ai_knowledge_cockpit/logs/dingtalk_bot.out.log
tail -f /Users/jiangwenrui/Downloads/mass/ai_knowledge_cockpit/logs/dingtalk_bot.err.log

# Stop the service (clean stop, no auto-restart on next load)
launchctl unload ~/Library/LaunchAgents/com.mavis.knowledge-bot.plist

# Restart (stop + start, useful after editing app code)
launchctl unload ~/Library/LaunchAgents/com.mavis.knowledge-bot.plist
launchctl load ~/Library/LaunchAgents/com.mavis.knowledge-bot.plist

# Remove entirely
launchctl unload ~/Library/LaunchAgents/com.mavis.knowledge-bot.plist
rm ~/Library/LaunchAgents/com.mavis.knowledge-bot.plist
```

## Why this is option 4 (vs the 3 quick ways)

| | 1. quickstart | 2. bare python | 3. nohup & | **4. launchd (this)** |
|---|---|---|---|---|
| Lives in your terminal | ✓ | ✓ | starts there | **detached** |
| Survives terminal close | ✗ | ✗ | ✓ | **✓** |
| Survives logout / reboot | ✗ | ✗ | ✗ | **✓** |
| Auto-restarts on crash | ✗ | ✗ | ✗ | **✓** |
| Logs go to file | ✗ | ✗ | optional | **always** |
| Best for | debugging | one-shot | days | **production** |

## When to use which

- **Just want to test something quickly?** → use 1 or 2
- **Need the bot up for a few days while you monitor?** → use 3 (nohup)
- **Want the bot to "just always work" and not think about it?** → use 4 (this)

## Editing the plist

If you ever need to change the command (e.g. switch to `feishu`):

```bash
# 1. Edit the file in this repo
vim /Users/jiangwenrui/Downloads/mass/ai_knowledge_cockpit/deploy/com.mavis.knowledge-bot.plist

# 2. Copy + restart
cp <edited>  ~/Library/LaunchAgents/com.mavis.knowledge-bot.plist
launchctl unload ~/Library/LaunchAgents/com.mavis.knowledge-bot.plist
launchctl load   ~/Library/LaunchAgents/com.mavis.knowledge-bot.plist
```
