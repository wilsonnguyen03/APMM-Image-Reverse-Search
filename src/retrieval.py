# src/retrieval.py
# ─────────────────────────────────────────────────────────────────
#  Multi-source reverse image search pipeline.
#
#  Sources (in priority order):
#    1. Google Cloud Vision  — primary, free 1,000/month
#    2. SerpAPI Google Lens  — secondary, 250 free/month permanent
#    3. Twitter/X API        — pay per use ~$0.008 AUD/tweet
#    4. Apify Instagram      — free $5 credit/month
#    5. Apify Facebook       — free $5 credit/month
#
#  All sources skip silently if no key is configured in .env
#
#  ── Key limits ───────────────────────────────────────────────────
MAX_VISION_HITS    = 39    # Google Vision pages (max 100)
MAX_LENS_HITS      = 20    # SerpAPI Lens results
MAX_TWITTER_HITS   = 15  # tweets per query
MAX_APIFY_RESULTS  = 15    # Instagram + Facebook results each
MAX_TOTAL_HITS     = 100  # hard cap across ALL sources

#  ── Date filter ──────────────────────────────────────────────────
#  Only return hits published within this many days.
#  Set to None to disable (recommended — Vision doesn't return dates)
FILTER_DAYS = None
# ─────────────────────────────────────────────────────────────────

import os
import time
import base64
import requests
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
from utils import log


def _hit(url, source, match_type="exact", title=None,
         matched_image_url=None, extra=None):
    return {
        "url":               url,
        "domain":            urlparse(url).netloc,
        "source_platform":   source,
        "match_type":        match_type,
        "page_title":        title,
        "matched_image_url": matched_image_url,
        **(extra or {}),
    }


def _is_image_url(url):
    exts = ('.jpg','.jpeg','.png','.gif','.webp','.avif',
            '.bmp','.tiff','.svg','.ico')
    path = urlparse(url).path.lower().split('?')[0]
    return any(path.endswith(e) for e in exts)


def _within_date_filter(date_str):
    """Return True if date is within FILTER_DAYS, or if no filter set."""
    if not FILTER_DAYS or not date_str:
        return True
    try:
        dt     = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        cutoff = datetime.now(timezone.utc) - timedelta(days=FILTER_DAYS)
        return dt >= cutoff
    except Exception:
        return True  # if we can't parse, keep it


# ── 1. Google Cloud Vision ────────────────────────────────────────

def search_google_vision(image_path):
    """
    Primary reverse image search.
    Returns real web pages containing the image — not raw image URLs.
    Free for first 1,000 calls/month.
    .env: GOOGLE_VISION_KEY
    """
    api_key = os.getenv("GOOGLE_VISION_KEY", "")
    if not api_key:
        log.info("  [Vision] No key — skipping.")
        return []

    hits = []
    try:
        # Auto-convert AVIF/HEIC to JPEG — Vision API doesn't support these
        working_path = image_path
        _ext = image_path.lower().rsplit(".", 1)[-1]
        if _ext in ("avif", "heic", "tiff", "tif"):
            from PIL import Image as _PIL
            _jpg = image_path.rsplit(".", 1)[0] + "_vision.jpg"
            _PIL.open(image_path).convert("RGB").save(_jpg, "JPEG", quality=95)
            working_path = _jpg
            log.info(f"  [Vision] Converted {_ext} → jpg")

        with open(working_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")

        ext  = working_path.lower().rsplit(".", 1)[-1]
        mime = {"jpg":"image/jpeg","jpeg":"image/jpeg","png":"image/png",
                "gif":"image/gif","webp":"image/webp","avif":"image/avif"
                }.get(ext, "image/jpeg")

        r = requests.post(
            f"https://vision.googleapis.com/v1/images:annotate?key={api_key}",
            json={"requests":[{
                "image":    {"content": b64},
                "features": [{"type":"WEB_DETECTION","maxResults": MAX_VISION_HITS}],
            }]},
            timeout=30,
        )
        r.raise_for_status()

        web = r.json().get("responses",[{}])[0].get("webDetection",{})

        labels = [e.get("label","") for e in web.get("bestGuessLabels",[])]
        if labels:
            log.info(f"  [Vision] Best guess: {labels}")

        for page in web.get("pagesWithMatchingImages", []):
            url = page.get("url", "")
            if not url or _is_image_url(url):
                continue
            full    = page.get("fullMatchingImages", [])
            partial = page.get("partialMatchingImages", [])
            img_url = (full or partial or [{}])[0].get("url") if (full or partial) else None
            hits.append(_hit(
                url=url,
                source="google_vision",
                match_type="exact" if full else "near-duplicate",
                title=page.get("pageTitle", ""),
                matched_image_url=img_url,
            ))

    except Exception as e:
        log.warning(f"  [Vision] Error: {e}")

    log.info(f"  [Vision] {len(hits)} page hits")
    return hits[:MAX_VISION_HITS]


# ── 2. SerpAPI Google Lens ────────────────────────────────────────

def search_google_lens(image_path):
    """
    Secondary reverse image search.
    250 free searches/month permanent tier.
    .env: SERPAPI_KEY  IMGBB_API_KEY
    """
    api_key   = os.getenv("SERPAPI_KEY", "")
    imgbb_key = os.getenv("IMGBB_API_KEY", "")

    if not api_key:
        log.info("  [Lens] No SerpAPI key — skipping.")
        return []
    if not imgbb_key:
        log.info("  [Lens] No imgbb key — skipping.")
        return []

    hits = []
    try:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")

        upload = requests.post(
            "https://api.imgbb.com/1/upload",
            data={"key": imgbb_key, "image": b64},
            timeout=30,
        )
        upload.raise_for_status()
        image_url = upload.json().get("data", {}).get("url", "")
        if not image_url:
            log.warning("  [Lens] imgbb upload failed.")
            return []

        r = requests.get(
            "https://serpapi.com/search",
            params={"engine":"google_lens","url":image_url,
                    "api_key":api_key,"no_cache":"true"},
            timeout=30,
        )
        r.raise_for_status()

        for match in r.json().get("visual_matches", [])[:MAX_LENS_HITS]:
            url = match.get("link", "")
            if url and not _is_image_url(url):
                hits.append(_hit(
                    url=url,
                    source="google_lens",
                    match_type="exact",
                    title=match.get("title", ""),
                    matched_image_url=match.get("thumbnail", ""),
                ))

    except Exception as e:
        log.warning(f"  [Lens] Error: {e}")

    log.info(f"  [Lens] {len(hits)} hits")
    return hits


# ── 3. Twitter / X ────────────────────────────────────────────────

def search_twitter(query_text="misinformation"):
    """
    Social media propagation via X API v2.
    Pay per use ~$0.0078 AUD per tweet read.
    .env: TWITTER_BEARER_TOKEN
    """
    token = os.getenv("TWITTER_BEARER_TOKEN", "")
    if not token:
        log.info("  [Twitter] No token — skipping.")
        return []

    def fetch(terms):
        r = requests.get(
            "https://api.twitter.com/2/tweets/search/recent",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "query":        f"{terms} has:images -is:retweet",
                "max_results":  max(10, min(MAX_TWITTER_HITS, 100)),  # API range is 10-100
                "tweet.fields": "created_at,author_id,text",
                "expansions":   "attachments.media_keys,author_id",
                "media.fields": "url,preview_image_url",
                "user.fields":  "username,location",
            },
            timeout=15,
        )
        r.raise_for_status()
        return r.json()

    hits = []
    try:
        words = query_text.split()
        # All keywords first (precise); fall back to any keyword if that finds nothing
        data = fetch(" ".join(words))
        if not data.get("data") and len(words) > 1:
            data = fetch("(" + " OR ".join(words) + ")")

        media_map = {m["media_key"]:m
                     for m in data.get("includes",{}).get("media",[])}
        user_map  = {u["id"]:u
                     for u in data.get("includes",{}).get("users",[])}

        for tweet in data.get("data", []):
            created = tweet.get("created_at", "")
            if not _within_date_filter(created):
                continue

            tweet_id  = tweet["id"]
            tweet_url = f"https://twitter.com/i/web/status/{tweet_id}"
            keys      = tweet.get("attachments",{}).get("media_keys",[])
            img_url   = next(
                (media_map.get(k,{}).get("url") or
                 media_map.get(k,{}).get("preview_image_url")
                 for k in keys if media_map.get(k)), None
            )
            user = user_map.get(tweet.get("author_id",""), {})

            hits.append(_hit(
                url=tweet_url,
                source="twitter",
                match_type="near-duplicate",
                matched_image_url=img_url,
                extra={
                    "tweet_id":  tweet_id,
                    "timestamp": created,
                    "author":    user.get("username",""),
                    "location":  user.get("location",""),
                    "post_text": tweet.get("text","")[:300],
                },
            ))

    except Exception as e:
        log.warning(f"  [Twitter] Error: {e}")

    log.info(f"  [Twitter] {len(hits)} hits")
    return hits


# ── 4 + 5. Apify (Instagram + Facebook) ──────────────────────────

def _run_apify_actor(actor_id, run_input, token, is_task=False):
    """
    Trigger an Apify Actor or Task, wait for it to finish, return results.
    Pass is_task=True to call a saved task instead of an actor directly.
    """
    endpoint = "tasks" if is_task else "acts"
    # Apify API requires / to be replaced with ~ in actor IDs
    encoded_id = actor_id.replace("/", "~")
    start = requests.post(
        f"https://api.apify.com/v2/{endpoint}/{encoded_id}/runs",
        headers={"Authorization": f"Bearer {token}"},
        json=run_input,
        timeout=30,
    )
    start.raise_for_status()
    run_id = start.json().get("data", {}).get("id")
    if not run_id:
        raise ValueError("No run ID returned from Apify")

    log.info(f"  [Apify] Run {run_id} started for {actor_id}")

    for attempt in range(36):
        time.sleep(5)
        status_r = requests.get(
            f"https://api.apify.com/v2/actor-runs/{run_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        status_r.raise_for_status()
        status = status_r.json().get("data", {}).get("status", "")
        log.info(f"  [Apify] Status: {status} ({attempt*5}s)")
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            break

    if status != "SUCCEEDED":
        raise RuntimeError(f"Apify run ended with status: {status}")

    dataset_id = status_r.json().get("data", {}).get("defaultDatasetId")
    results_r  = requests.get(
        f"https://api.apify.com/v2/datasets/{dataset_id}/items",
        headers={"Authorization": f"Bearer {token}"},
        params={"limit": MAX_APIFY_RESULTS, "format": "json"},
        timeout=30,
    )
    results_r.raise_for_status()
    return results_r.json()


def search_apify_instagram(query_text):
    """
    Search Instagram public posts via Apify.
    Uses apify/instagram-scraper — searches by hashtag.
    Result limit controlled by MAX_APIFY_RESULTS.
    .env: APIFY_TOKEN
    """
    token = os.getenv("APIFY_TOKEN", "")
    if not token:
        log.info("  [Instagram] No Apify token — skipping.")
        return []

    # Convert query words to hashtags — take first 3 meaningful words
    hashtags = [
        "#" + w.strip("#").replace(" ","").lower()
        for w in query_text.split()[:3]
        if len(w.strip("#")) > 2
    ]
    if not hashtags:
        hashtags = ["#misinformation"]

    log.info(f"  [Instagram] Searching hashtags: {hashtags}")

    hits = []
    try:
        results = _run_apify_actor(
            actor_id="apify/instagram-scraper",
            run_input={
                "directUrls": [
                    f"https://www.instagram.com/explore/tags/{h.strip('#')}/"
                    for h in hashtags
                ],
                "resultsType":  "posts",
                "resultsLimit": MAX_APIFY_RESULTS,
            },
            token=token,
            is_task=False,
        )

        for post in results:
            shortcode = post.get("shortCode") or post.get("id","")
            url = post.get("url","") or (
                f"https://www.instagram.com/p/{shortcode}/" if shortcode else ""
            )
            if not url:
                continue

            timestamp = str(post.get("timestamp") or post.get("takenAtTs",""))
            if not _within_date_filter(timestamp):
                continue

            hits.append(_hit(
                url=url,
                source="instagram",
                match_type="near-duplicate",
                title=(post.get("caption","") or "")[:100],
                matched_image_url=post.get("displayUrl") or post.get("imageUrl",""),
                extra={
                    "timestamp": timestamp,
                    "author":    post.get("ownerUsername",""),
                    "location":  post.get("locationName",""),
                    "post_text": (post.get("caption","") or "")[:300],
                    "likes":     post.get("likesCount", 0),
                    "comments":  post.get("commentsCount", 0),
                },
            ))

    except Exception as e:
        log.warning(f"  [Instagram] Apify error: {e}")

    log.info(f"  [Instagram] {len(hits)} hits")
    return hits


def search_apify_facebook(query_text):
    """
    Search Facebook public posts by keyword via Apify.
    Uses actor scraper_one/facebook-posts-search (id TMBawM4LZpKN15DZX).
    .env: APIFY_TOKEN
    """
    token = os.getenv("APIFY_TOKEN", "")
    if not token:
        log.info("  [Facebook] No Apify token — skipping.")
        return []

    log.info(f"  [Facebook] Searching for: {query_text}")

    hits = []
    try:
        results = _run_apify_actor(
            actor_id="scraper_one/facebook-posts-search",
            run_input={
                "query":        query_text,
                "resultsCount": MAX_APIFY_RESULTS,
                "searchType":   "top",
            },
            token=token,
            is_task=False,
        )

        for post in results:
            url = post.get("url", "")
            if not url:
                continue

            # The actor returns epoch milliseconds
            ms = post.get("timestamp")
            timestamp = (datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()
                         if isinstance(ms, (int, float)) else "")
            if not _within_date_filter(timestamp):
                continue

            photo = next((a.get("url", "") for a in post.get("attachments") or []
                          if a.get("type") == "photo"), "")
            text = post.get("postText", "") or ""
            hits.append(_hit(
                url=url,
                source="facebook",
                match_type="near-duplicate",
                title=text[:100],
                matched_image_url=photo,
                extra={
                    "timestamp": timestamp,
                    "author":    (post.get("author") or {}).get("name", ""),
                    "post_text": text[:300],
                    "likes":     post.get("reactionsCount", 0),
                    "shares":    post.get("sharesCount", 0),
                    "comments":  post.get("commentsCount", 0),
                },
            ))

    except Exception as e:
        log.warning(f"  [Facebook] Apify error: {e}")

    log.info(f"  [Facebook] {len(hits)} hits")
    return hits


# ── Combined runner ───────────────────────────────────────────────

def run_all_searches(image_path, image_sha256, query_text="misinformation"):
    """
    Run all sources in priority order.
    Sources with no key configured are silently skipped.
    Deduplicates on URL. Hard cap at MAX_TOTAL_HITS.
    """
    log.info(f"Starting multi-source search (cap: {MAX_TOTAL_HITS})...")

    seen, merged = set(), []

    def _add(hits):
        for hit in hits:
            url = hit.get("url","")
            if url and url not in seen and len(merged) < MAX_TOTAL_HITS:
                seen.add(url)
                merged.append(hit)

    _add(search_google_vision(image_path))   # primary
    _add(search_google_lens(image_path))     # secondary
    _add(search_twitter(query_text))         # social — X
    _add(search_apify_instagram(query_text)) # social — Instagram
    _add(search_apify_facebook(query_text))  # social — Facebook

    # Log source breakdown
    by_source = {}
    for h in merged:
        s = h.get("source_platform","?")
        by_source[s] = by_source.get(s,0) + 1
    log.info(f"Source breakdown: {by_source}")
    log.info(f"Total unique hits: {len(merged)}")
    return merged
