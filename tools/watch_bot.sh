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
LOG="$HOME/Downloads/mass/ai_knowledge_cockpit/logs/dingtalk_bot.out.log"
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

# 2. Is the log fresh (active) AND does it contain a recent success marker?
NOW=$(date +%s)
LOG_MTIME=0
[ -f "$LOG" ] && LOG_MTIME=$(stat -f %m "$LOG" 2>/dev/null || echo 0)
LOG_AGE=$(( NOW - LOG_MTIME ))

HAS_RECENT_SUCCESS=0
if [ "$LOG_AGE" -lt 90 ]; then
    # Look for "endpoint is" or "DingTalk text from" / "DingTalk picture" in last 30 lines
    if tail -n 30 "$LOG" 2>/dev/null | grep -qE "endpoint is|DingTalk (text|picture) from"; then
        HAS_RECENT_SUCCESS=1
    fi
fi

# Decision
if [ "$ALIVE" = "1" ] && [ "$HAS_RECENT_SUCCESS" = "1" ]; then
    echo "[$(date -Iseconds)] OK: pid=$PID, log_age=${LOG_AGE}s, recent success ✓"
    exit 0
fi

# Unhealthy. If we restarted recently, just wait.
NOW=$(date +%s)
SINCE_RESTART=$(( NOW - LAST_RESTART_TS ))
if [ "$SINCE_RESTART" -lt 300 ]; then
    echo "[$(date -Iseconds)] UNHEALTHY (alive=$ALIVE, success=$HAS_RECENT_SUCCESS) — recent restart ${SINCE_RESTART}s ago, waiting"
    exit 0
fi

# Restart.
echo "[$(date -Iseconds)] UNHEALTHY (alive=$ALIVE, log_age=${LOG_AGE}s, success=$HAS_RECENT_SUCCESS) — restarting bot"
launchctl kickstart -k "gui/501/$PLIST_ID"
date +%s > "$STATE"
exit 0
