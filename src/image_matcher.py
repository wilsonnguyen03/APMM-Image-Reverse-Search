# src/image_matcher.py
# ─────────────────────────────────────────────────────────────────
#  Visual image matching for crawled cascade items.
#
#  For each crawled page we:
#    1. Parse page.html and extract all <img> src URLs
#    2. Download each image found on the page
#    3. Compute perceptual hash (pHash) of each downloaded image
#    4. Compare against pHash of the input image
#    5. Flag the item if any image is visually similar
#
#  pHash is robust to:
#    - Resizing
#    - JPEG recompression
#    - Minor colour adjustments
#    - Watermarks (partial)
#
#  Match types returned:
#    exact        — hash distance 0–4   (near-identical)
#    near_duplicate — distance 5–12     (resized/recompressed)
#    possible     — distance 13–20      (cropped or heavily edited)
#    no_match     — distance > 20
#
#  Prerequisites:
#    pip install imagehash pillow beautifulsoup4 requests
# ─────────────────────────────────────────────────────────────────

import io
import os
import time
import requests
from pathlib import Path
from urllib.parse import urljoin, urlparse
from utils import log

# ── Thresholds ────────────────────────────────────────────────────
HASH_EXACT          = 4    # distance 0–4:   exact / trivially recompressed
HASH_NEAR_DUPLICATE = 12   # distance 5–12:  resized, different quality
HASH_POSSIBLE       = 20   # distance 13–20: cropped or heavily edited

# Max images to download and compare per page
MAX_IMAGES_PER_PAGE = 30

# Download timeout per image
IMAGE_DOWNLOAD_TIMEOUT = 8

# Min image size to bother comparing (skip tiny icons/buttons)
MIN_IMAGE_PIXELS = 50 * 50   # 2500px minimum area


def _compute_phash(image_bytes: bytes):
    """Compute perceptual hash from raw image bytes. Returns None on failure."""
    try:
        import imagehash
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        return imagehash.phash(img)
    except Exception:
        return None


def _compute_phash_from_file(image_path: str):
    """Compute perceptual hash from a file path."""
    try:
        with open(image_path, "rb") as f:
            return _compute_phash(f.read())
    except Exception:
        return None


def _hash_distance(h1, h2) -> int:
    """Hamming distance between two pHashes."""
    try:
        return h1 - h2
    except Exception:
        return 999


def _classify_match(distance: int) -> str:
    if distance <= HASH_EXACT:
        return "exact"
    elif distance <= HASH_NEAR_DUPLICATE:
        return "near_duplicate"
    elif distance <= HASH_POSSIBLE:
        return "possible"
    return "no_match"


def _extract_image_urls(html_path: Path, page_url: str) -> list[str]:
    """
    Extract all <img> src URLs from a saved HTML file.
    Also checks og:image and twitter:image meta tags.
    Resolves relative URLs against the page URL.
    """
    try:
        from bs4 import BeautifulSoup
        html = html_path.read_text(encoding="utf-8", errors="replace")
        soup = BeautifulSoup(html, "html.parser")
        urls = []

        # Standard img tags
        for tag in soup.find_all("img"):
            src = tag.get("src") or tag.get("data-src") or tag.get("data-lazy-src")
            if src:
                urls.append(src)

        # OG and Twitter meta images — often the main article image
        for meta in soup.find_all("meta"):
            prop = meta.get("property", "") or meta.get("name", "")
            if prop in ("og:image", "twitter:image", "og:image:url"):
                content = meta.get("content", "")
                if content:
                    urls.insert(0, content)  # prioritise — likely the main image

        # Resolve relative URLs
        resolved = []
        for url in urls:
            try:
                full = urljoin(page_url, url)
                if full.startswith("http"):
                    resolved.append(full)
            except Exception:
                continue

        # Deduplicate preserving order
        seen = set()
        unique = []
        for url in resolved:
            if url not in seen:
                seen.add(url)
                unique.append(url)

        return unique[:MAX_IMAGES_PER_PAGE]

    except Exception as e:
        log.warning(f"  [Matcher] Failed to extract image URLs: {e}")
        return []


def _download_image(url: str) -> bytes | None:
    """Download an image and return raw bytes. Returns None on failure."""
    try:
        r = requests.get(
            url,
            timeout=IMAGE_DOWNLOAD_TIMEOUT,
            headers={"User-Agent": "apmm-research-bot/1.0"},
            stream=True,
        )
        if r.status_code != 200:
            return None
        content_type = r.headers.get("content-type", "")
        if "image" not in content_type and "octet" not in content_type:
            return None
        return r.content
    except Exception:
        return None


def _image_too_small(image_bytes: bytes) -> bool:
    """Return True if image is too small to be worth comparing."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes))
        return (img.width * img.height) < MIN_IMAGE_PIXELS
    except Exception:
        return True


def match_images_on_page(
    input_hash,
    html_path: Path,
    page_url: str,
    snapshot_dir: Path,
) -> dict:
    """
    Check if the input image appears on a crawled page.

    Returns a result dict:
      {
        "image_match":       bool,
        "match_type":        "exact" | "near_duplicate" | "possible" | "no_match",
        "best_distance":     int,
        "best_match_url":    str | None,
        "images_checked":    int,
      }
    """
    result = {
        "image_match":    False,
        "match_type":     "no_match",
        "best_distance":  999,
        "best_match_url": None,
        "images_checked": 0,
    }

    if input_hash is None:
        return result

    if not html_path.exists():
        return result

    img_urls = _extract_image_urls(html_path, page_url)
    if not img_urls:
        return result

    best_distance = 999
    best_url      = None

    for url in img_urls:
        image_bytes = _download_image(url)
        if not image_bytes:
            continue
        if _image_too_small(image_bytes):
            continue

        page_hash = _compute_phash(image_bytes)
        if page_hash is None:
            continue

        distance = _hash_distance(input_hash, page_hash)
        result["images_checked"] += 1

        if distance < best_distance:
            best_distance = distance
            best_url      = url

        # Stop early if exact match found
        if distance <= HASH_EXACT:
            break

        time.sleep(0.1)  # polite delay between image downloads

    match_type = _classify_match(best_distance)
    is_match   = best_distance <= HASH_POSSIBLE

    result["image_match"]    = is_match
    result["match_type"]     = match_type
    result["best_distance"]  = best_distance
    result["best_match_url"] = best_url

    return result


# ── Main entry point ──────────────────────────────────────────────

def run_image_matching(
    items: list[dict],
    input_image_path: str,
    case_dir: Path,
) -> list[dict]:
    """
    Run visual image matching on all crawled items.
    Adds image_match fields to each item dict.
    Root item (no URL) is skipped.
    """
    log.info("Running visual image matching...")

    # Compute input image hash once
    input_hash = _compute_phash_from_file(input_image_path)
    if input_hash is None:
        log.warning("  [Matcher] Could not hash input image — skipping image matching.")
        for item in items:
            item["image_match"] = False
            item["image_match_type"] = "no_match"
        return items

    log.info(f"  Input image pHash: {input_hash}")

    matched = 0
    for item in items:
        item_id      = item.get("id", "")
        snapshot_dir = item.get("snapshot_path")
        page_url     = item.get("url", "")

        # Skip root
        if not page_url or not snapshot_dir:
            item["image_match"]      = False
            item["image_match_type"] = "root"
            continue

        html_path = Path(snapshot_dir) / "page.html"

        log.info(f"  Checking {item_id}: {(item.get('domain') or '?')[:40]}")
        result = match_images_on_page(
            input_hash, html_path, page_url, Path(snapshot_dir)
        )

        item["image_match"]          = result["image_match"]
        item["image_match_type"]     = result["match_type"]
        item["image_match_distance"] = result["best_distance"]
        item["image_match_url"]      = result["best_match_url"]
        item["images_checked"]       = result["images_checked"]

        if result["image_match"]:
            matched += 1
            log.info(
                f"    ✓ MATCH — type={result['match_type']} "
                f"distance={result['best_distance']} "
                f"url={result['best_match_url']}"
            )
        else:
            log.info(
                f"    ✗ no match — best distance={result['best_distance']} "
                f"({result['images_checked']} images checked)"
            )

    log.info(f"Image matching complete: {matched}/{len(items)} pages contain the input image")
    return items
