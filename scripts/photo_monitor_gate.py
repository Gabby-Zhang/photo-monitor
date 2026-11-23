#!/usr/bin/env python3
"""Decide whether this cron-job.org tick should run photo_library_monitor.py.

cron-job.org fires photo_monitor.yml every 20 minutes (workflow_dispatch with
tick=true). GitHub's own `schedule` was dropping most runs (23/11/2026, a
Strasbourg plenary day configured for every 20 min: 4 runs all day), so the
cadence now lives here, in Paris local time — no more CET/CEST cron shifting:

  * Strasbourg plenary days (PLENARY_DAYS), 08:00–20:00 Paris → every tick (20 min),
    but only the first tick of each hour scans everything; the other ticks are
    `--eu-only` (EU AV, EP Multimedia, Council). Getty/Imago/Alamy/Flickr stay
    hourly so their bot walls aren't hit every 20 min — plenary photos come from
    the EU sources anyway.
  * Weekdays 09:00–18:00 Paris                               → hourly, full scan
  * Everything else                                          → every 3 hours, full scan

Prints `run=true|false` and `mode=full|eu|skip` (also appended to $GITHUB_OUTPUT).
"""
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

# EP Strasbourg part-sessions (Mon–Thu). Update yearly from the official EP calendar.
PLENARY_WEEKS_2026 = [  # first day (Monday) of each session
    "2026-01-19", "2026-02-09", "2026-03-09", "2026-04-27", "2026-05-18",
    "2026-06-15", "2026-07-06", "2026-09-14", "2026-10-05", "2026-10-19",
    "2026-11-23", "2026-12-14",
]
PLENARY_DAYS = {
    date.fromisoformat(d) + timedelta(days=i) for d in PLENARY_WEEKS_2026 for i in range(4)
}

TICK_MINUTES = 20  # must match the cron-job.org interval


def decide(now: datetime) -> tuple[str, str]:
    """Return (mode, reason); mode is "full", "eu" or "skip"."""
    first_tick_of_hour = now.minute < TICK_MINUTES
    if now.date() in PLENARY_DAYS and 8 <= now.hour < 20:
        if first_tick_of_hour:
            return "full", "plenary day, first tick of the hour → full scan"
        return "eu", "plenary day, extra 20-min tick → EU sources only"
    if now.weekday() < 5 and 9 <= now.hour < 18:
        return ("full" if first_tick_of_hour else "skip"), "weekday daytime → hourly"
    return ("full" if first_tick_of_hour and now.hour % 3 == 0 else "skip"), "off hours → every 3h"


def main():
    now = datetime.now(ZoneInfo("Europe/Paris"))
    mode, why = decide(now)
    run = "false" if mode == "skip" else "true"
    print(f"{now:%Y-%m-%d %H:%M} Paris: {why} → mode={mode}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"run={run}\nmode={mode}\n")


if __name__ == "__main__":
    main()
