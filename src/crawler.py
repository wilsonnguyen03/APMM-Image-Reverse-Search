# src/crawler.py
# Visits each hit URL, saves HTML + full-page screenshot as evidence.
# No scoring, no judgement — raw extraction only.

import asyncio
from pathlib import Path
from urllib.parse import urlparse
from utils import log, make_snapshot_dir

TIMESTAMP_JS = """
() => {
    const selectors = [
        'meta[property="article:published_time"]',
        'meta[property="og:article:published_time"]',
        'meta[name="pubdate"]',
        'meta[name="publishdate"]',
        'meta[name="date"]',
        'time[datetime]',
    ];
    for (const sel of selectors) {
        const el = document.querySelector(sel);
        if (el) return el.getAttribute('content') || el.getAttribute('datetime') || null;
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
    ];
    for (const sel of selectors) {
        const el = document.querySelector(sel);
        if (el) return el.getAttribute('content') || null;
    }
    return null;
}
"""


async def crawl_url(url: str, case_dir: Path, item_id: str,
                    timeout_ms: int = 15000) -> dict:
    from playwright.async_api import async_playwright

    snapshot_dir = make_snapshot_dir(case_dir, item_id)
    result = {
        "url":           url,
        "domain":        urlparse(url).netloc,
        "publisher":     None,
        "page_title":    None,
        "timestamp":     None,
        "snapshot_path": str(snapshot_dir),
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()
        try:
            await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            await page.screenshot(
                path=str(snapshot_dir / "screenshot.png"), full_page=True
            )
            html = await page.content()
            (snapshot_dir / "page.html").write_text(
                html, encoding="utf-8", errors="replace"
            )
            result["page_title"] = await page.title() or None
            result["timestamp"]  = await page.evaluate(TIMESTAMP_JS)
            result["publisher"]  = await page.evaluate(PUBLISHER_JS)
            log.info(f"  Crawled: {result['domain']}")
        except Exception as e:
            log.warning(f"  Failed: {url[:60]} — {e}")
            (snapshot_dir / "error.txt").write_text(str(e), encoding="utf-8")
        finally:
            await browser.close()

    return result


async def crawl_all(hits: list[dict], case_dir: Path) -> list[dict]:
    enriched = []
    for i, hit in enumerate(hits):
        item_id = f"item_{i:03d}"
        url = hit.get("url", "")
        if not url:
            continue
        log.info(f"Crawling {i+1}/{len(hits)}: {url[:80]}")
        meta = await crawl_url(url, case_dir, item_id)
        enriched.append({**hit, **meta, "id": item_id})
    return enriched


def crawl_all_sync(hits: list[dict], case_dir: Path) -> list[dict]:
    return asyncio.run(crawl_all(hits, case_dir))
