#!/usr/bin/env python3
"""
Photo library monitor for Gabriel Attal and Stéphane Séjourné.

Sends push notifications via ntfy.sh when new images are found.

Topics:
  photo-alert-gabriel   → Gabriel Attal
  photo-alert-stephane  → Stéphane Séjourné

Sources:
  - Imago Images        (Playwright + stealth)
  - Alamy               (Playwright + stealth, may get rate-limited)
  - Flickr RenewEurope  (RSS feed, always reliable)
  - Getty Images        (requires free API key — set GETTY_API_KEY env var)

Usage:
  python3 photo_library_monitor.py          # normal run
  python3 photo_library_monitor.py --init   # first run: seed seen list, no notifications
  python3 photo_library_monitor.py --dry-run

Setup:
  pip3 install playwright playwright-stealth feedparser requests
  python3 -m playwright install chromium
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import argparse
import time
from pathlib import Path
from urllib.parse import quote

import requests
import feedparser
from html import unescape

# ── Supabase (optional) ───────────────────────────────────────────────────────
try:
    from supabase import create_client as _sb_create
    _SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    _SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    _sb = _sb_create(_SUPABASE_URL, _SUPABASE_KEY) if _SUPABASE_URL and _SUPABASE_KEY else None
except Exception:
    _sb = None

def _push_alert(person: str, source: str, item: dict):
    """Write new photo find to Supabase photo_alerts table."""
    if not _sb:
        return
    try:
        _sb.table("photo_alerts").upsert({
            "id":     item.get("id", ""),
            "person": person,
            "source": source,
            "title":  item.get("title", ""),
            "url":    item.get("url") or item.get("href", ""),
        }, on_conflict="id").execute()
    except Exception as e:
        log.warning(f"Supabase write failed: {e}")

def clean_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", unescape(str(s))).strip()
from playwright.async_api import async_playwright, Page
from playwright_stealth import Stealth

# ── Config ────────────────────────────────────────────────────────────────────

BASE_DIR  = Path(__file__).parent
SEEN_FILE = BASE_DIR / "photo_library_seen.json"
LOG_FILE  = BASE_DIR / "photo_library_monitor.log"
NTFY_BASE = "https://ntfy.sh"

# EU AV auto-download config (only runs locally on Mac, not on GitHub Actions)
EU_AV_CDN        = "https://ec.europa.eu/avservices/avs/files/video6/repository/prod/photo/store"
EU_AV_DOWNLOAD_DIR = Path.home() / "Pictures" / "Séjourné_EU_AV"

# Optional: set this env var to enable Getty Images API
GETTY_API_KEY = os.environ.get("GETTY_API_KEY", "")

PERSONS = {
    "Gabriel Attal": {
        "topic":   "photo-alert-gabriel",
        "queries": ["Gabriel Attal"],
        "must_contain": ["attal"],
        "search_urls": {
            "Getty Images":       "https://www.gettyimages.co.uk/search/2/image?family=editorial&phrase=gabriel+attal&sort=newest",
            "Imago Images":       "https://www.imago-images.com/search?querystring=Gabriel+Attal&category=all&sortby=date",
            "Alamy":              "https://www.alamy.com/stock-photo/gabriel-attal.html?sortBy=newest",
            "Flickr RenewEurope": "https://www.flickr.com/photos/reneweuropegroup/",
        },
    },
    "Stéphane Séjourné": {
        "topic":   "photo-alert-stephane",
        "queries": ["Séjourné", "Stephane Sejourne"],
        "must_contain": ["séjourné", "sejourne"],
        "eu_av_terms": ["séjourn", "sejourn"],
        "eu_av_person_id": 241448,   # EU AV internal person ID for Stéphane Séjourné
        "ep_multimedia_person_id": 14399,
        "search_urls": {
            "Getty Images":       "https://www.gettyimages.co.uk/search/2/image?family=editorial&phrase=stephane+sejourne&sort=newest",
            "Imago Images":       "https://www.imago-images.com/search?querystring=S%C3%A9journ%C3%A9&category=all&sortby=date",
            "Alamy":              "https://www.alamy.com/stock-photo/stephane-sejourne.html?sortBy=newest",
            "Flickr RenewEurope": "https://www.flickr.com/photos/reneweuropegroup/",
            "EU Audiovisual":     "https://audiovisual.ec.europa.eu/en/search?mediaType=REPORTAGE&sortField=search_date&sortFieldDirection=desc&groupedGenres=NEWS",
            "EP Multimedia":      "https://multimedia.europarl.europa.eu/en/search?tab=photos&person=14399&photoType=25&orderBy=newest&page=1",
        },
    },
}

def is_relevant(title: str, person: str) -> bool:
    """Return True only if the image title contains the person's last name."""
    keywords = PERSONS[person]["must_contain"]
    t = title.lower()
    return any(kw in t for kw in keywords)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE),
    ],
)
log = logging.getLogger(__name__)

# ── State ─────────────────────────────────────────────────────────────────────

def load_seen() -> dict:
    if SEEN_FILE.exists():
        try:
            return json.loads(SEEN_FILE.read_text())
        except Exception:
            return {}
    return {}

def save_seen(seen: dict):
    SEEN_FILE.write_text(json.dumps(seen, indent=2, ensure_ascii=False))

def make_id(*parts) -> str:
    return hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()[:16]

# ── Notifications ─────────────────────────────────────────────────────────────

def _ascii(s: str) -> str:
    """Convert accented characters to ASCII equivalents for HTTP headers."""
    import unicodedata
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")


def _ntfy_post(topic: str, title: str, message: str, click: str):
    """Send an ntfy notification using HTTP headers (avoids JSON body issues)."""
    requests.post(
        f"{NTFY_BASE}/{topic}",
        data=message.encode("utf-8"),
        headers={
            "Title":    _ascii(title),   # HTTP headers must be ASCII
            "Tags":     "camera",
            "Click":    click,
            "Priority": "default",
        },
        timeout=10,
    ).raise_for_status()


def notify_direct(person: str, source: str, title: str, url: str, dry_run: bool):
    """Send one notification per item with a direct link to the specific photo."""
    topic = PERSONS[person]["topic"]
    if dry_run:
        log.info(f"[DRY-RUN] {person} | {source} | {title[:60]} → {url}")
        return
    log.info(f"NOTIFY → {person} | {source} | {title[:60]}")
    try:
        _ntfy_post(topic, f"New photo: {person}", f"[{source}] {title}", url)
    except Exception as e:
        log.warning(f"ntfy error: {e}")


def notify(person: str, source: str, count: int, dry_run: bool):
    """Send one notification per source with a link to the filtered search page."""
    search_url = PERSONS[person]["search_urls"].get(source, "")
    if dry_run:
        log.info(f"[DRY-RUN] {person} | {source} | {count} new photo(s) → {search_url}")
        return
    topic = PERSONS[person]["topic"]
    log.info(f"NOTIFY → {person} | {source} | {count} new photo(s)")
    try:
        _ntfy_post(topic, f"New photo: {person}", f"{source} 上有 {count} 张新图片", search_url)
    except Exception as e:
        log.warning(f"ntfy error: {e}")

# ── Scraper: Imago Images ─────────────────────────────────────────────────────

async def scrape_imago(page: Page, query: str) -> list[dict]:
    url = f"https://www.imago-images.com/search?querystring={quote(query)}&category=all&sortby=date"
    # Keywords that must appear in the image title/alt to be considered relevant
    query_keywords = [w.lower() for w in query.split() if len(w) > 2]
    try:
        await page.goto(url, timeout=25000, wait_until="domcontentloaded")
        await asyncio.sleep(4)
        links = await page.query_selector_all("a[href*='/st/']")
        results = []
        seen_hrefs: set = set()
        for link in links[:30]:
            href = await link.get_attribute("href") or ""
            if not href or href in seen_hrefs or "/st/" not in href:
                continue
            seen_hrefs.add(href)
            img = await link.query_selector("img")
            alt = (await img.get_attribute("alt") or "").strip() if img else ""
            # Only include if at least one keyword from the query appears in the title
            if alt and not any(kw in alt.lower() for kw in query_keywords):
                continue
            title = alt or query
            full = f"https://www.imago-images.com{href}" if href.startswith("/") else href
            clean_href = href.split("?")[0]
            results.append({
                "id":    make_id("imago", clean_href),
                "title": title,
                "url":   full,
            })
        return results
    except Exception as e:
        log.warning(f"Imago error for '{query}': {e}")
        return []

# ── Scraper: Alamy (JSON API — no browser needed) ────────────────────────────

def scrape_alamy(query: str) -> list[dict]:
    """Alamy search via internal JSON API — sorted by DateTaken, recent only."""
    import datetime
    cutoff = datetime.date.today() - datetime.timedelta(days=180)  # last 6 months
    try:
        resp = requests.get(
            "https://www.alamy.com/search-api/v2/search/",
            params={
                "qt":         query,
                "sort":       "DateTaken:desc",
                "langCode":   "en",
                "geoLocations": "gb",
                "pageNumber": 1,
                "pageSize":   30,
            },
            headers={
                "User-Agent": UA,
                "Referer":    "https://www.alamy.com/",
            },
            timeout=15,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        results = []
        for item in items:
            ref  = item.get("altids", {}).get("ref", "")
            uri  = item.get("uri", "")
            cap  = item.get("caption", ref)
            created = item.get("firstcreated", "")  # e.g. "2026-06-02T00:00:00.000Z"
            # Skip photos older than cutoff
            if created:
                try:
                    photo_date = datetime.date.fromisoformat(created[:10])
                    if photo_date < cutoff:
                        continue
                except ValueError:
                    pass
            if not ref or not uri:
                continue
            results.append({
                "id":    make_id("alamy", ref),
                "title": cap[:120],
                "url":   uri,
            })
        return results
    except Exception as e:
        log.warning(f"Alamy error for '{query}': {e}")
        return []

# ── Scraper: Flickr RenewEurope ───────────────────────────────────────────────

def scrape_flickr_reneweurope(query: str) -> list[dict]:
    """RSS feed — no browser needed, always reliable."""
    feed_url = "https://www.flickr.com/photos/reneweuropegroup/feed/"
    try:
        feed = feedparser.parse(feed_url)
        q_lower = query.lower()
        results = []
        for entry in feed.entries[:30]:
            title   = entry.get("title", "")
            summary = entry.get("summary", "")
            if q_lower in title.lower() or q_lower in summary.lower():
                link = entry.get("link", "")
                results.append({
                    "id":    make_id("flickr", link),
                    "title": title,
                    "url":   link,
                })
        return results
    except Exception as e:
        log.warning(f"Flickr error for '{query}': {e}")
        return []

# ── Scraper: EU Audiovisual Service ──────────────────────────────────────────

def _eu_av_scan_reportage_photos(api_url: str, base_ref: str, n_photos: int,
                                  search_terms: list) -> bool:
    """Fetch all individual photos for a reportage by enumerating refs in parallel.

    Uses ThreadPoolExecutor to query each photo ref concurrently.
    Returns True if any photo caption contains a search term.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def fetch_caption(photo_num: int):
        ref = f"{base_ref}/00-{photo_num:02d}"
        try:
            r = requests.get(api_url, params={"fl": "summary_json", "wt": "json", "ref": ref},
                             timeout=8)
            docs = r.json().get("response", {}).get("docs", [])
            if docs:
                return unescape(str(docs[0].get("summary_json", "") or "")).lower()
        except Exception:
            pass
        return ""

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(fetch_caption, i): i for i in range(1, n_photos + 1)}
        for future in as_completed(futures):
            caption = future.result()
            if caption and any(t in caption for t in search_terms):
                return True
    return False


def eu_av_download_photos(base_ref: str, search_terms: list, shoot_date: str = "", title: str = "") -> int:
    """Download all SS photos from a reportage to ~/Pictures/Séjourné_EU_AV/.

    Only runs locally (skipped on GitHub Actions where HOME is /home/runner).
    Returns number of photos downloaded.
    """
    import platform
    if os.environ.get("GITHUB_ACTIONS"):
        return 0  # never run on CI

    AV_API = "https://gfdwwnbuul.execute-api.eu-west-1.amazonaws.com/avsportal/avsportal"
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Build folder name: base_ref + date + sanitised title
    date_str = shoot_date[:8] if shoot_date else ""
    safe_title = re.sub(r"[^\w\s\-]", "", title)[:40].strip().replace(" ", "_")
    folder_name = "_".join(filter(None, [base_ref.replace("/", "-"), date_str, safe_title]))
    save_dir = EU_AV_DOWNLOAD_DIR / folder_name
    save_dir.mkdir(parents=True, exist_ok=True)

    # First, get total number of photos in this reportage
    try:
        r = requests.get(AV_API, params={"fl": "childobjects", "wt": "json", "ref": f"{base_ref}/00-01"}, timeout=8)
        # childobjects not on individual photos; query the reportage ref
        r2 = requests.get(AV_API, params={"fl": "childobjects", "wt": "json", "ref": base_ref, "type": "REPORTAGE"}, timeout=8)
        docs = r2.json().get("response", {}).get("docs", [])
        n_photos = int((docs[0].get("childobjects") or 0) if docs else 0)
        if n_photos == 0:
            # fallback: scan until 404
            n_photos = 99
    except Exception:
        n_photos = 99

    downloaded = 0

    def fetch_and_save(photo_num: int):
        ref = f"{base_ref}/00-{photo_num:02d}"
        filepath = save_dir / f"{base_ref.replace('/', '-')}_00-{photo_num:02d}.jpg"
        if filepath.exists():
            return 0  # already downloaded
        try:
            r = requests.get(AV_API, params={"fl": "summary_json,media_json", "wt": "json", "ref": ref}, timeout=8)
            docs = r.json().get("response", {}).get("docs", [])
            if not docs:
                return -1  # no such photo
            doc = docs[0]
            caption = unescape(str(doc.get("summary_json", "") or "")).lower()
            if not any(t in caption for t in search_terms):
                return 0  # not SS photo
            media = doc.get("media_json", {}) or {}
            path = (media.get("ORIGINAL") or media.get("HIGH") or {}).get("PATH", "")
            if not path:
                return 0
            img = requests.get(EU_AV_CDN + path, timeout=30, headers={"User-Agent": UA})
            if img.status_code == 200 and img.headers.get("content-type", "").startswith("image"):
                filepath.write_bytes(img.content)
                return 1
        except Exception as e:
            log.debug(f"EU AV download {ref}: {e}")
        return 0

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(fetch_and_save, i) for i in range(1, n_photos + 1)]
        missing_streak = 0
        for future in futures:
            result = future.result()
            if result == -1:
                missing_streak += 1
                if missing_streak >= 3:
                    break  # no more photos
            else:
                missing_streak = 0
                if result == 1:
                    downloaded += 1

    if downloaded > 0:
        log.info(f"  EU AV download: {downloaded} photo(s) → {save_dir}")
    return downloaded


def scrape_eu_audiovisual(search_terms: list, person_id=None) -> list[dict]:
    """EU Audiovisual Service API — no browser needed.

    Pass 1 — REPORTAGE / VIDEO / PHOTO (top 100 each):
      Match by person_id in pers_json OR keywords in title/summary/caption.

    Pass 2 — Photo caption scan for unmatched REPORTAGEs (top 20):
      For reportages not matched in pass 1, fetch every individual photo ref in
      parallel and check captions. Catches group events (e.g. weekly College
      meetings) where individual photo captions name the person but the reportage
      title/tags do not.
    """
    AV_API = "https://gfdwwnbuul.execute-api.eu-west-1.amazonaws.com/avsportal/avsportal"
    PORTAL_PHOTO = "https://audiovisual.ec.europa.eu/en/media/photo"
    PORTAL_VIDEO = "https://audiovisual.ec.europa.eu/en/media/video"
    results = []
    matched_doc_refs: set = set()
    try:
        # ── Pass 1: standard type queries ────────────────────────────────────
        for media_type in ["REPORTAGE", "PHOTO", "VIDEO"]:
            params = {
                "fl": "type,ref,doc_ref,titles_json,shootstartdate,summary_json,pers_json",
                "hasMedia": 1, "wt": "json", "index": 1,
                "pagesize": 100, "type": media_type,
            }
            docs = requests.get(AV_API, params=params, timeout=20).json().get("response", {}).get("docs", [])
            seen_refs: set = set()
            for doc in docs:
                titles  = doc.get("titles_json", {}) or {}
                summary = doc.get("summary_json", {}) or {}
                pers    = doc.get("pers_json", []) or []

                person_match = person_id and any(
                    str(p.get("id", "")) == str(person_id) for p in pers
                )
                combined = unescape(" ".join(str(v) for v in list(titles.values()) + list(summary.values()))).lower()
                keyword_match = any(t in combined for t in search_terms)

                if not person_match and not keyword_match:
                    continue

                ref = doc.get("ref", "")
                doc_ref = doc.get("doc_ref") or ref.split("/")[0]
                dedup_key = doc_ref if media_type == "PHOTO" else ref
                if dedup_key in seen_refs:
                    continue
                seen_refs.add(dedup_key)
                matched_doc_refs.add(doc_ref)

                url = f"{PORTAL_VIDEO}/{doc_ref}" if media_type == "VIDEO" else f"{PORTAL_PHOTO}/{doc_ref}"
                title = clean_html(next(iter(titles.values()), ref))
                id_key = f"eu_av_photo|{doc_ref}" if media_type == "PHOTO" else ref
                results.append({
                    "id": make_id("eu_av", id_key), "title": title, "url": url,
                    "eu_av_base_ref": doc_ref,
                    "eu_av_date": str(doc.get("shootstartdate", ""))[:8],
                })

        # ── Pass 2: exhaustive per-photo caption scan for unmatched REPORTAGEs ─
        rep_params = {
            "fl": "ref,childobjects,titles_json",
            "hasMedia": 1, "wt": "json", "index": 1,
            "pagesize": 20, "type": "REPORTAGE",
        }
        reportages = requests.get(AV_API, params=rep_params, timeout=20).json().get("response", {}).get("docs", [])
        for doc in reportages:
            ref = doc.get("ref", "")
            base_ref = ref.split("/")[0]
            if base_ref in matched_doc_refs:
                continue
            n_photos = int(doc.get("childobjects") or 0)
            if n_photos < 1:
                continue
            if _eu_av_scan_reportage_photos(AV_API, base_ref, n_photos, search_terms):
                titles = doc.get("titles_json", {}) or {}
                title = clean_html(next(iter(titles.values()), base_ref))
                url = f"{PORTAL_PHOTO}/{base_ref}"
                results.append({
                    "id":              make_id("eu_av", f"eu_av_scan|{base_ref}"),
                    "title":           title,
                    "url":             url,
                    "confirmed_match": True,  # found via caption scan, skip title filter
                    "eu_av_base_ref":  base_ref,
                    "eu_av_date":      str(doc.get("shootstartdate", ""))[:8],
                })
                matched_doc_refs.add(base_ref)
                log.info(f"  EU AV pass-2 match: {base_ref} ({title[:50]})")

    except Exception as e:
        log.warning(f"EU AV error: {e}")
    return results

# ── Scraper: EP Multimedia Centre ────────────────────────────────────────────

async def scrape_ep_multimedia(page: Page, person_id: int) -> list[dict]:
    """EU Parliament Multimedia Centre — photos filtered by person ID."""
    EP_BASE = "https://multimedia.europarl.europa.eu"
    search_url = f"{EP_BASE}/en/search?tab=photos&person={person_id}&photoType=25&orderBy=newest&page=1&q="
    results = []
    next_data_holder: dict = {}

    async def on_response(resp):
        if "_next/data" in resp.url and "search.json" in resp.url:
            try:
                next_data_holder["data"] = await resp.json()
            except Exception:
                pass

    page.on("response", on_response)
    try:
        await page.goto(search_url, timeout=25000, wait_until="domcontentloaded")
        await asyncio.sleep(6)
        nd = next_data_holder.get("data")
        if nd:
            photos = (
                nd.get("pageProps", {})
                  .get("results", {})
                  .get("photos", {})
                  .get("content", [])
            )
            for item in photos[:20]:
                bid = item.get("mediaBusinessId", "")
                if not bid:
                    continue
                title = item.get("title", bid)
                url   = f"{EP_BASE}/en/photoset/{bid}"
                results.append({
                    "id":    make_id("ep_multimedia", bid),
                    "title": title,
                    "url":   url,
                })
        else:
            log.warning("EP Multimedia: no _next/data captured")
    except Exception as e:
        log.warning(f"EP Multimedia error: {e}")
    finally:
        page.remove_listener("response", on_response)
    return results

# ── Scraper: Getty Images (Playwright + Stealth) ──────────────────────────────

async def scrape_getty(page: Page, query: str) -> list[dict]:
    """Getty Images editorial search via stealth browser."""
    encoded = quote(query)
    url = f"https://www.gettyimages.co.uk/search/2/image?family=editorial&groupbyevent=false&phrase={encoded}&sort=newest"
    try:
        await page.goto(url, timeout=30000, wait_until="domcontentloaded")
        await asyncio.sleep(6)

        # Check for bot-wall redirect
        current_url = page.url
        if "bot-wall" in current_url or "captcha" in current_url:
            log.warning(f"Getty: bot-wall detected for '{query}'")
            return []

        html = await page.content()

        # Getty image detail links look like /detail/news-photo/XXXXXXXXX
        hrefs = re.findall(r'"(/detail/[^"]{10,})"', html)
        # Also try anchor tags
        links = await page.query_selector_all("a[href*='/detail/']")

        results = []
        seen_hrefs: set = set()

        for link in links[:20]:
            href = await link.get_attribute("href") or ""
            if not href or href in seen_hrefs:
                continue
            if "/detail/" not in href:
                continue
            seen_hrefs.add(href)
            img = await link.query_selector("img")
            alt = (await img.get_attribute("alt") or "").strip() if img else ""
            title = alt or query
            full = f"https://www.gettyimages.co.uk{href}" if href.startswith("/") else href
            results.append({
                "id":    make_id("getty", href.split("?")[0]),
                "title": title,
                "url":   full,
            })

        # Fallback: parse hrefs from raw HTML
        if not results:
            for href in hrefs[:20]:
                if href in seen_hrefs:
                    continue
                seen_hrefs.add(href)
                slug = href.split("/detail/")[-1].split("?")[0]
                title = slug.replace("-", " ").replace("news-photo/", "").title()[:80]
                full = f"https://www.gettyimages.co.uk{href}"
                results.append({
                    "id":    make_id("getty", href),
                    "title": title,
                    "url":   full,
                })

        return results
    except Exception as e:
        log.warning(f"Getty error for '{query}': {e}")
        return []


# ── Scraper: Getty Images API (optional) ──────────────────────────────────────

def scrape_getty_api(query: str) -> list[dict]:
    """Requires GETTY_API_KEY env var (paid Getty account)."""
    if not GETTY_API_KEY:
        return []
    try:
        resp = requests.get(
            "https://api.gettyimages.com/v3/search/images/editorial",
            params={
                "phrase":        query,
                "sort_order":    "newest",
                "page_size":     20,
                "editorial_segments": "news",
            },
            headers={
                "Api-Key":      GETTY_API_KEY,
                "Accept":       "application/json",
                "User-Agent":   UA,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        for img in data.get("images", []):
            img_id   = img.get("id", "")
            title    = img.get("title", query)
            url      = f"https://www.gettyimages.co.uk/detail/{img_id}"
            results.append({
                "id":    make_id("getty", img_id),
                "title": title,
                "url":   url,
            })
        return results
    except Exception as e:
        log.warning(f"Getty API error for '{query}': {e}")
        return []

# ── Orchestration ─────────────────────────────────────────────────────────────

async def run_checks(dry_run: bool, init_mode: bool):
    seen = load_seen()
    total_new = 0

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)  # non-headless: bypasses Getty bot-wall
        ctx = await browser.new_context(
            user_agent=UA,
            locale="fr-FR",
            extra_http_headers={"Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"},
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()
        await Stealth().apply_stealth_async(page)

        for person, cfg in PERSONS.items():
            key = person.replace(" ", "_")
            seen_ids: set = set(seen.get(key, []))

            for query in cfg["queries"]:
                log.info(f"── Checking: {query} ──")

                def process(results, source):
                    nonlocal total_new
                    new_count = 0
                    skipped = 0
                    for item in results:
                        if item["id"] in seen_ids:
                            continue
                        if init_mode:
                            seen_ids.add(item["id"])
                        else:
                            # Only add to seen_ids AFTER sending notification.
                            # If not relevant, skip silently this run (do NOT cache)
                            # so we can re-evaluate if the title changes or we fix logic.
                            if not item.get("confirmed_match") and not is_relevant(item["title"], person):
                                skipped += 1
                                continue
                            _push_alert(person, source, item)
                            seen_ids.add(item["id"])
                            new_count += 1
                            # Auto-download EU AV photos for Séjourné (local Mac only)
                            if source == "EU Audiovisual" and item.get("eu_av_base_ref") and not os.environ.get("GITHUB_ACTIONS"):
                                eu_av_terms = cfg.get("eu_av_terms", [])
                                n_dl = eu_av_download_photos(
                                    item["eu_av_base_ref"],
                                    eu_av_terms,
                                    shoot_date=item.get("eu_av_date", ""),
                                    title=item.get("title", ""),
                                )
                                if n_dl > 0 and not dry_run:
                                    requests.post(
                                        f"{NTFY_BASE}/{cfg['topic']}",
                                        headers={
                                            "Title": "📥 EU AV 照片已下载",
                                            "Message": f"{n_dl} 张照片已保存到 ~/Pictures/Séjourné_EU_AV/\n{item.get('title', '')}",
                                            "Priority": "low",
                                            "Tags": "floppy_disk",
                                        },
                                        timeout=10,
                                    )
                    if not init_mode and new_count > 0:
                        notify(person, source, new_count, dry_run)
                        total_new += new_count
                    if skipped:
                        log.info(f"  {source}: {skipped} filtered out (name not in title)")

                # -- Getty (Playwright + Stealth) --
                getty_results = await scrape_getty(page, query)
                log.info(f"  Getty Images: {len(getty_results)} result(s)")
                process(getty_results, "Getty Images")
                await asyncio.sleep(3)

                # -- Imago (Playwright) --
                imago_results = await scrape_imago(page, query)
                log.info(f"  Imago: {len(imago_results)} result(s)")
                process(imago_results, "Imago Images")
                await asyncio.sleep(3)

                # -- Alamy (Playwright, may be rate-limited) --
                alamy_results = scrape_alamy(query)
                log.info(f"  Alamy: {len(alamy_results)} result(s)")
                process(alamy_results, "Alamy")
                await asyncio.sleep(3)

                # -- Flickr (RSS, no browser) --
                flickr_results = scrape_flickr_reneweurope(query)
                log.info(f"  Flickr RenewEurope: {len(flickr_results)} result(s)")
                process(flickr_results, "Flickr RenewEurope")

                # -- EU Audiovisual (Séjourné only, no browser needed) --
                # API already filters by name — skip title filter here
                eu_terms = cfg.get("eu_av_terms")
                if eu_terms:
                    eu_results = scrape_eu_audiovisual(eu_terms, person_id=cfg.get("eu_av_person_id"))
                    log.info(f"  EU Audiovisual: {len(eu_results)} result(s)")
                    for item in eu_results:
                        if item["id"] not in seen_ids:
                            seen_ids.add(item["id"])
                            if not init_mode:
                                notify_direct(person, "EU Audiovisual", item["title"], item["url"], dry_run)
                                _push_alert(person, "EU Audiovisual", item)
                                total_new += 1

                # -- EP Multimedia Centre (Séjourné only, Playwright) --
                ep_person_id = cfg.get("ep_multimedia_person_id")
                if ep_person_id:
                    ep_results = await scrape_ep_multimedia(page, ep_person_id)
                    log.info(f"  EP Multimedia: {len(ep_results)} result(s)")
                    for item in ep_results:
                        if item["id"] not in seen_ids:
                            seen_ids.add(item["id"])
                            if not init_mode:
                                notify_direct(person, "EP Multimedia", item["title"], item["url"], dry_run)
                                total_new += 1

                # -- Getty API (optional) --
                if GETTY_API_KEY:
                    getty_api_results = scrape_getty_api(query)
                    log.info(f"  Getty API: {len(getty_api_results)} result(s)")
                    process(getty_api_results, "Getty Images")

            seen[key] = list(seen_ids)

        await browser.close()

    if not dry_run:
        save_seen(seen)

    if init_mode:
        count = sum(len(v) for v in seen.values())
        log.info(f"Init complete — seeded {count} item IDs (no notifications sent)")
    else:
        log.info(f"Done — {total_new} new item(s) found")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Photo library monitor — Attal & Séjourné")
    parser.add_argument("--init",    action="store_true",
                        help="Seed seen-list without sending notifications")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print new items without notifications or saving state")
    args = parser.parse_args()

    if args.init:
        log.info("=== INIT MODE ===")
    elif args.dry_run:
        log.info("=== DRY-RUN MODE ===")
    else:
        log.info("=== Photo Library Monitor ===")

    asyncio.run(run_checks(dry_run=args.dry_run, init_mode=args.init))


if __name__ == "__main__":
    main()
