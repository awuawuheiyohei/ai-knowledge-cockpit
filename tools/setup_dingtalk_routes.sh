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
# Idempotent: safe to re-run (skips nets that already have a non-TUN route).

set -e

EN0_GW="${EN0_GW:-192.168.43.117}"

# Aliyun ranges covering api.dingtalk.com and wss-open-connection-union.dingtalk.com
# Resolved via DoH (1.1.1.1 / dns.alidns.com) on 2026-08-26.
NETS=(
  "47.246.0.0/16"   # Aliyun international — api.dingtalk.com, wss endpoint
  "47.92.0.0/16"    # Aliyun international — wss endpoint alternate
  "106.11.0.0/16"   # Aliyun domestic — api.dingtalk.com alternate
)

# Idempotent route-add:
# `route -n get <net>` answers "what route would this packet use?" — wrong.
# We need to ask "is <net> itself a route entry, and does it NOT point to utun98?"
# Use `route -n show <net>` and grep for the exact destination + utun98 absence.
is_already_bypassing_tun() {
  local net="$1"
  local show_output
  show_output="$(route -n show "$net" 2>/dev/null || true)"
  # Look for a line starting with the network address pointing somewhere other than utun98.
  # `route show` output format: "<net>          <gateway>     <flags>      <interface>"
  if echo "$show_output" | grep -qE "^${net//./\\.}\s.*\s\S+\s*$"; then
    # Found a row for this net. Check it doesn't go to utun98.
    if echo "$show_output" | grep -v "utun98" | grep -q "^${net//./\\.}"; then
      return 0  # already bypassing TUN
    fi
  fi
  return 1  # not present, or still on utun98
}

for net in "${NETS[@]}"; do
  if is_already_bypassing_tun "$net"; then
    echo "[skip] $net already bypasses TUN"
  else
    if route -n add -net "$net" "$EN0_GW" 2>&1; then
      echo "[ok]   $net -> $EN0_GW (en0, bypass TUN)"
    else
      echo "[err]  failed to add $net" >&2
    fi
  fi
done
