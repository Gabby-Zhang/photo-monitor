#!/usr/bin/env python3
"""
Wrapper around sejourn_photo_monitor.py that adds a shared git-committed
heartbeat so the EU AV monitor can run on two independent machines (the
Windows PC as primary, GitHub Actions as fallback) without double-notifying.

Model: whichever side last ran successfully commits photo_sync_state.json +
sejourn_photo_monitor.heartbeat.json to git. --role windows always runs.
--role cloud first checks the heartbeat; if a run happened recently (within
--threshold-minutes), it assumes the Windows PC is on and skips — otherwise
it treats the PC as off, runs itself, and takes over the heartbeat until the
PC comes back and starts refreshing it again.

Usage:
  python3 scripts/run_sejourn_photo_monitor.py --role windows
  python3 scripts/run_sejourn_photo_monitor.py --role cloud [--threshold-minutes 100]
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from _env import load_dotenv

REPO_ROOT      = Path(__file__).resolve().parent.parent
MONITOR_SCRIPT = REPO_ROOT / "sejourn_photo_monitor.py"
STATE_FILE     = REPO_ROOT / "photo_sync_state.json"
HEARTBEAT_FILE = REPO_ROOT / "sejourn_photo_monitor.heartbeat.json"

load_dotenv(REPO_ROOT / ".env")

DEFAULT_THRESHOLD_MIN = 100  # > one missed run on an hourly schedule, < two


def _git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _heartbeat_age_minutes() -> float | None:
    if not HEARTBEAT_FILE.exists():
        return None
    try:
        data = json.loads(HEARTBEAT_FILE.read_text())
        last = datetime.strptime(data["last_run_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None
    return (datetime.now(timezone.utc) - last).total_seconds() / 60


def _commit_and_push(role: str) -> None:
    _git("add", STATE_FILE.name, HEARTBEAT_FILE.name)
    if _git("diff", "--cached", "--quiet").returncode == 0:
        print("No state/heartbeat changes to commit.")
        return

    commit = _git("commit", "-m", f"chore: sync EU AV monitor state ({role})")
    if commit.returncode != 0:
        print(f"git commit failed (continuing): {commit.stderr.strip()}")
        return

    push = _git("push")
    if push.returncode == 0:
        return

    print(f"git push failed, retrying once after rebase: {push.stderr.strip()}")
    _git("pull", "--rebase")
    retry = _git("push")
    if retry.returncode != 0:
        print(f"git push still failed (state stays committed locally): {retry.stderr.strip()}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", required=True, choices=["windows", "cloud"])
    ap.add_argument("--threshold-minutes", type=int, default=DEFAULT_THRESHOLD_MIN)
    args = ap.parse_args()

    pull = _git("pull", "--ff-only")
    if pull.returncode != 0:
        print(f"git pull failed (continuing with local state): {pull.stderr.strip()}")

    if args.role == "cloud":
        age = _heartbeat_age_minutes()
        if age is not None and age < args.threshold_minutes:
            print(f"Heartbeat is {age:.0f} min old (< {args.threshold_minutes}) — "
                  f"Windows PC looks alive, skipping cloud run.")
            return 0
        print(f"Heartbeat is {'missing' if age is None else f'{age:.0f} min old'} — "
              f"running as fallback.")

    result = subprocess.run([sys.executable, str(MONITOR_SCRIPT)], cwd=REPO_ROOT)
    if result.returncode != 0:
        print(f"sejourn_photo_monitor.py exited {result.returncode} — "
              f"not refreshing the heartbeat, so the other side takes over next time.")
        return result.returncode

    HEARTBEAT_FILE.write_text(json.dumps({"last_run_utc": _now_iso(), "runner": args.role}, indent=2) + "\n")
    _commit_and_push(args.role)
    return 0


if __name__ == "__main__":
    sys.exit(main())
