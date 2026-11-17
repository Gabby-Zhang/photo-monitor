#!/usr/bin/env python3
"""
Wrapper for Windows Task Scheduler: `git pull`s the latest code, then runs
photo_library_monitor.py --mac-only — the two sources that can only run on
this PC (not GitHub Actions' cloud runners): Reuters Connect, which needs a
persistent logged-in browser profile (set up once via reuters_login.py), and
Shutterstock Editorial, which Akamai blocks from datacenter IPs. Every other
source is already covered by .github/workflows/photo_monitor.yml.

Dedup state (photo_library_seen.json) stays local to this PC — it is not
git-synced, matching how it worked on the old Mac. See CLAUDE.md.

Also watches the monitor's output for "LOGIN EXPIRED" (Reuters Connect's
session died) and pushes one Telegram alert per expiry via
TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID — this source otherwise fails silently
every 3 hours until someone notices and re-runs reuters_login.py.

Usage (Task Scheduler action):
  python.exe <repo>\\scripts\\run_photo_library_monitor_mac_only.py
"""

import os
import subprocess
import sys
from pathlib import Path

import requests

from _env import load_dotenv

REPO_ROOT            = Path(__file__).resolve().parent.parent
LOGIN_EXPIRED_MARKER = REPO_ROOT / ".reuters_login_expired_alerted"

load_dotenv(REPO_ROOT / ".env")


def _telegram_alert(text: str) -> None:
    token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print(f"[alert, not pushed — TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID unset] {text}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        ).raise_for_status()
    except Exception as e:
        print(f"Telegram push failed: {e}")


def main() -> int:
    pull = subprocess.run(["git", "pull", "--ff-only"], cwd=REPO_ROOT, capture_output=True, text=True)
    if pull.returncode != 0:
        print(f"git pull failed (continuing with current code): {pull.stderr.strip()}")

    result = subprocess.run(
        [sys.executable, "photo_library_monitor.py", "--mac-only"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)

    expired = "LOGIN EXPIRED" in result.stdout or "LOGIN EXPIRED" in result.stderr
    if expired and not LOGIN_EXPIRED_MARKER.exists():
        _telegram_alert(
            "⚠️ Reuters Connect 登录态过期了,图库监控暂时漏看这个源。"
            "在这台 Windows 电脑上重新跑一次 reuters_login.py 重新登录(会要求过一次设备验证)。"
        )
        LOGIN_EXPIRED_MARKER.write_text("")  # don't alert again until it's fixed
    elif not expired and LOGIN_EXPIRED_MARKER.exists():
        LOGIN_EXPIRED_MARKER.unlink()  # login works again — re-arm the alert

    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
