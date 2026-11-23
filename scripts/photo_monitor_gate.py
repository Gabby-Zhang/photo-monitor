#!/usr/bin/env python3
"""Decide whether this cron-job.org tick should run photo_library_monitor.py.

cron-job.org fires photo_monitor.yml every 20 minutes (workflow_dispatch with
tick=true). GitHub's own `schedule` was dropping most runs (23/11/2026, a
Strasbourg plenary day configured for every 20 min: 4 runs all day), so the
cadence now lives here, in Paris local time — no more CET/CEST cron shifting:

  * Strasbourg plenary days (PLENARY_DAYS), 08:00–20:00 Paris → every tick (20 min)
  * Weekdays 09:00–18:00 Paris                               → hourly (first tick of the hour)
  * Everything else                                          → every 3 hours

Prints `run=true|false` (and appends it to $GITHUB_OUTPUT when set).
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


def should_run(now: datetime) -> tuple[bool, str]:
    first_tick_of_hour = now.minute < TICK_MINUTES
    if now.date() in PLENARY_DAYS and 8 <= now.hour < 20:
        return True, "plenary day, daytime → every tick"
    if now.weekday() < 5 and 9 <= now.hour < 18:
        return first_tick_of_hour, "weekday daytime → hourly"
    return first_tick_of_hour and now.hour % 3 == 0, "off hours → every 3h"


def main():
    now = datetime.now(ZoneInfo("Europe/Paris"))
    run, why = should_run(now)
    print(f"{now:%Y-%m-%d %H:%M} Paris: {why} → run={str(run).lower()}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"run={str(run).lower()}\n")


if __name__ == "__main__":
    main()
