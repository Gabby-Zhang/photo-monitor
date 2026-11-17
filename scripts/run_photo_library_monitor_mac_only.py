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

Usage (Task Scheduler action):
  python.exe <repo>\\scripts\\run_photo_library_monitor_mac_only.py
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    pull = subprocess.run(["git", "pull", "--ff-only"], cwd=REPO_ROOT, capture_output=True, text=True)
    if pull.returncode != 0:
        print(f"git pull failed (continuing with current code): {pull.stderr.strip()}")

    return subprocess.run(
        [sys.executable, "photo_library_monitor.py", "--mac-only"], cwd=REPO_ROOT
    ).returncode


if __name__ == "__main__":
    sys.exit(main())
