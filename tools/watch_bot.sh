#!/bin/bash
# Watchdog for the DingTalk bot. If the WebSocket is stuck (no successful
# connect / no inbound message in N seconds), restart the bot via launchd.
#
# Heuristic:
#   - Bot process must be alive (PID via launchctl list)
#   - Log file mtime must be < 90s old AND content must contain a recent
#     success marker ("endpoint is" for a successful ticket exchange, or
#     "DingTalk text/picture" for a received message).
#
# If either check fails for two consecutive runs (90s grace + 5 min check
# interval = ~6 min of stuck state), force a kickstart -k restart.

set -u
PLIST_ID="com.mavis.knowledge-bot"
LOG_OUT="$HOME/Downloads/mass/ai_knowledge_cockpit/logs/dingtalk_bot.out.log"
LOG_ERR="$HOME/Downloads/mass/ai_knowledge_cockpit/logs/dingtalk_bot.err.log"
LOG="$LOG_OUT"  # primary mtime source
HEARTBEAT="/tmp/dingtalk_bot.alive"  # touched every 20s by a daemon thread
STATE="/tmp/bot_watchdog.state"

# Default state file
LAST_RESTART_TS=0
[ -f "$STATE" ] && LAST_RESTART_TS=$(cat "$STATE" 2>/dev/null || echo 0)

# 1. Is the bot process alive?
if launchctl list 2>/dev/null | grep -q "$PLIST_ID"; then
    PID=$(launchctl list 2>/dev/null | awk -v id="$PLIST_ID" '$3==id {print $1}')
    if [ -z "$PID" ] || [ "$PID" = "-" ]; then
        echo "[$(date -Iseconds)] DEAD: launchd has no PID for $PLIST_ID"
        ALIVE=0
    else
        ALIVE=1
    fi
else
    ALIVE=0
fi

# 2. Is the heartbeat file fresh? A daemon thread inside the bot
# touches /tmp/dingtalk_bot.alive every 20s. If the bot is healthy
# (process alive + event loop not frozen) the heartbeat will be
# fresh. If the event loop is dead, the file goes stale and we
# restart. This is more reliable than parsing log files because
# the SDK is silent on a healthy WebSocket.
NOW=$(date +%s)
HB_MTIME=0
[ -f "$HEARTBEAT" ] && HB_MTIME=$(stat -f %m "$HEARTBEAT" 2>/dev/null || echo 0)
HB_AGE=$(( NOW - HB_MTIME ))
HEARTBEAT_OK=0
[ "$HB_AGE" -lt 90 ] && HEARTBEAT_OK=1

# 3. Belt-and-suspenders: also check the log for recent success markers
# (helpful right after a restart, before the first heartbeat tick).
LOG_MTIME=0
[ -f "$LOG_OUT" ] && LOG_MTIME=$(stat -f %m "$LOG_OUT" 2>/dev/null || echo 0)
LOG_AGE=$(( NOW - LOG_MTIME ))
HAS_RECENT_SUCCESS=0
if [ "$LOG_AGE" -lt 30 ]; then
    if tail -n 5 "$LOG_ERR" 2>/dev/null | grep -qE "endpoint is"; then
        HAS_RECENT_SUCCESS=1
    fi
fi

# Decision
if [ "$ALIVE" = "1" ] && { [ "$HEARTBEAT_OK" = "1" ] || [ "$HAS_RECENT_SUCCESS" = "1" ]; }; then
    echo "[$(date -Iseconds)] OK: pid=$PID, hb_age=${HB_AGE}s, log_age=${LOG_AGE}s"
    exit 0
fi

# Unhealthy. If we restarted recently, just wait.
NOW=$(date +%s)
SINCE_RESTART=$(( NOW - LAST_RESTART_TS ))
if [ "$SINCE_RESTART" -lt 600 ]; then
    REASON="hb_age=${HB_AGE}s, log_age=${LOG_AGE}s, recent_success=$HAS_RECENT_SUCCESS"
    echo "[$(date -Iseconds)] UNHEALTHY (alive=$ALIVE, $REASON) — recent restart ${SINCE_RESTART}s ago, waiting"
    exit 0
fi

# Restart.
REASON="hb_age=${HB_AGE}s, log_age=${LOG_AGE}s, recent_success=$HAS_RECENT_SUCCESS"
echo "[$(date -Iseconds)] UNHEALTHY (alive=$ALIVE, $REASON) — restarting bot"
launchctl kickstart -k "gui/501/$PLIST_ID"
date +%s > "$STATE"
exit 0
