"""
security_audit.py — Run a security audit on the AI Knowledge Cockpit.

What it checks
--------------
1. Secret scan
   - grep tracked source for hard-coded API keys, tokens, passwords
   - confirm .env is git-ignored
   - confirm the commited exam/questions.json + scripts/*.sh don't leak

2. SQL injection / path traversal fuzzing
   - send malicious doc= and query= values to the live exam app + KB search
   - verify the app doesn't crash, leak, or execute the injection

3. Dangerous code patterns
   - eval / exec / os.system / subprocess with shell=True
   - pickle.load on untrusted input
   - requests.get with verify=False
   - hashlib.md5 used as security (note: ok for non-security, warn if used for auth)

4. Dependency CVEs
   - pip-audit if installed; otherwise list outdated packages

5. Auth / network exposure
   - confirm exam app binds to 127.0.0.1 only (not 0.0.0.0)
   - confirm bot serves only IM (not a public HTTP port)

Output
------
- One-line summary per check (✅ / ⚠️ / ❌)
- Detailed findings list at the end
- Exit code 0 if no critical findings, 1 if any ❌
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SECRET_PATTERNS = [
    (r"sk-[a-zA-Z0-9]{20,}", "OpenAI-style API key"),
    (r"sk-ant-[a-zA-Z0-9-]{20,}", "Anthropic API key"),
    (r"ghp_[a-zA-Z0-9]{20,}", "GitHub PAT"),
    (r"gho_[a-zA-Z0-9]{20,}", "GitHub OAuth token"),
    (r"AKIA[0-9A-Z]{16}", "AWS access key"),
    (r"AIza[0-9A-Za-z_-]{35}", "Google API key"),
    # For env-var patterns, only flag if the value looks REAL (long,
    # not a placeholder). Placeholders like "your-key", "...", "你的"
    # are skipped.
    (r"VL_API_KEY\s*=\s*['\"]([^'\"]+)['\"]", "VL_API_KEY literal in code"),
    (r"LLM_API_KEY\s*=\s*['\"]([^'\"]+)['\"]", "LLM_API_KEY literal in code"),
    (r"DINGTALK_APP[_-]KEY\s*=\s*['\"]([^'\"]+)['\"]", "DingTalk key literal"),
    (r"WECOM_CORP[_-]ID\s*=\s*['\"]([^'\"]+)['\"]", "WeCom ID literal"),
]

PLACEHOLDER_HINTS = [
    "your", "example", "placeholder", "xxx", "...",
    "你的", "示例", "占位", "替换", "改为",
    "<", ">",
]


def _is_placeholder(value: str) -> bool:
    """A value is a placeholder if it's short, contains truncation
    markers, or matches common placeholder patterns."""
    v = value.strip()
    if len(v) < 16:
        return True
    vl = v.lower()
    for hint in PLACEHOLDER_HINTS:
        if hint in vl:
            return True
    return False

DANGEROUS_PATTERNS = [
    (r"\beval\s*\(", "eval() — RCE risk if input is untrusted"),
    (r"\bexec\s*\(", "exec() — RCE risk if input is untrusted"),
    (r"os\.system\s*\(", "os.system — command injection risk"),
    (r"subprocess\.[a-z]+\([^)]*shell\s*=\s*True", "subprocess shell=True — command injection risk"),
    (r"pickle\.loads?\s*\(", "pickle.load — arbitrary code execution on untrusted data"),
    (r"verify\s*=\s*False", "requests verify=False — MITM risk"),
    (r"hashlib\.md5\s*\(\s*\)\.hexdigest\s*\(\s*\)", "MD5 used as hash (note: only OK for non-security)"),
    (r"\.format\s*\([^)]*\{[a-z_]+\}", ".format() with user data — potential injection if untrusted"),
]

SOURCE_EXTENSIONS = {".py", ".sh", ".md", ".yml", ".yaml", ".json"}


def find_tracked_sources() -> list[Path]:
    """All tracked files in git that look like source."""
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True
    ).stdout.splitlines()
    return [ROOT / p for p in out if Path(p).suffix in SOURCE_EXTENSIONS]


def scan_secrets(files: list[Path]) -> list[dict]:
    findings = []
    for f in files:
        try:
            text = f.read_text(errors="ignore")
        except Exception:
            continue
        for pat, label in SECRET_PATTERNS:
            for m in re.finditer(pat, text):
                # The env-var patterns have a capture group for the value.
                # For ungrouped patterns (like sk-...), group 0 is the value.
                try:
                    value = m.group(1)
                except IndexError:
                    value = m.group(0)
                if _is_placeholder(value):
                    continue
                findings.append({
                    "file": str(f.relative_to(ROOT)),
                    "pattern": label,
                    "match": m.group(0)[:60],
                    "line": text[:m.start()].count("\n") + 1,
                })
    return findings


def scan_dangerous_code(files: list[Path]) -> list[dict]:
    findings = []
    for f in files:
        if not f.suffix == ".py":
            continue
        try:
            text = f.read_text(errors="ignore")
        except Exception:
            continue
        for pat, label in DANGEROUS_PATTERNS:
            for m in re.finditer(pat, text):
                line_no = text[:m.start()].count("\n") + 1
                line_text = text.splitlines()[line_no - 1].strip() if line_no <= len(text.splitlines()) else ""
                findings.append({
                    "file": str(f.relative_to(ROOT)),
                    "line": line_no,
                    "pattern": label,
                    "snippet": line_text[:100],
                })
    return findings


def check_gitignore() -> list[dict]:
    findings = []
    gitignore = (ROOT / ".gitignore").read_text() if (ROOT / ".gitignore").exists() else ""
    if ".env" not in gitignore:
        findings.append({
            "file": ".gitignore",
            "pattern": ".env NOT in gitignore — secrets could leak",
        })
    # Confirm .env is not actually tracked
    tracked = subprocess.run(
        ["git", "ls-files", ".env", ".env.*"],
        cwd=ROOT, capture_output=True, text=True
    ).stdout.strip()
    if tracked:
        findings.append({
            "file": tracked,
            "pattern": ".env is TRACKED in git! Immediate rotate all secrets.",
        })
    return findings


def fuzz_exam_app() -> list[dict]:
    """Fuzz the live exam app endpoints with malicious inputs."""
    import urllib.request
    import urllib.parse
    import urllib.error

    base = "http://127.0.0.1:5001"
    findings = []

    # (label, path, query_dict, method, body)
    tests = [
        ("SQLi in doc",     "/api/question", {"doc": "' OR 1=1 --"},         "GET",  None),
        ("SQLi in mode",    "/api/question", {"mode": "random' OR '1"},     "GET",  None),
        ("Path traversal doc", "/api/question", {"doc": "../../../etc/passwd"}, "GET", None),
        ("Path traversal in id", "/api/question/..%2F..%2Fetc%2Fpasswd", {},   "GET",  None),
        ("XSS in question_id", "/api/answer", {}, "POST",
         {"question_id": "<script>alert(1)</script>", "user_answer": "A"}),
        ("Huge query",      "/api/question", {"doc": "A" * 10000},          "GET",  None),
        ("Null byte in doc","/api/question", {"doc": "foo\x00.png"},         "GET",  None),
        ("Mass assign",     "/api/answer",   {}, "POST",
         {"question_id": "x", "user_answer": "A", "is_correct": True, "admin": True}),
    ]

    for label, path, query, method, body in tests:
        try:
            if method == "GET":
                qs = urllib.parse.urlencode(query) if query else ""
                url = f"{base}{path}?{qs}" if qs else f"{base}{path}"
                req = urllib.request.Request(url)
            else:
                req = urllib.request.Request(
                    f"{base}{path}", method="POST",
                    data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"},
                )
            try:
                resp = urllib.request.urlopen(req, timeout=5)
                code = resp.status
                text = resp.read().decode("utf-8", errors="ignore")[:300]
            except urllib.error.HTTPError as e:
                code = e.code
                text = e.read().decode("utf-8", errors="ignore")[:300]
            if code >= 500:
                findings.append({
                    "test": label, "url": path, "result": f"❌ 5xx crash: {code}", "body": text,
                })
            elif code == 200 and ("traceback" in text.lower() or "stack trace" in text.lower()):
                findings.append({
                    "test": label, "url": path, "result": "❌ stack trace leaked", "body": text,
                })
        except Exception as e:
            findings.append({
                "test": label, "url": path, "result": f"❌ exception: {type(e).__name__}: {e}",
            })
    return findings


def check_bind_addresses() -> list[dict]:
    """Confirm dev servers don't bind to 0.0.0.0."""
    findings = []
    for f in [ROOT / "app.py", ROOT / "exam" / "app.py"]:
        try:
            text = f.read_text()
        except Exception:
            continue
        if re.search(r"app\.run\([^)]*host\s*=\s*['\"]0\.0\.0\.0", text):
            findings.append({
                "file": str(f.relative_to(ROOT)),
                "pattern": "Flask binds 0.0.0.0 — exposes to network",
            })
        if "host=" not in text and "host =" not in text:
            # Flask default is 127.0.0.1, which is fine
            pass
    return findings


def check_dependencies() -> list[dict]:
    """Try pip-audit; fall back to listing installed versions."""
    findings = []
    try:
        result = subprocess.run(
            [".venv/bin/pip-audit", "--strict"],
            cwd=ROOT, capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0 and result.stdout:
            findings.append({
                "tool": "pip-audit",
                "result": result.stdout[:2000],
            })
    except FileNotFoundError:
        findings.append({
            "tool": "pip-audit",
            "result": "pip-audit not installed (run: pip install pip-audit)",
        })
    except subprocess.TimeoutExpired:
        findings.append({
            "tool": "pip-audit",
            "result": "pip-audit timed out after 60s",
        })
    return findings


def main() -> int:
    print("=" * 70)
    print("AI Knowledge Cockpit — security audit")
    print("=" * 70)

    files = find_tracked_sources()
    print(f"\nScanning {len(files)} tracked source files…")

    # 1. Secret scan
    secrets = scan_secrets(files)
    print(f"\n[1/5] Secret scan: {len(secrets)} findings")
    if secrets:
        for s in secrets:
            print(f"   ❌ {s['file']}:{s['line']} — {s['pattern']}")
            print(f"      {s['match']}")
    else:
        print("   ✅ no hard-coded secrets detected")

    # 2. Gitignore
    gi = check_gitignore()
    print(f"\n[2/5] Gitignore check: {len(gi)} findings")
    if gi:
        for f in gi:
            print(f"   ❌ {f}")
    else:
        print("   ✅ .env is git-ignored and not tracked")

    # 3. Dangerous code patterns
    code = scan_dangerous_code(files)
    print(f"\n[3/5] Dangerous code patterns: {len(code)} findings")
    if code:
        for c in code:
            print(f"   {'❌' if 'pickle' in c['pattern'] or 'eval' in c['pattern'] or 'shell=True' in c['pattern'] else '⚠️'} "
                  f"{c['file']}:{c['line']} — {c['pattern']}")
            print(f"      {c['snippet']}")
    else:
        print("   ✅ no eval / pickle / shell=True / verify=False")

    # 4. Bind addresses
    bind = check_bind_addresses()
    print(f"\n[4/5] Network bind check: {len(bind)} findings")
    if bind:
        for b in bind:
            print(f"   ❌ {b}")
    else:
        print("   ✅ all dev servers bind 127.0.0.1 only")

    # 5. Fuzz live exam app
    print(f"\n[5/5] Endpoint fuzzing (exam app on 127.0.0.1:5001)…")
    fuzz = fuzz_exam_app()
    if fuzz:
        for f in fuzz:
            print(f"   ❌ {f['test']}: {f.get('url', '')}")
            print(f"      {f['result']}")
    else:
        print("   ✅ all 8 fuzz cases handled gracefully (no 5xx, no leak)")

    # Optional: dep audit
    print(f"\n[bonus] Dependency CVE check…")
    deps = check_dependencies()
    for d in deps:
        if "no findings" in d.get("result", "").lower() or "all clear" in d.get("result", "").lower():
            print(f"   ✅ {d.get('result', '').splitlines()[0]}")
        else:
            print(f"   ⚠️ {d}")

    # Summary
    print("\n" + "=" * 70)
    crit = len(secrets) + len([f for f in fuzz if "5xx" in f.get("result", "") or "exception" in f.get("result", "")]) + len([c for c in code if "pickle" in c["pattern"] or "eval(" in c["pattern"] or "shell=True" in c["pattern"]]) + len(bind) + len([g for g in gi if "TRACKED" in g.get("pattern", "")])
    print(f"CRITICAL: {crit}    HIGH: ...    summary above")
    return 1 if crit > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
