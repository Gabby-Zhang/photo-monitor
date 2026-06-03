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

# Optional: set this env var to enable Getty Images API
GETTY_API_KEY = os.environ.get("GETTY_API_KEY", "")

PERSONS = {
    "Gabriel Attal": {
        "topic":   "photo-alert-gabriel",
        "queries": ["Gabriel Attal"],
        "must_contain": ["attal"],
        "search_urls": {
            "Getty Images":       "https://www.gettyimages.co.uk/search/2/image?family=editorial&phrase=gabriel+attal&sort=newest",
            "Imago Images":       "https://www.imago-images.com/search?q=Gabriel+Attal&sortby=date",
            "Alamy":              "https://www.alamy.com/stock-photo/gabriel-attal.html?sortBy=newest",
            "Flickr RenewEurope": "https://www.flickr.com/photos/reneweuropegroup/",
        },
    },
    "Stephane Sejourne": {
        "topic":   "photo-alert-stephane",
        "queries": ["Stéphane Séjourné", "Stephane Sejourne"],
        "must_contain": ["séjourné", "sejourne"],
        "eu_av_terms": ["séjourn", "sejourn"],
        "ep_multimedia_person_id": 14399,
        "search_urls": {
            "Getty Images":       "https://www.gettyimages.co.uk/search/2/image?family=editorial&phrase=stephane+sejourne&sort=newest",
            "Imago Images":       "https://www.imago-images.com/search?q=Stephane+Sejourne&sortby=date",
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

def _ntfy_post(topic: str, title: str, message: str, click: str):
    """Send an ntfy notification using HTTP headers (avoids JSON body issues)."""
    requests.post(
        f"{NTFY_BASE}/{topic}",
        data=message.encode("utf-8"),
        headers={
            "Title":    title,
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
    url = f"https://www.imago-images.com/search?q={quote(query)}"
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

# ── Scraper: Alamy ────────────────────────────────────────────────────────────

async def scrape_alamy(page: Page, query: str) -> list[dict]:
    url = f"https://www.alamy.com/stock-photo/{quote(query.lower().replace(' ', '-'))}.html?sortBy=newest"
    try:
        await page.goto(url, timeout=25000, wait_until="domcontentloaded")
        await asyncio.sleep(5)
        # Check if we got blocked
        title = await page.title()
        if "403" in title or "forbidden" in title.lower() or "captcha" in title.lower():
            log.warning(f"Alamy blocked (403/captcha) for '{query}'")
            return []

        html = await page.content()
        # Alamy individual image URLs have long slugs + image ID
        # Pattern: /stock-photo/some-long-title-IMAGEID.html
        hrefs = re.findall(
            r'href="(/stock-photo/[^"]{40,}\.html[^"]*)"',
            html,
        )
        results = []
        seen: set = set()
        for href in hrefs[:20]:
            if href in seen:
                continue
            seen.add(href)
            # Try to get alt text
            full = f"https://www.alamy.com{href}"
            # Extract a title from the slug itself
            from urllib.parse import unquote
            slug = href.split("/stock-photo/")[-1].split(".html")[0]
            title = unquote(slug).replace("-", " ").title()
            results.append({
                "id":    make_id("alamy", href.split("?")[0]),
                "title": title[:80],
                "url":   full,
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

def scrape_eu_audiovisual(search_terms: list) -> list[dict]:
    """EU Audiovisual Service API — no browser needed."""
    AV_API = "https://gfdwwnbuul.execute-api.eu-west-1.amazonaws.com/avsportal/avsportal"
    PORTAL_PHOTO = "https://audiovisual.ec.europa.eu/en/media/photo"
    PORTAL_VIDEO = "https://audiovisual.ec.europa.eu/en/media/video"
    results = []
    try:
        for media_type in ["REPORTAGE", "PHOTO", "VIDEO"]:
            params = {
                "fl": "type,ref,titles_json,shootstartdate,summary_json",
                "hasMedia": 1, "wt": "json", "index": 1,
                "pagesize": 100, "type": media_type,
            }
            docs = requests.get(AV_API, params=params, timeout=20).json().get("response", {}).get("docs", [])
            seen_urls: set = set()
        for doc in docs:
                titles = doc.get("titles_json", {}) or {}
                summary = doc.get("summary_json", {}) or {}
                combined = unescape(" ".join(str(v) for v in list(titles.values()) + list(summary.values()))).lower()
                if not any(t in combined for t in search_terms):
                    continue
                ref = doc.get("ref", "")
                base_ref = ref.split("/")[0]
                url = f"{PORTAL_VIDEO}/{base_ref}" if media_type == "VIDEO" else f"{PORTAL_PHOTO}/{base_ref}"
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                title = clean_html(next(iter(titles.values()), ref))
                results.append({
                    "id":    make_id("eu_av", url),
                    "title": title,
                    "url":   url,
                })
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
                        if item["id"] not in seen_ids:
                            seen_ids.add(item["id"])
                            if not init_mode:
                                if not is_relevant(item["title"], person):
                                    skipped += 1
                                    continue
                                new_count += 1
                                _push_alert(person, source, item)
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
                alamy_results = await scrape_alamy(page, query)
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
                    eu_results = scrape_eu_audiovisual(eu_terms)
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
