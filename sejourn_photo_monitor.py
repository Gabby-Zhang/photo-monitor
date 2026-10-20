#!/usr/bin/env python3
"""
Monitors the EU Audiovisual Service for new photos (REPORTAGE, PHOTO) and
videos (VIDEO) featuring Stéphane Séjourné, pushes a Markdown log to GitHub,
and sends an ntfy.sh push notification.

Requirements:
  pip3 install requests

Usage:
  python3 sejourn_photo_monitor.py            # check for new items
  python3 sejourn_photo_monitor.py --dry-run  # preview without pushing
  python3 sejourn_photo_monitor.py --reset    # clear state and reprocess all
"""

import base64
import json
import os
import re
import subprocess
import sys
import argparse
from datetime import datetime, date
from html import unescape
from pathlib import Path

import requests

# ── Config ─────────────────────────────────────────────────────────────────────

AV_API_URL   = "https://gfdwwnbuul.execute-api.eu-west-1.amazonaws.com/avsportal/avsportal"
PHOTOS_BASE       = "https://ec.europa.eu/avservices/avs/files/video6/repository/prod/photo/store"
PORTAL_PHOTO_BASE = "https://audiovisual.ec.europa.eu/en/media/photo"
PORTAL_VIDEO_BASE = "https://audiovisual.ec.europa.eu/en/media/video"
SEARCH_URL        = (
    "https://audiovisual.ec.europa.eu/en/search"
    "?mediaType=REPORTAGE&sortField=search_date&sortFieldDirection=desc&groupedGenres=NEWS"
)

GITHUB_REPO  = "Gabby-Zhang/sejourn-calendar"
GITHUB_FILE  = "sejourn_photos.md"

NTFY_TOPIC   = "ss-calendar-update"   # must match your ntfy subscription

STATE_FILE   = Path(__file__).parent / "photo_sync_state.json"

# Items per type fetched per run. 3 types × 100 = 300 API records checked.
FETCH_COUNT  = 100

MEDIA_TYPES  = ["REPORTAGE", "PHOTO", "VIDEO"]

TYPE_EMOJI   = {"REPORTAGE": "📷", "PHOTO": "📷", "VIDEO": "🎥"}

SEARCH_TERMS = ["séjourn", "sejourn"]   # checked case-insensitively

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# ── AV Portal API ──────────────────────────────────────────────────────────────

def fetch_latest_items() -> list:
    """Fetch the latest FETCH_COUNT items for each media type separately and merge."""
    all_docs = []
    for media_type in MEDIA_TYPES:
        params = {
            "fl":       "type,ref,titles_json,shootstartdate,summary_json,media_json,languages",
            "hasMedia": 1,
            "wt":       "json",
            "index":    1,
            "pagesize": FETCH_COUNT,
            "type":     media_type,
        }
        resp = requests.get(AV_API_URL, params=params, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        docs = resp.json().get("response", {}).get("docs", [])
        print(f"  {media_type}: {len(docs)} items fetched")
        all_docs.extend(docs)
    return all_docs


def mentions_sejourn(doc: dict) -> bool:
    texts = []
    for field in ("titles_json", "summary_json"):
        val = doc.get(field, {})
        if isinstance(val, dict):
            texts.extend(val.values())
        elif isinstance(val, str):
            texts.append(val)
    combined = unescape(" ".join(str(t) for t in texts)).lower()
    return any(term in combined for term in SEARCH_TERMS)


# ── Data helpers ───────────────────────────────────────────────────────────────

def base_ref(ref: str) -> str:
    """'P-068123/00-01'  →  'P-068123'  (videos already have no slash)"""
    return ref.split("/")[0]


def portal_url(doc: dict) -> str:
    ref = base_ref(doc["ref"])
    if doc.get("type") == "VIDEO":
        return f"{PORTAL_VIDEO_BASE}/{ref}"
    return f"{PORTAL_PHOTO_BASE}/{ref}"


def thumbnail_url(doc: dict) -> str:
    media = doc.get("media_json", {})
    if doc.get("type") == "VIDEO":
        # Structure: media_json["16:9"][lang]["THUMB"] — full URL
        aspect = media.get("16:9", {})
        for lang in ("INT", "MS", "EN"):
            thumb = aspect.get(lang, {}).get("THUMB", "")
            if thumb:
                return thumb.split("?")[0]   # strip cache-busting params
        return ""
    # PHOTO / REPORTAGE: relative PATH needs base prefix
    for res in ("THUMB", "LOW", "MED"):
        path = media.get(res, {}).get("PATH", "")
        if path:
            return PHOTOS_BASE + path
    return ""


def parse_date(raw: str) -> str:
    """'20260527 19:30'  →  '2026-05-27'"""
    try:
        return datetime.strptime(raw[:8], "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return raw[:10]


def clean_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", unescape(str(s))).strip()


def get_title(doc: dict) -> str:
    titles = doc.get("titles_json", {})
    raw = (
        titles.get("EN")
        or titles.get("FR")
        or (next(iter(titles.values()), "") if titles else "")
    )
    return clean_html(raw) or "No title"


def get_summary(doc: dict) -> str:
    s = doc.get("summary_json", {})
    raw = (
        s.get("EN") or s.get("FR") or (next(iter(s.values()), "") if isinstance(s, dict) else "")
    )
    return clean_html(raw)


# ── State ──────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"seen": {}, "all_items": []}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ── Markdown ───────────────────────────────────────────────────────────────────

def build_markdown(all_items: list) -> str:
    sorted_items = sorted(all_items, key=lambda x: x["date"], reverse=True)
    lines = [
        "# Séjourné — EU Audiovisual Media",
        "",
        f"_Last updated: {date.today().isoformat()} · {len(all_items)} item(s) logged_",
        "",
        "| Date | Type | Title | Link |",
        "|------|------|-------|------|",
    ]
    for p in sorted_items:
        emoji = TYPE_EMOJI.get(p.get("type", ""), "📷")
        title = p["title"].replace("|", "\\|")
        lines.append(f"| {p['date']} | {emoji} | {title} | [View]({p['url']}) |")
    lines += [
        "",
        "---",
        f"_Source: [EU Audiovisual Service]({SEARCH_URL})_",
        "",
    ]
    return "\n".join(lines)


# ── GitHub ─────────────────────────────────────────────────────────────────────

def push_to_github(markdown: str):
    gh_bin = os.path.expanduser("~/bin/gh")
    if not os.path.exists(gh_bin):
        print("   gh CLI not found — skipping GitHub push")
        return

    token = subprocess.run(
        [gh_bin, "auth", "token"], capture_output=True, text=True
    ).stdout.strip()
    if not token:
        print("   No GitHub token — skipping push")
        return

    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"
    gh_headers = {
        "Authorization": f"token {token}",
        "Accept":        "application/vnd.github.v3+json",
    }

    r = requests.get(api_url, headers=gh_headers, timeout=10)
    sha = r.json().get("sha", "") if r.ok else ""

    payload = {
        "message": f"chore: update photo log {datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "content": base64.b64encode(markdown.encode("utf-8")).decode(),
    }
    if sha:
        payload["sha"] = sha

    r = requests.put(api_url, headers=gh_headers, json=payload, timeout=15)
    if r.ok:
        print("   Markdown pushed to GitHub ✅")
    else:
        print(f"   GitHub push failed: {r.status_code} {r.text[:80]}")


# ── Notifications ──────────────────────────────────────────────────────────────

def _osa_quote(s: str) -> str:
    """Escape a string for use inside an AppleScript "..." literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def send_notification(new_items: list) -> bool:
    """Returns True if the ntfy push went through (or ntfy is disabled)."""
    count   = len(new_items)
    subject = f"{count} new Séjourné item{'s' if count > 1 else ''}"
    lines   = [
        f"• {TYPE_EMOJI.get(p.get('type',''), '📷')} {p['date']}  {p['title'][:55]}"
        for p in new_items[:5]
    ]
    body = subject + "\n\n" + "\n".join(lines)
    if count > 5:
        body += f"\n…and {count - 5} more"

    # macOS system notification
    mac_script = (
        f'display notification "{_osa_quote(new_items[0]["title"][:80])}" '
        f'with title "Séjourné AV" '
        f'subtitle "{_osa_quote(subject)}"'
    )
    subprocess.run(["osascript", "-e", mac_script], capture_output=True)

    # ntfy.sh push notification
    if not NTFY_TOPIC:
        return True
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={
                "Title":    "Sejourne - New Media",
                "Priority": "default",
                "Tags":     "camera,eu",
                "Click":    SEARCH_URL,
            },
            timeout=10,
        ).raise_for_status()
        print(f"   ntfy notification sent ✅")
        return True
    # Broad on purpose: a non-ASCII header raised UnicodeEncodeError, not a
    # RequestException, and crashed the run after state was already saved.
    except Exception as e:
        print(f"   ntfy failed: {e}")
        return False


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Monitor EU Audiovisual Service for new Séjourné photos and videos"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without writing state or pushing")
    parser.add_argument("--reset", action="store_true",
                        help="Clear sync state so all matched items are treated as new")
    args = parser.parse_args()

    print("=== Séjourné Media Monitor — EU Audiovisual Service ===\n")

    if args.reset and STATE_FILE.exists():
        STATE_FILE.unlink()
        print("State cleared.\n")

    state     = load_state()
    seen      = state.get("seen", {})       # ref → first-seen ISO timestamp
    all_items = state.get("all_items", [])  # full history list

    print(f"Fetching latest {FETCH_COUNT} items per type (REPORTAGE / PHOTO / VIDEO)…")
    try:
        docs = fetch_latest_items()
    except requests.RequestException as e:
        print(f"API error: {e}")
        sys.exit(1)

    print(f"\nTotal {len(docs)} items fetched, filtering for Séjourné…")
    matched = [d for d in docs if mentions_sejourn(d)]
    print(f"Matched {len(matched)} Séjourné item(s)")

    new_items = []
    for doc in matched:
        ref = doc["ref"]
        if ref in seen:
            continue
        item = {
            "ref":       ref,
            "type":      doc.get("type", "REPORTAGE"),
            "date":      parse_date(doc.get("shootstartdate", "")),
            "title":     get_title(doc),
            "summary":   get_summary(doc),
            "url":       portal_url(doc),
            "thumbnail": thumbnail_url(doc),
        }
        new_items.append(item)
        seen[ref] = datetime.now().isoformat()
        if not any(p["ref"] == ref for p in all_items):
            all_items.append(item)

    already = len(matched) - len(new_items)
    print(f"\n{len(matched)} matched  •  {len(new_items)} new  •  {already} already seen")

    if not new_items:
        print("Nothing new — all up to date ✓")
        return

    if args.dry_run:
        print("\n[DRY RUN] New items found:")
        for p in new_items:
            emoji = TYPE_EMOJI.get(p["type"], "📷")
            print(f"  {emoji} [{p['date']}] [{p['type']}] {p['title'][:65]}")
            print(f"    {p['url']}")
        return

    # Notify first, persist after: if the push fails, the items stay unseen
    # and the next hourly run retries them instead of silently dropping them
    # (2026-10: 12 items were lost to a notify crash under the old order).
    if not send_notification(new_items):
        print("\n⚠️  Notification failed — state NOT saved, will retry next run")
        sys.exit(1)

    state["seen"]      = seen
    state["all_items"] = all_items
    save_state(state)

    # Build markdown and push
    markdown = build_markdown(all_items)
    push_to_github(markdown)

    print(f"\n✅ Done — {len(new_items)} new Séjourné item(s) logged")
    for p in new_items:
        emoji = TYPE_EMOJI.get(p["type"], "📷")
        print(f"  {emoji} [{p['date']}] {p['title'][:70]}")


if __name__ == "__main__":
    main()
