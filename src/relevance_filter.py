# src/relevance_filter.py
# ─────────────────────────────────────────────────────────────────
#  Relevance filter for crawled cascade items.
#
#  For each item we already have on disk:
#    - cases/<case_id>/snapshots/<item_id>/screenshot.png
#    - cases/<case_id>/snapshots/<item_id>/page.html
#
#  This module:
#    1. Extracts visible text from the HTML (fast)
#    2. OCRs the screenshot using Tesseract (thorough)
#    3. Combines both text sources
#    4. Scores relevance against a set of keywords derived
#       from the query context you provide
#    5. Marks each item with:
#         relevant        — bool
#         relevance_score — float 0.0–1.0
#         relevance_reasons — list of matched signals
#
#  Items that fail are NOT removed — they stay in the cascade
#  record with relevant=False. Gagandeep's spec tolerates extra
#  fields on items and graph_builder ignores them.
#
#  Prerequisites:
#    pip install pytesseract beautifulsoup4 pillow
#    Install Tesseract: github.com/UB-Mannheim/tesseract/wiki
#    Windows default path: C:\Program Files\Tesseract-OCR\tesseract.exe
# ─────────────────────────────────────────────────────────────────

import os
import re
import json
from pathlib import Path
from utils import log

# ── Tesseract path ────────────────────────────────────────────────
# Adjust if your Tesseract is installed elsewhere
TESSERACT_PATHS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    "/usr/bin/tesseract",
    "/usr/local/bin/tesseract",
]

def _configure_tesseract() -> bool:
    """Find and configure Tesseract. Returns True if found."""
    try:
        import pytesseract
        # Check if already on PATH
        import shutil
        if shutil.which("tesseract"):
            return True
        # Try known Windows paths
        for path in TESSERACT_PATHS:
            if Path(path).exists():
                pytesseract.pytesseract.tesseract_cmd = path
                return True
        return False
    except ImportError:
        return False

TESSERACT_AVAILABLE = _configure_tesseract()


# ── Text extraction ───────────────────────────────────────────────

def extract_html_text(html_path: Path) -> str:
    """
    Extract visible text from saved HTML using BeautifulSoup.
    Strips scripts, styles, nav, footer — keeps article body.
    """
    try:
        from bs4 import BeautifulSoup

        html = html_path.read_text(encoding="utf-8", errors="replace")
        soup = BeautifulSoup(html, "html.parser")

        # Remove noise elements
        for tag in soup(["script", "style", "nav", "footer",
                         "header", "aside", "noscript", "iframe"]):
            tag.decompose()

        # Prefer article body if present
        article = soup.find("article") or soup.find("main") or soup.body
        if article:
            text = article.get_text(separator=" ", strip=True)
        else:
            text = soup.get_text(separator=" ", strip=True)

        # Collapse whitespace
        text = re.sub(r"\s+", " ", text).strip()
        return text[:50000]  # cap to avoid huge pages

    except Exception as e:
        log.warning(f"  [Filter] HTML extraction failed: {e}")
        return ""


def extract_ocr_text(screenshot_path: Path) -> str:
    """
    OCR the full-page screenshot using Tesseract.
    Returns extracted text or empty string if Tesseract unavailable.
    """
    if not TESSERACT_AVAILABLE:
        log.warning("  [Filter] Tesseract not found — OCR skipped.")
        log.warning("  [Filter] Install from: github.com/UB-Mannheim/tesseract/wiki")
        return ""

    try:
        import pytesseract
        from PIL import Image

        img = Image.open(screenshot_path)

        # Resize if very tall — Tesseract can be slow on huge images
        max_height = 6000
        if img.height > max_height:
            ratio = max_height / img.height
            img = img.resize(
                (int(img.width * ratio), max_height),
                Image.LANCZOS
            )

        # Run OCR — psm 1 = automatic page segmentation
        text = pytesseract.image_to_string(img, config="--psm 1")
        text = re.sub(r"\s+", " ", text).strip()
        log.info(f"  [Filter] OCR extracted {len(text)} chars")
        return text

    except Exception as e:
        log.warning(f"  [Filter] OCR failed: {e}")
        return ""


def get_combined_text(snapshot_dir: Path) -> str:
    """
    Get all text from a snapshot — HTML text + OCR text combined.
    """
    html_path        = snapshot_dir / "page.html"
    screenshot_path  = snapshot_dir / "screenshot.png"

    html_text = ""
    ocr_text  = ""

    if html_path.exists():
        html_text = extract_html_text(html_path)

    if screenshot_path.exists():
        ocr_text = extract_ocr_text(screenshot_path)

    # Combine — HTML text first (more reliable), OCR fills gaps
    combined = f"{html_text} {ocr_text}".strip()
    return combined


# ── Relevance scoring ─────────────────────────────────────────────

def score_relevance(
    text: str,
    keywords: list[str],
    item: dict,
) -> tuple[float, list[str]]:
    """
    Score how relevant a page's text is to the given keywords.

    Scoring signals:
      - Keyword hits in page text (weighted by keyword importance)
      - URL/domain match to keywords
      - Page title match
      - Metadata match (publisher, platform)

    Returns (score 0.0–1.0, list of matched reasons).
    """
    if not text and not keywords:
        return 0.0, []

    text_lower  = text.lower()
    reasons     = []
    score       = 0.0

    # ── Signal 1: keyword hits in body text ───────────────────────
    keyword_hits = 0
    for kw in keywords:
        kw_lower = kw.lower()
        count = text_lower.count(kw_lower)
        if count > 0:
            keyword_hits += 1
            reasons.append(f"keyword '{kw}' found {count}x in page text")
            # Diminishing returns for repeated hits
            score += min(0.3, 0.1 * count)

    # Bonus if multiple keywords hit
    if keyword_hits >= 3:
        score += 0.15
        reasons.append(f"{keyword_hits} keywords matched")
    elif keyword_hits >= 2:
        score += 0.08

    # ── Signal 2: keyword in URL ──────────────────────────────────
    url = (item.get("url") or "").lower()
    for kw in keywords:
        if kw.lower() in url:
            score += 0.1
            reasons.append(f"keyword '{kw}' in URL")
            break

    # ── Signal 3: keyword in page title ──────────────────────────
    title = (item.get("page_title") or "").lower()
    for kw in keywords:
        if kw.lower() in title:
            score += 0.15
            reasons.append(f"keyword '{kw}' in page title")
            break

    # ── Signal 4: keyword in publisher/domain ─────────────────────
    domain = (item.get("domain") or "").lower()
    for kw in keywords:
        if kw.lower() in domain:
            score += 0.1
            reasons.append(f"keyword '{kw}' in domain")
            break

    # ── Signal 5: matched image URL context ───────────────────────
    matched_img = (item.get("matched_image_url") or "").lower()
    for kw in keywords:
        if kw.lower() in matched_img:
            score += 0.05
            reasons.append(f"keyword '{kw}' in matched image URL")
            break

    # Cap at 1.0
    score = min(1.0, score)
    return score, reasons


# ── Main filter function ──────────────────────────────────────────

RELEVANCE_THRESHOLD = 0.15  # items below this score are marked not relevant

def filter_items(
    items: list[dict],
    keywords: list[str],
    case_dir: Path,
    threshold: float = RELEVANCE_THRESHOLD,
) -> list[dict]:
    """
    Run relevance filtering on all crawled items.

    For each item:
      - Reads its snapshot from disk
      - Extracts HTML text + OCR
      - Scores against keywords
      - Adds relevance fields to the item dict

    Returns the same items list with relevance fields added.
    Root item (no snapshot) is always marked relevant=True.
    """
    if not keywords:
        log.warning("  [Filter] No keywords provided — skipping relevance filter.")
        for item in items:
            item["relevant"]          = True
            item["relevance_score"]   = 1.0
            item["relevance_reasons"] = ["no keywords provided"]
        return items

    log.info(f"Running relevance filter on {len(items)} items...")
    log.info(f"  Keywords: {keywords}")

    relevant_count = 0

    for item in items:
        item_id      = item.get("id", "")
        snapshot_dir = item.get("snapshot_path")

        # Root item — always relevant
        if not snapshot_dir or not item.get("url"):
            item["relevant"]          = True
            item["relevance_score"]   = 1.0
            item["relevance_reasons"] = ["root item"]
            relevant_count += 1
            continue

        snapshot_path = Path(snapshot_dir)

        # Check for crawl error
        if snapshot_path.exists() and (snapshot_path / "error.txt").exists():
            # Still score on metadata even if crawl failed
            combined_text = ""
            log.info(f"  {item_id}: crawl failed — scoring on metadata only")
        elif snapshot_path.exists():
            combined_text = get_combined_text(snapshot_path)
        else:
            # No snapshot on disk — score on metadata only
            combined_text = ""

        # Score relevance
        score, reasons = score_relevance(combined_text, keywords, item)
        is_relevant    = score >= threshold

        item["relevant"]          = is_relevant
        item["relevance_score"]   = round(score, 3)
        item["relevance_reasons"] = reasons

        status = "RELEVANT" if is_relevant else "FILTERED"
        log.info(
            f"  {item_id}: {status} "
            f"(score={score:.2f}, {len(reasons)} signals)"
        )
        if is_relevant:
            relevant_count += 1

    total    = len(items)
    filtered = total - relevant_count
    log.info(
        f"Filter complete: {relevant_count}/{total} relevant, "
        f"{filtered} filtered out"
    )

    return items


def apply_to_cascade(
    record: dict,
    keywords: list[str],
    case_dir: Path,
    threshold: float = RELEVANCE_THRESHOLD,
) -> dict:
    """
    Apply relevance filtering to a full cascade record.
    Updates items in-place with relevance fields.
    Also removes edges whose child item was filtered out.
    """
    record["items"] = filter_items(
        record["items"], keywords, case_dir, threshold
    )

    # Remove edges pointing to irrelevant items
    # (keep edges to root and relevant items only)
    relevant_ids = {
        item["id"] for item in record["items"]
        if item.get("relevant", True)
    }
    original_edges = len(record["edges"])
    record["edges"] = [
        e for e in record["edges"]
        if e[1] in relevant_ids
    ]
    removed = original_edges - len(record["edges"])
    if removed:
        log.info(f"Removed {removed} edges to filtered items")

    # Summary counts
    record["filter_summary"] = {
        "keywords":       keywords,
        "threshold":      threshold,
        "total_items":    len(record["items"]),
        "relevant_items": len(relevant_ids),
        "filtered_items": len(record["items"]) - len(relevant_ids),
    }

    return record


def apply_to_cascade_with_images(
    record: dict,
    keywords: list[str],
    case_dir: Path,
    input_image_path: str = None,
    threshold: float = RELEVANCE_THRESHOLD,
) -> dict:
    """
    Full relevance pipeline with optional visual image matching.
    If input_image_path provided, runs pHash comparison on all crawled pages
    and boosts scores for pages that visually contain the input image.
    """
    # Step 1: Visual image matching (adds image_match fields + _image_boost)
    if input_image_path:
        try:
            from image_matcher import run_image_matching
            record["items"] = run_image_matching(
                record["items"], input_image_path, case_dir
            )
            # Pre-apply image boost to items before text scoring
            for item in record["items"]:
                if item.get("image_match"):
                    match_type = item.get("image_match_type", "")
                    item["_image_boost"] = {
                        "exact":          0.60,
                        "near_duplicate": 0.45,
                        "possible":       0.25,
                    }.get(match_type, 0.0)
                else:
                    item["_image_boost"] = 0.0
        except Exception as e:
            log.warning(f"  [Filter] Image matching failed: {e}")

    # Step 2: Text relevance filter (uses keywords + absorbs _image_boost)
    record["items"] = filter_items_with_image_boost(
        record["items"], keywords, case_dir, threshold
    )

    # Step 3: Remove edges to irrelevant items
    relevant_ids = {
        item["id"] for item in record["items"]
        if item.get("relevant", True)
    }
    original_edges = len(record["edges"])
    record["edges"] = [
        e for e in record["edges"] if e[1] in relevant_ids
    ]
    removed = original_edges - len(record["edges"])
    if removed:
        log.info(f"Removed {removed} edges to filtered items")

    record["filter_summary"] = {
        "keywords":       keywords,
        "threshold":      threshold,
        "total_items":    len(record["items"]),
        "relevant_items": len(relevant_ids),
        "filtered_items": len(record["items"]) - len(relevant_ids),
        "image_matched":  sum(1 for i in record["items"] if i.get("image_match")),
    }

    return record


def filter_items_with_image_boost(
    items: list[dict],
    keywords: list[str],
    case_dir: Path,
    threshold: float = RELEVANCE_THRESHOLD,
) -> list[dict]:
    """
    Like filter_items but incorporates _image_boost from image matching.
    A confirmed image match can push an item over the threshold even if
    text keywords didn't fire.
    """
    for item in items:
        item_id      = item.get("id", "")
        snapshot_dir = item.get("snapshot_path")
        image_boost  = item.pop("_image_boost", 0.0)

        # Root item — always relevant
        if not item.get("url") or not snapshot_dir:
            item["relevant"]          = True
            item["relevance_score"]   = 1.0
            item["relevance_reasons"] = ["root item"]
            continue

        snapshot_path = Path(snapshot_dir)

        # Get text
        if snapshot_path.exists():
            combined_text = get_combined_text(snapshot_path)
        else:
            combined_text = ""

        # Score text
        score, reasons = score_relevance(combined_text, keywords, item)

        # Apply image boost
        if image_boost > 0:
            score += image_boost
            match_type = item.get("image_match_type", "match")
            reasons.append(f"image {match_type} found on page (+{image_boost:.2f} boost)")

        score = min(1.0, score)

        # If image confirmed match, always mark relevant regardless of text
        if item.get("image_match") and item.get("image_match_type") in ("exact", "near_duplicate"):
            is_relevant = True
            if "image confirmed match" not in str(reasons):
                reasons.append("image confirmed — overrides text threshold")
        else:
            is_relevant = score >= threshold

        item["relevant"]          = is_relevant
        item["relevance_score"]   = round(score, 3)
        item["relevance_reasons"] = reasons

        status = "RELEVANT" if is_relevant else "FILTERED"
        log.info(
            f"  {item_id}: {status} "
            f"(score={score:.2f}, image={'✓' if item.get('image_match') else '✗'})"
        )

    relevant_count = sum(1 for i in items if i.get("relevant"))
    log.info(f"Filter complete: {relevant_count}/{len(items)} relevant")
    return items
