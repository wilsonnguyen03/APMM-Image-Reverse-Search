# src/relevance_filter.py
# Decides which crawled pages are relevant to the case.
#
# Evidence is combined with a noisy-OR: every independent signal can raise
# the score, none can lower it, and one strong signal is enough on its own.
#   - Image match (pHash)      strongest evidence: the page contains the image
#   - Keyword in title / body / URL / publisher / matched-image URL
# Items below THRESHOLD stay in the record with relevant=False.
#
# Needs: beautifulsoup4, pytesseract + the Tesseract binary (OCR, optional).

import re
import math
import shutil
from pathlib import Path
from utils import log

THRESHOLD = 0.25   # score needed to count as relevant
# Without an image match, a page must match at least this many distinct keywords.
# Keeps generic hits ("trump" alone) from passing; image-confirmed pages are exempt.
MIN_KEYWORDS = 2
TEXT_ONLY_CAP = 0.2   # score ceiling for pages below MIN_KEYWORDS (stays under THRESHOLD)

# Chance that a keyword found in each place means the page is on-topic
P_TITLE = 0.50
P_URL   = 0.30
P_META  = 0.25     # publisher name / matched image URL
SHORT_KEYWORD_WEIGHT = 0.5   # words of <=3 letters (e.g. "AI") are ambiguous

KNOWN_DOMAINS = {
    "reuters.com", "apnews.com", "bbc.com", "bbc.co.uk", "nbcnews.com",
    "cnn.com", "theguardian.com", "washingtonpost.com", "nytimes.com",
    "foxnews.com", "abc.net.au", "france24.com", "dw.com", "aljazeera.com",
    "facebook.com", "instagram.com", "twitter.com", "x.com",
    "youtube.com", "tiktok.com", "reddit.com",
}
DOMAIN_BONUS = 0.05   # only applied when there is already some evidence

TESSERACT_PATHS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
]


def _configure_tesseract() -> bool:
    try:
        import pytesseract
    except ImportError:
        return False
    if shutil.which("tesseract"):
        return True
    for path in TESSERACT_PATHS:
        if Path(path).exists():
            pytesseract.pytesseract.tesseract_cmd = path
            return True
    return False

TESSERACT_AVAILABLE = _configure_tesseract()
_warned_no_ocr = False


# ── Text extraction ───────────────────────────────────────────────

def extract_html_text(html_path: Path) -> str:
    """Visible text of the saved page, preferring <article>/<main>."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_path.read_text(encoding="utf-8", errors="replace"),
                             "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header",
                         "aside", "noscript", "iframe"]):
            tag.decompose()
        root = soup.find("article") or soup.find("main") or soup.body or soup
        return re.sub(r"\s+", " ", root.get_text(separator=" ", strip=True))[:50000]
    except Exception as e:
        log.warning(f"  [Filter] HTML extraction failed: {e}")
        return ""


def extract_ocr_text(screenshot_path: Path) -> str:
    """OCR of the full-page screenshot ('' if Tesseract is unavailable)."""
    global _warned_no_ocr
    if not TESSERACT_AVAILABLE:
        if not _warned_no_ocr:
            log.warning("  [Filter] Tesseract not found — OCR skipped "
                        "(github.com/UB-Mannheim/tesseract/wiki)")
            _warned_no_ocr = True
        return ""
    try:
        import pytesseract
        from PIL import Image
        img = Image.open(screenshot_path)
        if img.height > 6000:   # keep Tesseract fast on very tall pages
            img = img.resize((int(img.width * 6000 / img.height), 6000), Image.LANCZOS)
        return re.sub(r"\s+", " ", pytesseract.image_to_string(img, config="--psm 1")).strip()
    except Exception as e:
        log.warning(f"  [Filter] OCR failed: {e}")
        return ""


def get_combined_text(snapshot_dir: Path) -> str:
    """HTML text first (more reliable), OCR text appended to fill gaps."""
    html, shot = snapshot_dir / "page.html", snapshot_dir / "screenshot.png"
    parts = [extract_html_text(html) if html.exists() else "",
             extract_ocr_text(shot) if shot.exists() else ""]
    return " ".join(p for p in parts if p).strip()


# ── Scoring ───────────────────────────────────────────────────────

def _count(kw: str, text: str) -> int:
    kw = kw.lower()
    if len(kw) <= 3:   # whole-word match so "AI" doesn't hit "said"
        return len(re.findall(r"\b" + re.escape(kw) + r"\b", text))
    return text.count(kw)


def _noisy_or(ps) -> float:
    out = 1.0
    for p in ps:
        out *= 1.0 - p
    return 1.0 - out


def _body_p(count: int) -> float:
    """0 hits -> 0; 1 hit -> ~0.32; 10+ hits -> 0.6."""
    return 0.0 if count == 0 else min(0.6, 0.2 + 0.4 * math.log1p(count) / math.log1p(10))


def score_text(text: str, keywords: list[str], item: dict,
               required: tuple = ()) -> tuple[float, list[str]]:
    """Keyword evidence across title, body, URL and metadata."""
    title = (item.get("page_title") or "").lower()
    url   = (item.get("url") or "").lower()
    meta  = " ".join([(item.get("publisher") or ""),
                      (item.get("matched_image_url") or "")]).lower()
    # Social posts have little page text; their caption lives in post_text
    body  = f"{text or ''} {item.get('post_text') or ''}".lower()

    per_kw, reasons, matched = [], [], set()
    for kw in keywords:
        weight = SHORT_KEYWORD_WEIGHT if len(kw) <= 3 else 1.0
        found = []
        if _count(kw, title): found.append(("title", P_TITLE))
        if _count(kw, url):   found.append(("URL", P_URL))
        if _count(kw, meta):  found.append(("metadata", P_META))
        n = _count(kw, body)
        if n:                 found.append((f"body x{n}", _body_p(n)))
        if found:
            per_kw.append(weight * _noisy_or(p for _, p in found))
            matched.add(kw.lower())
            reasons.append(f"'{kw}' in " + ", ".join(w for w, _ in found))

    score = _noisy_or(per_kw)
    need = min(MIN_KEYWORDS, len(keywords))
    missing = [k for k in required if k.lower() not in matched]
    weak = bool(missing) or len(per_kw) < need
    if missing:
        reasons.append(f"missing required keyword: {', '.join(missing)}")
    elif weak and per_kw:
        reasons.append(f"only {len(per_kw)} of {need} keywords matched")
    domain = (item.get("domain") or "").replace("www.", "")
    if score > 0 and any(domain.endswith(d) for d in KNOWN_DOMAINS):
        score = min(1.0, score + DOMAIN_BONUS)
        reasons.append(f"known domain: {domain}")
    if weak:   # applied last so no bonus can lift a weak match over the threshold
        score = min(score, TEXT_ONLY_CAP)
    return score, reasons


def score_image(item: dict) -> tuple[float, str | None]:
    """Strength of the visual-match evidence, falling smoothly with pHash distance."""
    if not item.get("image_match"):
        return 0.0, None
    d = item.get("image_match_distance")
    d = 20 if d is None else d
    if d <= 4:    p = 0.95 - 0.02 * d
    elif d <= 12: p = 0.85 - 0.02 * (d - 4)
    else:         p = 0.45
    return p, f"input image found on page (pHash distance {d})"


def score_item(text: str, keywords: list[str], item: dict,
               required: tuple = ()) -> tuple[float, list[str]]:
    t, reasons = score_text(text, keywords, item, required) if keywords else (0.0, [])
    i, why = score_image(item)
    if why:
        reasons.insert(0, why)
    return round(_noisy_or([t, i]), 4), reasons


# ── Pipeline step ─────────────────────────────────────────────────

def _page_text(item: dict) -> str:
    snap = item.get("snapshot_path")
    if not snap or not Path(snap).exists() or (Path(snap) / "error.txt").exists():
        return ""   # failed crawl: score on search-hit metadata alone
    return get_combined_text(Path(snap))


def apply_to_cascade_with_images(
    record: dict,
    keywords: list[str],
    case_dir: Path,
    input_image_path: str = None,
    threshold: float = THRESHOLD,
    required: tuple = (),
) -> dict:
    """
    Image-match every crawled page, score relevance, prune edges to irrelevant
    items and write filter_summary. The root item is always relevant.
    `required` keywords must appear for a page to pass on text alone.
    """
    if input_image_path:
        try:
            from image_matcher import run_image_matching
            record["items"] = run_image_matching(record["items"], input_image_path, case_dir)
        except Exception as e:
            log.warning(f"  [Filter] Image matching failed: {e}")

    for item in record["items"]:
        if not item.get("url"):   # root
            item.update(relevant=True, relevance_score=1.0, relevance_reasons=["root item"])
            continue
        if not keywords and not item.get("image_match"):
            item.update(relevant=True, relevance_score=1.0,
                        relevance_reasons=["no keywords provided"])
            continue
        score, reasons = score_item(_page_text(item), keywords, item, required)
        item.update(relevant=score >= threshold, relevance_score=score,
                    relevance_reasons=reasons)
        log.info(f"  {item['id']}: {'RELEVANT' if item['relevant'] else 'FILTERED'} "
                 f"(score={score:.2f})")

    relevant_ids = {i["id"] for i in record["items"] if i["relevant"]}
    before = len(record["edges"])
    record["edges"] = [e for e in record["edges"] if e[1] in relevant_ids]
    if before != len(record["edges"]):
        log.info(f"Removed {before - len(record['edges'])} edges to filtered items")

    record["filter_summary"] = {
        "keywords":       keywords,
        "required":       list(required),
        "threshold":      threshold,
        "total_items":    len(record["items"]),
        "relevant_items": len(relevant_ids),
        "filtered_items": len(record["items"]) - len(relevant_ids),
        "image_matched":  sum(1 for i in record["items"] if i.get("image_match")),
    }
    return record
