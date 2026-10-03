#!/usr/bin/env python3
"""
scripts/connect_telegram.py — connect the operator's Telegram bot so
hypogeum.operator_alerts can message blockers.

    1. In Telegram, open @BotFather -> /newbot -> pick a name -> copy the token.
    2. python3 scripts/connect_telegram.py
    3. When asked, open your new bot in Telegram and press Start (or send /start).

Reads the bot token from the macOS clipboard and clears the clipboard right
after (or --prompt to type it hidden) — never pass it on the command line or
paste it into a chat. Verifies it with getMe, waits for your /start to learn
your chat id, writes TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID into .env, sends a
test message, and restarts the dashboard LaunchAgent (which runs the checks)
so it picks the settings up.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")


def _api(token: str, method: str, **params) -> dict:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(params).encode(), headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=40))


def _set_env(path: Path, values: dict) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    seen = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in values:
            lines[i] = f"{key}={values[key]}"
            seen.add(key)
    lines += [f"{k}={v}" for k, v in values.items() if k not in seen]
    path.write_text("\n".join(lines) + "\n")
    os.chmod(path, 0o600)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", action="store_true", help="type the token hidden instead of using the clipboard")
    ap.add_argument("--wait", type=int, default=180, help="seconds to wait for /start")
    args = ap.parse_args()

    if args.prompt:
        token = getpass.getpass("Telegram bot token (hidden): ").strip()
    else:
        token = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout.strip()
        subprocess.run(["pbcopy"], input="", text=True)
        if token:
            print(f"✓ token read from clipboard ({len(token)} chars) and clipboard cleared")
    if not _TOKEN_RE.match(token or ""):
        print("that doesn't look like a bot token (123456:ABC…) — copy it from @BotFather and re-run")
        return 1

    try:
        me = _api(token, "getMe")["result"]
    except Exception as exc:  # noqa: BLE001
        print(f"Telegram rejected the token: {exc}")
        return 1
    print(f"✓ bot @{me['username']} found")

    print(f"\n→ open https://t.me/{me['username']} in Telegram and press Start (waiting {args.wait}s)…")
    chat_id, offset, deadline = None, None, time.time() + args.wait
    while time.time() < deadline and chat_id is None:
        try:
            params = {"timeout": 25, **({"offset": offset} if offset else {})}
            for upd in _api(token, "getUpdates", **params).get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                if (msg.get("chat") or {}).get("type") == "private":
                    chat_id = msg["chat"]["id"]
        except Exception:  # noqa: BLE001
            time.sleep(3)
    if chat_id is None:
        print("no /start received — re-run and press Start in the bot chat")
        return 1
    if offset:
        _api(token, "getUpdates", offset=offset, timeout=0)   # mark consumed
    print("✓ chat found")

    _set_env(ROOT / ".env", {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": str(chat_id)})
    print("✓ saved to .env")

    _api(token, "sendMessage", chat_id=chat_id, text=(
        "✅ بوت Cortex اتربط.\n"
        "هبعتلك هنا أي حاجة توقّف المصنع أو محتاجة منك تدخّل: رصيد OpenRouter، الإنتاج لو وقف، "
        "الـ worker لو وقع، أغلفة يوتيوب المستنية، بوستات مانزلتش، حسابات محتاجة تتربط، ومساحة الهارد.\n"
        "كل مشكلة رسالة واحدة، ورسالة تانية لما تتحل."))
    print("✓ test message sent")

    uid = os.getuid()
    r = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/com.hypogeum.dashboard"],
                       capture_output=True, text=True)
    print("✓ dashboard restarted — alerts are live" if r.returncode == 0
          else "! restart the dashboard yourself so it picks up the bot settings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
