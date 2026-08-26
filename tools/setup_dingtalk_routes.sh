#!/bin/bash
# setup_dingtalk_routes.sh
# Add /16 routes for Aliyun IP ranges via en0, bypassing iKuuuVPN's TUN.
#
# Why: iKuuuVPN runs in TUN mode with fake-IP DNS, and its upstream relay is
# intermittently flaky. The DingTalk bot's WebSocket dies when the relay hiccups.
# Adding /16 routes for the Aliyun ranges (where DingTalk's API + WSS endpoints
# live) forces bot traffic through en0 (real NIC) instead of utun98 (VPN TUN).
#
# Trade-off: all Aliyun service traffic (e.g. Aliyun OSS/RDS if used) also
# bypasses the VPN. For a personal-use machine this is usually fine.
#
# Run as root via launchd (com.mavis.dingtalk-bypass.plist).
# Idempotent: safe to re-run.

set -e

EN0_GW="${EN0_GW:-192.168.43.117}"

# Aliyun ranges covering api.dingtalk.com and wss-open-connection-union.dingtalk.com
# Resolved via DoH (1.1.1.1 / dns.alidns.com) on 2026-08-26.
NETS=(
  "47.246.0.0/16"   # Aliyun international — api.dingtalk.com, wss endpoint
  "47.92.0.0/16"    # Aliyun international — wss endpoint alternate
  "106.11.0.0/16"   # Aliyun domestic — api.dingtalk.com alternate
)

# Idempotent route-add: ignore "File exists" errors
for net in "${NETS[@]}"; do
  if route -n get "$net" >/dev/null 2>&1; then
    echo "[skip] $net already routed"
  else
    if route -n add -net "$net" "$EN0_GW" 2>&1; then
      echo "[ok]   $net -> $EN0_GW (en0, bypass TUN)"
    else
      echo "[err]  failed to add $net" >&2
    fi
  fi
done
