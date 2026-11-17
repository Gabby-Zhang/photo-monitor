"""
Minimal .env loader, no extra pip dependency. Shared by the Windows Task
Scheduler wrapper scripts, matching the .env pattern already used for the
Mac launchd jobs (see CLAUDE.md) — Task Scheduler's "run whether user is
logged on or not" mode doesn't reliably pass through env vars set some
other way, so secrets live in a local, gitignored .env file instead.
"""

import os
from pathlib import Path


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
