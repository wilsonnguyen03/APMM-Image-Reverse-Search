# src/retrieval.py
# ─────────────────────────────────────────────────────────────────
#  Multi-source reverse image search pipeline.
#
#  Sources (in priority order):
#    1. Google Cloud Vision  — primary, free 1,000/month
#    2. SerpAPI Google Lens  — secondary, 250 free/month permanent
#    3. Apify Twitter/X      — ~$0.40 per 1,000 tweets, supports date range
#    4. Apify Instagram      — free $5 credit/month
#    5. Apify Facebook       — free $5 credit/month
#    6. Apify TikTok         — ~$0.30 per 1,000 results
#    7. Apify Reddit         — ~$1.19 per 1,000 results, supports date range
#
#  All sources skip silently if no key is configured in .env
#
#  ── Key limits ───────────────────────────────────────────────────
MAX_VISION_HITS    = 45    # Google Vision pages (max 100)
MAX_LENS_HITS      = 30    # SerpAPI Lens results
IMGBB_ATTEMPTS     = 3     # imgbb upload tries before Lens is skipped
MAX_TWITTER_HITS   = 10    # tweets per query
MAX_APIFY_RESULTS  = 10    # results per Apify source (Instagram, Facebook, TikTok, Reddit)
MAX_TOTAL_HITS     = 100   # hard cap across ALL sources
#  Every social platform is guaranteed this many slots (if it returns that many);
#  Google Vision + Lens fill the rest, so they stay the majority. Any social
#  results beyond the quota only fill slots Google leaves unused.
SOCIAL_QUOTA       = 6
#  --since without --until searches Twitter + Reddit for this many days after it
EARLIEST_WINDOW_DAYS = 14
SOCIAL_PLATFORMS   = ("twitter", "instagram", "facebook", "tiktok", "reddit")

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
from urllib.parse import urlparse, parse_qsl, urlencode
from utils import log

_HOST_ALIASES = {
    "twitter.com": "x.com", "mobile.twitter.com": "x.com",
    "m.facebook.com": "facebook.com", "mbasic.facebook.com": "facebook.com",
    "old.reddit.com": "reddit.com", "np.reddit.com": "reddit.com",
    "m.youtube.com": "youtube.com",
}
_TRACKING_PARAMS = {"fbclid", "igshid", "gclid", "si", "feature"}


def _url_key(url):
    """Normalised URL used only for de-duplication (same page, different spelling)."""
    p = urlparse(url.strip())
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    host = _HOST_ALIASES.get(host, host)
    query = sorted((k, v) for k, v in parse_qsl(p.query)
                   if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS)
    return host + p.path.rstrip("/") + ("?" + urlencode(query) if query else "")


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

        # imgbb is occasionally slow or down; retry before giving up on Lens
        image_url = ""
        for attempt in range(1, IMGBB_ATTEMPTS + 1):
            try:
                upload = requests.post(
                    "https://api.imgbb.com/1/upload",
                    data={"key": imgbb_key, "image": b64},
                    timeout=(10, 30),
                )
                upload.raise_for_status()
                image_url = upload.json().get("data", {}).get("url", "")
                if image_url:
                    break
                log.warning(f"  [Lens] imgbb returned no URL (attempt {attempt}/{IMGBB_ATTEMPTS})")
            except requests.RequestException as e:
                log.warning(f"  [Lens] imgbb upload failed (attempt {attempt}/{IMGBB_ATTEMPTS}): {e}")
            if attempt < IMGBB_ATTEMPTS:
                time.sleep(3 * attempt)
        if not image_url:
            log.warning("  [Lens] imgbb upload failed — skipping Lens.")
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


# ── 3. Twitter / X (Apify) ────────────────────────────────────────

def _parse_tweet_date(s):
    """Actor returns 'Fri Nov 24 17:49:36 +0000 2023'; convert to ISO 8601."""
    try:
        return datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y").isoformat()
    except (ValueError, TypeError):
        return ""


def search_twitter(query_text="misinformation", since=None, until=None):
    """
    Search X/Twitter posts by keyword via Apify, with optional history window.
    Uses actor apidojo/tweet-scraper.
    since / until: 'YYYY-MM-DD' strings, applied as since:/until: search operators
                   (the actor's recommended way to filter by date).
    .env: APIFY_TOKEN
    """
    token = os.getenv("APIFY_TOKEN", "")
    if not token:
        log.info("  [Twitter] No Apify token — skipping.")
        return []

    def fetch(terms):
        q = f"{terms} -filter:nativeretweets"
        if since:
            q += f" since:{since}"
        if until:
            q += f" until:{until}"
        log.info(f"  [Twitter] Searching for: {q}")
        return _run_apify_actor(
            actor_id="apidojo/tweet-scraper",
            run_input={
                "searchTerms": [q],
                "maxItems":    MAX_TWITTER_HITS,
                "sort":        "Top",
                "onlyImage":   True,
            },
            token=token,
            is_task=False,
        )

    hits = []
    try:
        words = query_text.split()
        # All keywords first (precise); fall back to any keyword if that finds nothing
        results = fetch(" ".join(words))
        if not results and len(words) > 1:
            results = fetch("(" + " OR ".join(words) + ")")

        for tweet in results:
            url = tweet.get("url", "")
            if not url:
                continue

            created = _parse_tweet_date(tweet.get("createdAt", ""))
            if not _within_date_filter(created):
                continue

            author = tweet.get("author") or {}
            text = tweet.get("text", "") or ""
            # Media is not documented for plain tweets; take it if the actor includes it
            media = (tweet.get("extendedEntities") or {}).get("media") or []
            img_url = media[0].get("media_url_https") if media else None

            hits.append(_hit(
                url=url,
                source="twitter",
                match_type="near-duplicate",
                title=text[:100],
                matched_image_url=img_url,
                extra={
                    "tweet_id":  tweet.get("id", ""),
                    "timestamp": created,
                    "author":    author.get("userName", ""),
                    "location":  author.get("location", ""),
                    "post_text": text[:300],
                    "likes":     tweet.get("likeCount", 0),
                    "shares":    tweet.get("retweetCount", 0),
                    "comments":  tweet.get("replyCount", 0),
                },
            ))

    except Exception as e:
        log.warning(f"  [Twitter] Apify error: {e}")

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


def search_apify_tiktok(query_text):
    """
    Search TikTok videos by keyword via Apify.
    Uses actor xmolodtsov/tiktok-search-scraper.
    .env: APIFY_TOKEN
    """
    token = os.getenv("APIFY_TOKEN", "")
    if not token:
        log.info("  [TikTok] No Apify token — skipping.")
        return []

    log.info(f"  [TikTok] Searching for: {query_text}")

    hits = []
    try:
        results = _run_apify_actor(
            actor_id="xmolodtsov/tiktok-search-scraper",
            run_input={
                "keywords":              [query_text],
                "maxItems":              MAX_APIFY_RESULTS,
                "sortType":              "RELEVANCE",
                "includeSearchKeywords": False,
                "uniqueAuthors":         False,
            },
            token=token,
            is_task=False,
        )

        for post in results:
            # postPage is the permanent link; video.url is a short-lived CDN link
            url = post.get("postPage", "")
            if not url:
                continue

            timestamp = post.get("uploadedAtFormatted", "") or ""
            if not _within_date_filter(timestamp):
                continue

            text = post.get("title", "") or ""
            video = post.get("video") or {}
            channel = post.get("channel") or {}
            hits.append(_hit(
                url=url,
                source="tiktok",
                match_type="near-duplicate",
                title=text[:100],
                matched_image_url=video.get("cover") or video.get("thumbnail", ""),
                extra={
                    "timestamp": timestamp,
                    "author":    channel.get("username", ""),
                    "post_text": text[:300],
                    "views":     post.get("views", 0),
                    "likes":     post.get("likes", 0),
                    "shares":    post.get("shares", 0),
                    "comments":  post.get("comments", 0),
                },
            ))

    except Exception as e:
        log.warning(f"  [TikTok] Apify error: {e}")

    log.info(f"  [TikTok] {len(hits)} hits")
    return hits


def search_apify_reddit(query_text, since=None, until=None):
    """
    Search Reddit posts by keyword via Apify, with optional date window.
    Uses actor fatihtahta/reddit-scraper-search-fast.
    since / until: 'YYYY-MM-DD' strings (the actor's dateFrom / dateTo).
    .env: APIFY_TOKEN
    """
    token = os.getenv("APIFY_TOKEN", "")
    if not token:
        log.info("  [Reddit] No Apify token — skipping.")
        return []

    log.info(f"  [Reddit] Searching for: {query_text}")

    run_input = {
        "queries":        [query_text],
        "maxPosts":       MAX_APIFY_RESULTS,
        "sort":           "relevance",
        "includeNsfw":    False,
        "scrapeComments": False,
    }
    if since:
        run_input["dateFrom"] = since
    if until:
        run_input["dateTo"] = until

    hits = []
    try:
        results = _run_apify_actor(
            actor_id="fatihtahta/reddit-scraper-search-fast",
            run_input=run_input,
            token=token,
            is_task=False,
        )

        for post in results:
            url = post.get("url", "")
            if not url or post.get("kind", "post") != "post":
                continue

            timestamp = post.get("created_utc", "") or ""
            if not _within_date_filter(timestamp):
                continue

            # Prefer a gallery image, then a direct image link, then the thumbnail
            gallery = post.get("gallery_images") or []
            direct = post.get("url_overridden_by_dest", "") or ""
            img_url = (gallery[0].get("url") if gallery
                       else direct if _is_image_url(direct)
                       else post.get("thumbnail", "") or "")
            if img_url and not img_url.startswith("http"):   # "self", "default", ...
                img_url = ""

            text = post.get("body", "") or ""
            title = post.get("title", "") or ""
            hits.append(_hit(
                url=url,
                source="reddit",
                match_type="near-duplicate",
                title=title[:100],
                matched_image_url=img_url,
                extra={
                    "timestamp": timestamp,
                    "author":    post.get("author", ""),
                    "subreddit": post.get("subreddit", ""),
                    "post_text": (title + " " + text).strip()[:300],
                    "likes":     post.get("score", 0),
                    "comments":  post.get("num_comments", 0),
                },
            ))

    except Exception as e:
        log.warning(f"  [Reddit] Apify error: {e}")

    log.info(f"  [Reddit] {len(hits)} hits")
    return hits


# ── Combined runner ───────────────────────────────────────────────

def _in_window(hits, since, until):
    """Drop hits dated outside [since, until] (YYYY-MM-DD). Undated hits are kept."""
    if not (since or until) or not hits:
        return hits
    kept = []
    for h in hits:
        try:
            day = datetime.fromisoformat(h.get("timestamp", "").replace("Z", "+00:00")).strftime("%Y-%m-%d")
        except (ValueError, AttributeError):
            kept.append(h)
            continue
        if (not since or day >= since) and (not until or day <= until):
            kept.append(h)
    if len(kept) < len(hits):
        log.info(f"  [{hits[0].get('source_platform', '?')}] dropped {len(hits) - len(kept)} "
                 f"hit(s) dated outside {since or '…'} to {until or '…'}")
    return kept


def run_all_searches(image_path, image_sha256, query_text="misinformation",
                     since=None, until=None):
    """
    Run all sources. Sources with no key configured are silently skipped.
    Deduplicates on normalised URL (the same domain may repeat; the same page
    may not). Google sources win a duplicate. Hard cap at MAX_TOTAL_HITS, split so
    Google Vision + Lens are the majority and every social platform keeps its
    SOCIAL_QUOTA slots.
    """
    log.info(f"Starting multi-source search (cap: {MAX_TOTAL_HITS})...")

    # --since alone means "the earliest posts": search only the window right after it
    if since and not until:
        try:
            until = (datetime.strptime(since, "%Y-%m-%d")
                     + timedelta(days=EARLIEST_WINDOW_DAYS)).strftime("%Y-%m-%d")
            log.info(f"  No --until given: searching {since} to {until}")
        except ValueError:
            log.warning(f"  --since '{since}' is not YYYY-MM-DD; ignoring window")

    seen = set()

    def _unique(hits):
        out = []
        for hit in hits:
            url = hit.get("url", "")
            key = _url_key(url) if url else ""
            if key and key not in seen:
                seen.add(key)
                out.append(hit)
        return out

    google = _unique(search_google_vision(image_path)) \
           + _unique(search_google_lens(image_path))

    # Only Twitter and Reddit can filter by date server-side. The others (Instagram
    # hashtag feeds especially) return today's posts, so drop anything outside the
    # window after fetching.
    social = {
        "twitter":   _unique(search_twitter(query_text, since, until)),
        "instagram": _unique(_in_window(search_apify_instagram(query_text), since, until)),
        "facebook":  _unique(_in_window(search_apify_facebook(query_text), since, until)),
        "tiktok":    _unique(_in_window(search_apify_tiktok(query_text), since, until)),
        "reddit":    _unique(search_apify_reddit(query_text, since, until)),
    }

    reserved = [h for p in SOCIAL_PLATFORMS for h in social[p][:SOCIAL_QUOTA]]
    extras   = [h for p in SOCIAL_PLATFORMS for h in social[p][SOCIAL_QUOTA:]]
    reserved = reserved[:MAX_TOTAL_HITS]
    merged   = google[:MAX_TOTAL_HITS - len(reserved)] + reserved
    merged  += extras[:MAX_TOTAL_HITS - len(merged)]

    # Log source breakdown
    by_source = {}
    for h in merged:
        s = h.get("source_platform","?")
        by_source[s] = by_source.get(s,0) + 1
    log.info(f"Source breakdown: {by_source}")
    log.info(f"Total unique hits: {len(merged)}")
    return merged
