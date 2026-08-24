# src/retrieval.py
# ─────────────────────────────────────────────────────────────────
#  Multi-source reverse image search.
#
#  Tier 0 — Free, no limits:
#    4chan    — public JSON API, no key needed
#    Yandex   — Playwright scrape, no key needed
#
#  Tier 1 — Free trials / permanent free tiers:
#    SerpAPI  — Google Lens, 100 free trial searches  (serpapi.com)
#    Zenserp  — Google Image Search, 50 free/month    (zenserp.com)
#
#  Tier 2 — Paid / gated (skipped if no key in .env):
#    Twitter  — $100/month minimum
#
#  ── Hit limits ───────────────────────────────────────────────────
#  Adjust these to control how many results each source returns.
#  Lower = faster run, fewer API credits used.
#  Higher = more coverage, slower, uses more credits.

MAX_4CHAN_BOARDS       = 0    # how many boards to scan (out of 10)
MAX_4CHAN_THREADS      = 0    # threads to check per board
MAX_4CHAN_HITS         = 0   # hard cap on total 4chan hits
MAX_YANDEX_HITS        = 0   # max results from Yandex
MAX_GOOGLE_LENS_HITS   = 20   # max results from Google Lens
MAX_ZENSERP_HITS       = 0   # max results from Zenserp
MAX_TOTAL_HITS         = 20   # hard cap across ALL sources combined

# ─────────────────────────────────────────────────────────────────

import os
import time
import asyncio
import requests
import base64
from urllib.parse import urlparse
from utils import log


def _hit(url: str, source: str, match_type: str = "exact",
         title: str = None, matched_image_url: str = None,
         extra: dict = None) -> dict:
    return {
        "url":               url,
        "domain":            urlparse(url).netloc,
        "source_platform":   source,
        "match_type":        match_type,
        "page_title":        title,
        "matched_image_url": matched_image_url,
        **(extra or {}),
    }


FOURCHAN_BOARDS = [
    "pol", "news", "wsg", "gif", "int",
    "his", "b", "r9k", "v", "vg",
]

def search_4chan(image_sha256: str) -> list[dict]:
    hits  = []
    base  = "https://a.4cdn.org"
    boards_to_scan = FOURCHAN_BOARDS[:MAX_4CHAN_BOARDS]

    for board in boards_to_scan:
        if len(hits) >= MAX_4CHAN_HITS:
            break
        try:
            r = requests.get(
                f"{base}/{board}/threads.json",
                timeout=10,
                headers={"User-Agent": "apmm-research-bot/1.0"},
            )
            if r.status_code != 200:
                continue

            thread_ids = [
                t["no"]
                for page in r.json()
                for t in page.get("threads", [])
            ][:MAX_4CHAN_THREADS]

            for tid in thread_ids:
                if len(hits) >= MAX_4CHAN_HITS:
                    break
                tr = requests.get(
                    f"{base}/{board}/thread/{tid}.json",
                    timeout=10,
                    headers={"User-Agent": "apmm-research-bot/1.0"},
                )
                if tr.status_code != 200:
                    continue

                for post in tr.json().get("posts", []):
                    if len(hits) >= MAX_4CHAN_HITS:
                        break
                    if "tim" not in post or "ext" not in post:
                        continue
                    img_url  = f"https://i.4cdn.org/{board}/{post['tim']}{post['ext']}"
                    page_url = f"https://boards.4chan.org/{board}/thread/{tid}#p{post['no']}"
                    hits.append(_hit(
                        url=page_url,
                        source="4chan",
                        match_type="exact",
                        title=(post.get("sub") or post.get("com", ""))[:80],
                        matched_image_url=img_url,
                        extra={
                            "board":     board,
                            "thread_id": str(tid),
                            "post_id":   str(post["no"]),
                            "timestamp": str(post.get("time", "")),
                            "post_text": post.get("com", "")[:300],
                        },
                    ))

            time.sleep(0.5)

        except Exception as e:
            log.warning(f"  [4chan] /{board}/ error: {e}")

    log.info(f"  [4chan] {len(hits)} hits (cap: {MAX_4CHAN_HITS})")
    return hits


async def _yandex_async(image_path: str) -> list[dict]:
    from playwright.async_api import async_playwright

    hits = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ))
        try:
            await page.goto("https://yandex.com/images/", timeout=20000)
            await page.wait_for_timeout(2000)

            camera_selectors = [
                '[class*="camera"]',
                '[data-type="cbir"]',
                '.cbir-button',
                'button[aria-label*="image"]',
                '[class*="SearchForm-cbir"]',
                '[class*="ImageSearch"]',
                'button[class*="cbir"]',
            ]

            clicked = False
            for selector in camera_selectors:
                try:
                    btn = page.locator(selector).first
                    if await btn.count() > 0:
                        await btn.click(timeout=3000)
                        clicked = True
                        break
                except Exception:
                    continue

            if not clicked:
                log.warning("  [Yandex] Could not find camera button — Yandex may have updated their UI.")
                await browser.close()
                return []

            await page.wait_for_timeout(1500)

            file_input = page.locator('input[type="file"]')
            if await file_input.count() == 0:
                log.warning("  [Yandex] No file input found.")
                await browser.close()
                return []

            await file_input.set_input_files(image_path)
            await page.wait_for_timeout(5000)

            result_selectors = [
                ".serp-item",
                '[class*="serp-item"]',
                '[class*="SerpItem"]',
                '[class*="ImagesApp-SerpItem"]',
            ]

            items = []
            for sel in result_selectors:
                items = await page.locator(sel).all()
                if items:
                    break

            for item in items[:MAX_YANDEX_HITS]:
                try:
                    link  = await item.locator("a").first.get_attribute("href")
                    title = ""
                    for title_sel in ['[class*="title"]', '[class*="Title"]', "h3", "h2"]:
                        try:
                            title = await item.locator(title_sel).first.inner_text()
                            if title:
                                break
                        except Exception:
                            continue
                    if link and link.startswith("http"):
                        hits.append(_hit(
                            url=link,
                            source="yandex",
                            match_type="near-duplicate",
                            title=title[:200],
                        ))
                except Exception:
                    continue

        except Exception as e:
            log.warning(f"  [Yandex] Scrape error: {e}")
        finally:
            await browser.close()

    return hits


def search_yandex(image_path: str) -> list[dict]:
    try:
        hits = asyncio.run(_yandex_async(image_path))
        log.info(f"  [Yandex] {len(hits)} hits (cap: {MAX_YANDEX_HITS})")
        return hits
    except Exception as e:
        log.warning(f"  [Yandex] Failed: {e}")
        return []


def search_google_lens(image_path: str) -> list[dict]:
    api_key = os.getenv("SERPAPI_KEY", "")
    if not api_key:
        log.info("  [Google Lens] No key — skipping.")
        return []

    hits = []
    try:
        # Step 1: Upload image to imgbb to get a public URL (free, no account needed)
        with open(image_path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("utf-8")

        upload = requests.post(
            "https://api.imgbb.com/1/upload",
            data={
                "key": os.getenv("IMGBB_API_KEY", ""),  # free public key
                "image": image_b64,
            },
            timeout=30,
        )

        # Fallback: if imgbb fails, skip
        if upload.status_code != 200:
            log.warning("  [Google Lens] Image hosting failed — skipping.")
            return []

        image_url = upload.json().get("data", {}).get("url", "")
        if not image_url:
            return []

        # Step 2: Pass the public URL to SerpAPI
        r = requests.get(
            "https://serpapi.com/search",
            params={
                "engine":    "google_reverse_image",
                "image_url": image_url,
                "api_key":   api_key,
                "hl":        "en",
                "gl":        "us",
            },
            timeout=30,
        )
        r.raise_for_status()

        for match in r.json().get("image_results", [])[:MAX_GOOGLE_LENS_HITS]:
            url = match.get("link", "")
            if url:
                hits.append(_hit(
                    url=url,
                    source="google_lens",
                    match_type="exact",
                    title=match.get("title", ""),
                    matched_image_url=match.get("thumbnail", ""),
                ))

    except Exception as e:
        log.warning(f"  [Google Lens] Error: {e}")

    log.info(f"  [Google Lens] {len(hits)} hits (cap: {MAX_GOOGLE_LENS_HITS})")
    return hits


def search_zenserp(image_path: str) -> list[dict]:
    api_key = os.getenv("ZENSERP_API_KEY", "")
    if not api_key:
        log.info("  [Zenserp] No key — skipping.")
        return []

    hits = []
    try:
        filename = os.path.splitext(os.path.basename(image_path))[0]
        query    = filename.replace("_", " ").replace("-", " ")

        r = requests.get(
            "https://app.zenserp.com/api/v2/search",
            headers={"apikey": api_key},
            params={
                "q":   query,
                "tbm": "isch",
                "num": MAX_ZENSERP_HITS,
            },
            timeout=15,
        )
        r.raise_for_status()

        for item in r.json().get("image_results", [])[:MAX_ZENSERP_HITS]:
            url = item.get("sourceUrl") or item.get("url", "")
            if url:
                hits.append(_hit(
                    url=url,
                    source="zenserp",
                    match_type="near-duplicate",
                    title=item.get("title", ""),
                    matched_image_url=item.get("imageUrl", ""),
                ))

    except Exception as e:
        log.warning(f"  [Zenserp] Error: {e}")

    log.info(f"  [Zenserp] {len(hits)} hits (cap: {MAX_ZENSERP_HITS})")
    return hits


def search_twitter(query_text: str = "misinformation") -> list[dict]:
    token = os.getenv("TWITTER_BEARER_TOKEN", "")
    if not token:
        log.info("  [Twitter] No token — skipping.")
        return []

    hits = []
    try:
        r = requests.get(
            "https://api.twitter.com/2/tweets/search/recent",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "query":        f"{query_text} has:images -is:retweet",
                "max_results":  20,
                "tweet.fields": "created_at,author_id",
                "expansions":   "attachments.media_keys",
                "media.fields": "url,preview_image_url",
            },
            timeout=15,
        )
        r.raise_for_status()
        data      = r.json()
        media_map = {
            m["media_key"]: m
            for m in data.get("includes", {}).get("media", [])
        }
        for tweet in data.get("data", []):
            tweet_url  = f"https://twitter.com/i/web/status/{tweet['id']}"
            media_keys = tweet.get("attachments", {}).get("media_keys", [])
            img_url    = None
            for mk in media_keys:
                m = media_map.get(mk, {})
                img_url = m.get("url") or m.get("preview_image_url")
                if img_url:
                    break
            hits.append(_hit(
                url=tweet_url,
                source="twitter",
                match_type="near-duplicate",
                matched_image_url=img_url,
                extra={
                    "tweet_id":  tweet["id"],
                    "timestamp": tweet.get("created_at", ""),
                },
            ))
    except Exception as e:
        log.warning(f"  [Twitter] Error: {e}")

    log.info(f"  [Twitter] {len(hits)} hits")
    return hits


def run_all_searches(image_path: str, image_sha256: str,
                     query_text: str = "misinformation") -> list[dict]:
    """
    Run quality sources only.
    4chan removed — it has no reverse image matching capability.
    """
    log.info(f"Starting search (total cap: {MAX_TOTAL_HITS} hits)...")

    seen:   set[str]   = set()
    merged: list[dict] = []

    def _add(hits: list[dict]) -> None:
        for hit in hits:
            url = hit.get("url", "")
            if url and url not in seen and len(merged) < MAX_TOTAL_HITS:
                seen.add(url)
                merged.append(hit)

    _add(search_google_lens(image_path))
    _add(search_zenserp(image_path))
    _add(search_twitter(query_text))
    _add(search_yandex(image_path))

    log.info(f"Total unique hits: {len(merged)}")
    return merged