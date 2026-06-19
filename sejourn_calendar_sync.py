#!/usr/bin/env python3
"""
Scrapes Stéphane Séjourné's EU Commission calendar and syncs events to Apple Calendar.

Requirements:
  pip3 install requests beautifulsoup4

Usage:
  python3 sejourn_calendar_sync.py            # upcoming events only
  python3 sejourn_calendar_sync.py --all      # include past events
  python3 sejourn_calendar_sync.py --dry-run  # preview without writing
  python3 sejourn_calendar_sync.py --reset    # clear sync state & re-sync all
"""

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import argparse
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── Config ─────────────────────────────────────────────────────────────────────

BASE_URL = (
    "https://commission.europa.eu/about/organisation/college-commissioners"
    "/calendar-items-president-and-commissioners_en"
)
# NOTE: the old server-side commissioner facet (f[0]=commissioner_dynamic…) is
# dead — the EU site migrated to OpenEuropa List Pages and ignores it, and it has
# dropped Séjourné from the dropdown entirely. We now fetch the page UNFILTERED
# and match by name client-side (see parse_events), so we never depend on his
# facet option existing.
FILTER_QUERY = ""

# EU Transparency Register — his + cabinet meetings with interest representatives.
# Live source that still works while the aggregate page's pagination is broken.
TRANSPARENCY_BASE = "https://ec.europa.eu/transparency-initiative/meetings/meeting.do?host="
TRANSPARENCY_HOSTS = [
    ("self",    "d8fba42d-8cc3-42c8-b1f1-e07d9b2ee8ea", "Séjourné meets"),
    ("cabinet", "21deeb50-48f9-40a3-9ab0-ac66cdbb2ca2", "Séjourné Cabinet meets"),
]

STATE_FILE = Path(__file__).parent / "sync_state.json"
CALENDAR_NAME = "Séjourné - EC Calendar"
ITEMS_PER_PAGE = 20

# ── Push notifications (ntfy.sh) ───────────────────────────────────────────────
# Set your ntfy topic here (must match what you subscribed to in the ntfy app)
NTFY_TOPIC = "ss-calendar-update"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

MONTH_MAP = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4,  "May": 5,  "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

# ── Scraper ────────────────────────────────────────────────────────────────────

def fetch_page(page: int) -> BeautifulSoup:
    qs = (FILTER_QUERY + "&") if FILTER_QUERY else ""
    url = f"{BASE_URL}?{qs}page={page}"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def parse_events(soup: BeautifulSoup) -> list:
    events = []
    for article in soup.select("article.ecl-content-item--inline"):
        time_el = article.select_one("time.ecl-content-item__date")
        if not time_el:
            continue

        day   = time_el.select_one(".ecl-date-block__day")
        month = time_el.select_one(".ecl-date-block__month")
        year  = time_el.select_one(".ecl-date-block__year")
        if not (day and month and year):
            continue

        try:
            event_date = date(
                int(year.get_text(strip=True)),
                MONTH_MAP[month.get_text(strip=True)],
                int(day.get_text(strip=True)),
            )
        except (KeyError, ValueError):
            continue

        classes = time_el.get("class", [])
        if "ecl-date-block--past" in classes:
            status = "past"
        elif "ecl-date-block--ongoing" in classes:
            status = "ongoing"
        else:
            status = "upcoming"

        title_el    = article.select_one(".ecl-content-block__title")
        location_el = article.select_one(".ecl-content-block__secondary-meta-label")
        title = title_el.get_text(strip=True) if title_el else "No title"

        # Client-side filter: only Séjourné's events (page is now unfiltered).
        if "journ" not in title.lower():
            continue

        events.append({
            "title":    title,
            "date":     event_date.isoformat(),
            "location": (location_el.get_text(strip=True) if location_el else ""),
            "status":   status,
            "source":   "calendar",
            "subject":  "",
            "url":      "",
        })
    return events


# ── Transparency Register source ────────────────────────────────────────────────

def scrape_transparency(days_back: int) -> list:
    """Scrape his + cabinet meetings with interest representatives."""
    today  = date.today().isoformat()
    cutoff = (date.today() - timedelta(days=days_back)).isoformat()
    out = []
    for label, host, prefix in TRANSPARENCY_HOSTS:
        try:
            resp = requests.get(TRANSPARENCY_BASE + host, headers=HEADERS, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  Transparency:{label} failed – {e}")
            continue
        soup = BeautifulSoup(resp.text, "html.parser")
        for tr in soup.select("table tr"):
            cells = tr.find_all("td")
            if len(cells) < 4:
                continue
            m = re.match(r"(\d{2})/(\d{2})/(\d{4})", cells[0].get_text(" ", strip=True))
            if not m:
                continue
            dd, mm, yyyy = m.groups()
            event_date = f"{yyyy}-{mm}-{dd}"
            if event_date < cutoff:
                continue
            location = cells[1].get_text(" ", strip=True)
            org      = cells[2].get_text(" ", strip=True)
            subject  = cells[3].get_text(" ", strip=True)
            a = tr.find("a", href=True)
            link = ""
            if a:
                href = a["href"]
                link = href if href.startswith("http") else \
                    "https://ec.europa.eu/transparency-initiative/meetings/" + href.lstrip("/")
            status = ("upcoming" if event_date > today
                      else "ongoing" if event_date == today else "past")
            out.append({
                "title":    f"{prefix} {org}".strip(),
                "date":     event_date,
                "location": location,
                "status":   status,
                "source":   "transparency",
                "subject":  subject,
                "url":      link,
            })
    return out


def get_total_pages(soup: BeautifulSoup) -> int:
    items = soup.select("article.ecl-content-item--inline")
    if len(items) < ITEMS_PER_PAGE:
        return 1
    # Try to extract total count from page text
    for text in soup.find_all(string=True):
        t = text.strip()
        if "of" in t:
            parts = t.split("of")
            if len(parts) == 2:
                try:
                    total = int(parts[1].strip().replace(",", ""))
                    return (total + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
                except ValueError:
                    pass
    return 20  # safe fallback


def scrape_events(days_back: int = 7) -> list:
    """
    Scrape events from (today - days_back) onward.
    Stops paginating as soon as all events on a page are older than the cutoff.
    """
    cutoff = (date.today() - timedelta(days=days_back)).isoformat()

    print(f"Fetching events from {cutoff} onward…")
    all_events = []
    prev_titles = None

    # Source 1: aggregate calendar page (all commissioners, name-filtered).
    for page in range(50):  # hard cap at 50 pages
        if page > 0:
            time.sleep(0.4)
        print(f"  Page {page + 1}…", end="\r")
        try:
            soup = fetch_page(page)
        except requests.RequestException as e:
            print(f"\nWarning: page {page + 1} failed – {e}")
            break

        articles = soup.select("article.ecl-content-item--inline")
        if not articles:
            break
        # Detect non-advancing pagination (site bug: same rows for every page).
        titles = tuple(
            (a.select_one(".ecl-content-block__title").get_text(strip=True)
             if a.select_one(".ecl-content-block__title") else "")
            for a in articles
        )
        if page > 0 and titles == prev_titles:
            break
        prev_titles = titles

        batch = parse_events(soup)
        all_events.extend(e for e in batch if e["date"] >= cutoff)

    cal_count = len(all_events)
    print(f"\nAggregate page: {cal_count} Séjourné events in window.")

    # Source 2: Transparency Register meetings.
    reg = scrape_transparency(days_back)
    print(f"Transparency Register: {len(reg)} meetings in window.")

    # Merge + dedup by (date, title).
    seen, merged = set(), []
    for ev in all_events + reg:
        k = f"{ev['date']}|{ev['title']}"
        if k not in seen:
            seen.add(k)
            merged.append(ev)

    print(f"Found {len(merged)} unique events in the last {days_back} days + upcoming.")
    return merged


# ── Apple Calendar via AppleScript ────────────────────────────────────────────

def _esc(s: str) -> str:
    """Escape a string for embedding in an AppleScript string literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def build_applescript(new_events: list) -> str:
    """
    Build a single AppleScript that creates all new events in one osascript call.
    Uses locale-independent date construction (year/month/day properties).
    Adds two display alarms for upcoming/ongoing events:
      - 1 day before  (trigger interval -1440 minutes)
      - 1 hour before (trigger interval -60 minutes)
    """
    lines = [
        f'tell application "Calendar"',
        f'  -- Ensure our calendar exists',
        f'  if not (exists calendar "{_esc(CALENDAR_NAME)}") then',
        f'    make new calendar with properties {{name:"{_esc(CALENDAR_NAME)}"}}',
        f'  end if',
        f'  set targetCal to calendar "{_esc(CALENDAR_NAME)}"',
        f'  tell targetCal',
    ]

    for ev in new_events:
        yr, mo, dy = ev["date"].split("-")
        title    = _esc(ev["title"])
        location = _esc(ev["location"])
        add_alarms = ev["status"] in ("upcoming", "ongoing")

        lines += [
            f'    -- {ev["date"]}: {ev["title"][:60]}',
            f'    set sd to current date',
            f'    set year of sd to {int(yr)}',
            f'    set month of sd to {int(mo)}',
            f'    set day of sd to {int(dy)}',
            f'    set time of sd to 0',
            f'    set ev to make new event with properties {{summary:"{title}", start date:sd, end date:sd, location:"{location}", allday event:true}}',
        ]

        if add_alarms:
            lines += [
                f'    tell ev',
                f'      make new display alarm with properties {{trigger interval:-1440}}',
                f'      make new display alarm with properties {{trigger interval:-60}}',
                f'    end tell',
            ]

    lines += [
        f'  end tell',
        f'end tell',
    ]
    return "\n".join(lines)


def run_applescript(script: str) -> bool:
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".applescript",
                                    delete=False, encoding="utf-8") as f:
        f.write(script)
        tmp_path = f.name
    try:
        result = subprocess.run(
            ["osascript", tmp_path],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"AppleScript error:\n{result.stderr.strip()}")
            return False
        return True
    finally:
        os.unlink(tmp_path)


def ensure_calendar_exists():
    script = (
        f'tell application "Calendar"\n'
        f'  if exists calendar "{_esc(CALENDAR_NAME)}" then\n'
        f'    return "exists"\n'
        f'  else\n'
        f'    return "missing"\n'
        f'  end if\n'
        f'end tell'
    )
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    status = result.stdout.strip()
    if status == "missing":
        print(f'\n❌ Calendar "{CALENDAR_NAME}" not found in Apple Calendar.')
        print(f'   Please create it manually:')
        print(f'   Calendar app → File → New Calendar → choose iCloud → name it exactly:')
        print(f'   {CALENDAR_NAME}')
        print(f'   Then run this script again.\n')
        sys.exit(1)
    else:
        print(f'Using existing calendar "{CALENDAR_NAME}" ✅')


# ── State management ──────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def event_key(ev: dict) -> str:
    return f"{ev['date']}|{ev['title']}"


# ── Main sync ─────────────────────────────────────────────────────────────────

def push_notification(new_events: list):
    """Send a push notification via ntfy.sh when new events are found."""
    # macOS system notification (always fires)
    upcoming = [e for e in new_events if e["status"] in ("upcoming", "ongoing")]
    count = len(new_events)
    summary = f"{count} new activit{'y' if count == 1 else 'ies'} added"
    details = "\n".join(
        f"• {e['date']}  {e['title'][:60]}" for e in new_events[:5]
    )
    if len(new_events) > 5:
        details += f"\n…and {len(new_events) - 5} more"

    mac_script = (
        f'display notification "{details.splitlines()[0]}" '
        f'with title "Séjourné Calendar" '
        f'subtitle "{summary}"'
    )
    subprocess.run(["osascript", "-e", mac_script], capture_output=True)

    # iPhone push via ntfy.sh (only if topic is configured)
    if not NTFY_TOPIC:
        return
    body = summary + "\n\n" + details
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={
                "Title": "Sejourn - EU Commission",
                "Priority": "high" if upcoming else "default",
                "Tags": "calendar,eu",
            },
            timeout=10,
        )
        print(f"   Push notification sent to ntfy topic '{NTFY_TOPIC}'")
    except requests.RequestException as e:
        print(f"   Push notification failed: {e}")


GITHUB_REPO = "Gabby-Zhang/sejourn-calendar"
GITHUB_ICS  = "sejourn.ics"


def generate_ics(events: list) -> str:
    now = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Sejourn EU Commission Calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Séjourné - EC Calendar",
        "X-WR-TIMEZONE:Europe/Brussels",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        "X-PUBLISHED-TTL:PT1H",
    ]
    for ev in events:
        dt     = ev["date"].replace("-", "")
        # Deterministic UID (matches cloud sync.py) so subscribers aren't re-alerted.
        digest = hashlib.md5(f"{ev['date']}|{ev['title']}".encode("utf-8")).hexdigest()[:10]
        uid    = f"{ev['date']}-{digest}@sejourn-eu"
        title  = ev["title"].replace(",", "\\,").replace("\n", "\\n")
        loc    = ev.get("location", "").replace(",", "\\,")

        desc_parts = []
        if ev.get("subject"):
            desc_parts.append(ev["subject"])
        if ev.get("source") == "transparency":
            desc_parts.append("Source: EU Transparency Register")
        if ev.get("url"):
            desc_parts.append(ev["url"])
        description = "\\n".join(p.replace(",", "\\,") for p in desc_parts)

        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{now}",
            f"DTSTART;VALUE=DATE:{dt}",
            f"DTEND;VALUE=DATE:{dt}",
            f"SUMMARY:{title}",
            f"LOCATION:{loc}",
        ]
        if description:
            lines.append(f"DESCRIPTION:{description}")
        if ev.get("url"):
            lines.append(f"URL:{ev['url']}")
        lines.append("STATUS:CONFIRMED")
        if ev["status"] in ("upcoming", "ongoing"):
            lines += [
                "BEGIN:VALARM",
                "TRIGGER:-P1D",
                "ACTION:DISPLAY",
                f"DESCRIPTION:Tomorrow: {title[:50]}",
                "END:VALARM",
                "BEGIN:VALARM",
                "TRIGGER:-PT1H",
                "ACTION:DISPLAY",
                f"DESCRIPTION:In 1 hour: {title[:50]}",
                "END:VALARM",
            ]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)


def push_ics_to_github(events: list):
    """Generate ICS and push to GitHub so webcal subscribers always have full data."""
    gh_bin = os.path.expanduser("~/bin/gh")
    if not os.path.exists(gh_bin):
        return

    # Get GitHub token via gh CLI
    token_result = subprocess.run(
        [gh_bin, "auth", "token"], capture_output=True, text=True
    )
    token = token_result.stdout.strip()
    if not token:
        return

    ics_content = generate_ics(events)
    encoded     = base64.b64encode(ics_content.encode("utf-8")).decode()
    api_url     = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_ICS}"
    headers     = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    # Get current file SHA (required for updates)
    r = requests.get(api_url, headers=headers, timeout=10)
    sha = r.json().get("sha", "") if r.ok else ""

    payload = {
        "message": f"chore: update calendar {datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "content": encoded,
    }
    if sha:
        payload["sha"] = sha

    r = requests.put(api_url, headers=headers, json=payload, timeout=15)
    if r.ok:
        print(f"   ICS pushed to GitHub ({len(events)} events) → webcal updated ✅")
    else:
        print(f"   GitHub ICS push failed: {r.status_code}")



def sync(events: list, dry_run: bool = False):
    state     = load_state()
    new_events = [e for e in events if event_key(e) not in state]

    print(f"\n{len(events)} events fetched  •  {len(new_events)} new  •  "
          f"{len(events) - len(new_events)} already synced")

    if not new_events:
        print("Nothing to sync — all up to date ✓")
        return

    if dry_run:
        print("\n[DRY RUN] Would create:")
        for e in new_events:
            alarm = "🔔" if e["status"] in ("upcoming", "ongoing") else "  "
            print(f"  {alarm} [{e['status']:8}] {e['date']}  {e['title'][:65]}")
            if e["location"]:
                print(f"              📍 {e['location']}")
        return

    # Batch into chunks of 50 to avoid giant scripts
    CHUNK = 50
    for i in range(0, len(new_events), CHUNK):
        chunk = new_events[i : i + CHUNK]
        print(f"\nWriting events {i+1}–{min(i+CHUNK, len(new_events))} to Apple Calendar…")
        script = build_applescript(chunk)
        if run_applescript(script):
            for e in chunk:
                state[event_key(e)] = datetime.now().isoformat()
            save_state(state)
            print(f"  ✓ {len(chunk)} events written")
        else:
            print("  ✗ Batch failed — check AppleScript error above")
            sys.exit(1)

    print(f"\n✅ Done — {len(new_events)} events added to \"{CALENDAR_NAME}\"")
    print("   Upcoming events have alerts: 1 day before (display) + 1 hour before (display)")
    push_notification(new_events)
    push_ics_to_github(events)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Sync Séjourné EU Commission calendar → Apple Calendar (free, no API key)"
    )
    parser.add_argument("--days-back", type=int, default=7,
                        help="How many past days to include (default: 7)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview events without writing to Calendar")
    parser.add_argument("--reset", action="store_true",
                        help="Clear sync state so all fetched events are re-created")
    parser.add_argument("--ics-only", action="store_true",
                        help="Only update GitHub ICS file, skip Apple Calendar entirely")
    args = parser.parse_args()

    # ── --ics-only 模式：只更新 GitHub ICS，不碰 Apple Calendar ──
    if args.ics_only:
        days = args.days_back if args.days_back != 7 else 180
        print(f"=== Séjourné → GitHub ICS only (past {days} days + upcoming) ===\n")
        events = scrape_events(days_back=days)
        if not events:
            print("No events found.")
            return
        push_ics_to_github(events)
        print(f"\n✅ GitHub ICS updated with {len(events)} events.")
        print("   Apple Calendar and subscriptions are unchanged.")
        return

    # ── 正常模式：同步到 Apple Calendar ──────────────────────────
    print("=== Séjourné EU Commission → Apple Calendar ===\n")

    if args.reset and STATE_FILE.exists():
        STATE_FILE.unlink()
        print("Sync state cleared.\n")

    events = scrape_events(days_back=args.days_back)

    if not events:
        print("No events found.")
        return

    if not args.dry_run:
        ensure_calendar_exists()

    sync(events, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
