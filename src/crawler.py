# src/crawler.py
# Visits each hit URL, saves HTML + full-page screenshot as evidence.
# No scoring, no judgement — raw extraction only.
#
# Date extraction strategy (in order):
#   1. Standard meta tags (article:published_time, pubdate etc.)
#   2. Extended JS selectors — waits for JS to render, catches social media
#   3. Regex on page text — catches dates written in article body

import re
import asyncio
from pathlib import Path
from urllib.parse import urlparse
from utils import log, make_snapshot_dir

# ── Extended timestamp selectors ──────────────────────────────────
# Covers meta tags, schema.org, social media, CMS platforms
TIMESTAMP_JS = """
() => {
    const selectors = [
        // Standard meta
        'meta[property="article:published_time"]',
        'meta[property="og:article:published_time"]',
        'meta[name="pubdate"]',
        'meta[name="publishdate"]',
        'meta[name="date"]',
        'meta[name="DC.date"]',
        'meta[name="DC.Date"]',
        'meta[name="creation_date"]',
        'meta[itemprop="datePublished"]',
        'meta[itemprop="dateCreated"]',
        // Schema.org
        '[itemprop="datePublished"]',
        '[itemprop="dateCreated"]',
        // Time elements
        'time[datetime]',
        'time[pubdate]',
        // Twitter/X specific
        'a[href*="/status/"] time',
        '[data-testid="Tweet-User-Avatar"] ~ div time',
        // Instagram specific
        'time._aaqe',
        'time[class*="time"]',
        // Facebook specific
        '[data-utime]',
        'abbr[data-utime]',
        // News CMS patterns
        '.date-display-single',
        '.entry-date',
        '.post-date',
        '.published',
        '.article-date',
        '.byline time',
        '.timestamp',
        '[class*="PublishDate"]',
        '[class*="publish-date"]',
        '[class*="article__date"]',
        '[class*="ArticleDate"]',
    ];

    for (const sel of selectors) {
        try {
            const el = document.querySelector(sel);
            if (!el) continue;
            const val = el.getAttribute('content')
                     || el.getAttribute('datetime')
                     || el.getAttribute('data-utime')
                     || el.getAttribute('pubdate')
                     || el.innerText
                     || el.textContent;
            if (val && val.trim().length > 4) return val.trim();
        } catch(e) {}
    }
    return null;
}
"""

PUBLISHER_JS = """
() => {
    const selectors = [
        'meta[property="og:site_name"]',
        'meta[name="publisher"]',
        'meta[name="author"]',
        'meta[property="article:author"]',
        'meta[name="application-name"]',
        '[itemprop="publisher"] [itemprop="name"]',
        '.publisher',
        '[class*="publisher"]',
    ];
    for (const sel of selectors) {
        try {
            const el = document.querySelector(sel);
            if (el) return el.getAttribute('content') || el.innerText || null;
        } catch(e) {}
    }
    return null;
}
"""

# ── Regex date extraction ─────────────────────────────────────────
# Matches common date formats found in article body text
DATE_PATTERNS = [
    # ISO: 2026-04-13, 2026/04/13
    r'\b(20\d{2}[-/]\d{1,2}[-/]\d{1,2})\b',
    # Long form: April 13, 2026 / 13 April 2026
    r'\b((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
    r'Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
    r'\s+\d{1,2},?\s+20\d{2})\b',
    r'\b(\d{1,2}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
    r'Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
    r'\s+20\d{2})\b',
    # Numeric: 13/04/2026, 04/13/2026
    r'\b(\d{1,2}/\d{1,2}/20\d{2})\b',
    # Unix timestamp as data attribute (Facebook style)
    r'"date_time_iso8601"\s*:\s*"(20\d{2}-\d{2}-\d{2}T[^"]+)"',
]

def extract_date_from_text(text: str) -> str | None:
    """
    Scan page text for date patterns.
    Returns the first match found, or None.
    """
    if not text:
        return None
    for pattern in DATE_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


# ── Crawler ───────────────────────────────────────────────────────

CONCURRENCY = 8   # pages crawled in parallel

async def crawl_url(browser, url: str, case_dir: Path, item_id: str,
                    timeout_ms: int = 20000) -> dict:
    snapshot_dir = make_snapshot_dir(case_dir, item_id)
    result = {
        "url":           url,
        "domain":        urlparse(url).netloc,
        "publisher":     None,
        "page_title":    None,
        "timestamp":     None,
        "snapshot_path": str(snapshot_dir),
    }

    # One isolated context per page; the browser itself is shared.
    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 900},
    )
    try:
        page = await context.new_page()

        # domcontentloaded returns quickly; networkidle is only a bounded
        # best-effort wait so JS-rendered dates can appear without stalling
        # on pages that never go idle.
        await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:
            pass
        await page.wait_for_timeout(1000)

        await page.screenshot(
            path=str(snapshot_dir / "screenshot.png"), full_page=True
        )
        html = await page.content()
        (snapshot_dir / "page.html").write_text(
            html, encoding="utf-8", errors="replace"
        )

        result["page_title"] = await page.title() or None
        result["publisher"]  = await page.evaluate(PUBLISHER_JS)

        timestamp = await page.evaluate(TIMESTAMP_JS)

        # Regex fallback on visible page text if no selector matched
        if not timestamp:
            page_text = await page.evaluate("""
                () => {
                    const body = document.body;
                    if (!body) return '';
                    const clone = body.cloneNode(true);
                    clone.querySelectorAll('script,style,noscript').forEach(el => el.remove());
                    return clone.innerText || clone.textContent || '';
                }
            """)
            timestamp = extract_date_from_text(page_text)
            if timestamp:
                log.info(f"  Date found via regex: {timestamp}")

        result["timestamp"] = timestamp
        log.info(f"  Crawled: {result['domain']}"
                 + (f" | date: {timestamp[:10]}" if timestamp else " | date: not found"))

    except Exception as e:
        log.warning(f"  Failed: {url[:60]} — {e}")
        (snapshot_dir / "error.txt").write_text(str(e), encoding="utf-8")
    finally:
        await context.close()

    return result


async def crawl_all(hits: list[dict], case_dir: Path) -> list[dict]:
    from playwright.async_api import async_playwright

    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(browser, i, hit):
        item_id = f"item_{i:03d}"
        async with sem:
            log.info(f"Crawling {i+1}/{len(hits)}: {hit['url'][:80]}")
            meta = await crawl_url(browser, hit["url"], case_dir, item_id)
        # A failed crawl must not overwrite the search hit's own title/date/etc. with None
        meta = {k: v for k, v in meta.items() if v is not None}
        return {**hit, **meta, "id": item_id}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            # gather preserves input order, so item ids stay stable
            return list(await asyncio.gather(*(
                one(browser, i, hit)
                for i, hit in enumerate(hits) if hit.get("url")
            )))
        finally:
            await browser.close()


def crawl_all_sync(hits: list[dict], case_dir: Path) -> list[dict]:
    return asyncio.run(crawl_all(hits, case_dir))
