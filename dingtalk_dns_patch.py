# dingtalk_dns_patch.py
# Monkey-patch socket.getaddrinfo so *.dingtalk.com hostnames resolve via
# DNS-over-HTTPS (1.1.1.1 / alidns) instead of the system resolver.
#
# Why: iKuuuVPN runs in TUN mode with fake-IP DNS (198.18.0.0/15). Every
# DNS query — even to public resolvers — gets hijacked to a fake IP. The
# DingTalk SDK then connects to a fake IP that iKuuuVPN's TUN is supposed
# to relay upstream, but that relay is intermittently flaky and the WS dies.
#
# Workaround: resolve via DoH (HTTPS port 443, which iKuuuVPN doesn't hijack
# by content) so we get the REAL Aliyun IPs, then connect to those. The
# companion LaunchDaemon (com.mavis.dingtalk-bypass) adds /16 routes for
# those IPs via en0, so the connection bypasses TUN entirely.
#
# Privacy trade-off: this bot's DingTalk traffic doesn't go through the VPN.
# For a personal-use bot talking to a work chat, that's fine.
#
# Auto-loaded by dingtalk_server.py at import time.

import json
import logging
import socket
import urllib.request

_log = logging.getLogger("dingtalk_dns_patch")
if not _log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    _log.addHandler(_h)
    _log.setLevel(logging.INFO)

_REAL_IP_CACHE: dict[str, str] = {}
_DOH_SERVERS = [
    # (URL template, requires accept header?)
    ("https://1.1.1.1/dns-query?name={name}&type=A", True),
    ("https://dns.alidns.com/resolve?name={name}&type=A", False),
]


def _doh_resolve(name: str) -> str | None:
    """Resolve a hostname via DoH. Returns the first A record IP, or None."""
    if name in _REAL_IP_CACHE:
        return _REAL_IP_CACHE[name]

    for tmpl, needs_accept in _DOH_SERVERS:
        try:
            url = tmpl.format(name=name)
            req = urllib.request.Request(url)
            if needs_accept:
                req.add_header("accept", "application/dns-json")
            with urllib.request.urlopen(req, timeout=5) as resp:
                payload = json.loads(resp.read())
            for ans in payload.get("Answer", []):
                if ans.get("type") == 1:  # A record
                    ip = ans["data"]
                    _REAL_IP_CACHE[name] = ip
                    _log.info("DoH resolved %s -> %s (via %s)", name, ip, tmpl.split("/")[2])
                    return ip
        except Exception as exc:  # noqa: BLE001
            _log.debug("DoH via %s failed for %s: %s", tmpl, name, exc)
            continue

    _log.warning("DoH could not resolve %s, falling back to system DNS", name)
    return None


def _is_dingtalk_host(host: str) -> bool:
    if not host:
        return False
    h = host.lower().rstrip(".")
    return h == "dingtalk.com" or h.endswith(".dingtalk.com")


_original_getaddrinfo = socket.getaddrinfo


def _patched_getaddrinfo(host, port, *args, **kwargs):
    if _is_dingtalk_host(host):
        real_ip = _doh_resolve(host)
        if real_ip:
            # Mirror the original tuple shape: (family, type, proto, canonname, sockaddr)
            family = kwargs.get("family", socket.AF_INET)
            socktype = kwargs.get("type", socket.SOCK_STREAM)
            proto = kwargs.get("proto", 0)
            return [(family, socktype, proto, "", (real_ip, port))]
    return _original_getaddrinfo(host, port, *args, **kwargs)


socket.getaddrinfo = _patched_getaddrinfo
_log.info("socket.getaddrinfo patched for *.dingtalk.com (DoH-based)")
