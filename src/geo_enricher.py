# src/geo_enricher.py
# ─────────────────────────────────────────────────────────────────
#  Adds country and coordinate data to cascade items.
#
#  Strategy (in order):
#    1. Static lookup table for major news/social domains (instant)
#    2. TLD-based inference (.co.uk → GB, .com.au → AU etc.)
#    3. Free IP geolocation via ip-api.com (no key needed)
#
#  Adds to each item:
#    country      — full name e.g. "United States"
#    country_code — ISO 3166-1 alpha-2 e.g. "US"
#    lat          — latitude (float)
#    lon          — longitude (float)
#    city         — city if known
# ─────────────────────────────────────────────────────────────────

import socket
import time
import requests
from urllib.parse import urlparse
from utils import log

# ── Static domain → country mapping ──────────────────────────────
# Covers the most common outlets — avoids API calls for known domains
DOMAIN_COUNTRY = {
    # USA
    "reuters.com":          ("United States", "US", 38.9, -77.0),
    "apnews.com":           ("United States", "US", 38.9, -77.0),
    "nytimes.com":          ("United States", "US", 40.7, -74.0),
    "washingtonpost.com":   ("United States", "US", 38.9, -77.0),
    "wsj.com":              ("United States", "US", 40.7, -74.0),
    "nbcnews.com":          ("United States", "US", 40.7, -74.0),
    "cbsnews.com":          ("United States", "US", 40.7, -74.0),
    "abcnews.go.com":       ("United States", "US", 40.7, -74.0),
    "foxnews.com":          ("United States", "US", 40.7, -74.0),
    "cnn.com":              ("United States", "US", 33.7, -84.4),
    "usatoday.com":         ("United States", "US", 38.9, -77.0),
    "politico.com":         ("United States", "US", 38.9, -77.0),
    "thehill.com":          ("United States", "US", 38.9, -77.0),
    "huffpost.com":         ("United States", "US", 40.7, -74.0),
    "buzzfeednews.com":     ("United States", "US", 40.7, -74.0),
    "vox.com":              ("United States", "US", 38.9, -77.0),
    "axios.com":            ("United States", "US", 38.9, -77.0),
    "propublica.org":       ("United States", "US", 40.7, -74.0),
    "wired.com":            ("United States", "US", 37.7, -122.4),
    "techcrunch.com":       ("United States", "US", 37.7, -122.4),
    "theverge.com":         ("United States", "US", 40.7, -74.0),
    "snopes.com":           ("United States", "US", 37.7, -122.4),
    "politifact.com":       ("United States", "US", 30.3, -81.6),
    "factcheck.org":        ("United States", "US", 38.9, -77.0),
    # Social media (US-headquartered)
    "facebook.com":         ("United States", "US", 37.5, -122.2),
    "instagram.com":        ("United States", "US", 37.5, -122.2),
    "twitter.com":          ("United States", "US", 37.8, -122.4),
    "x.com":                ("United States", "US", 37.8, -122.4),
    "youtube.com":          ("United States", "US", 37.4, -122.0),
    "tiktok.com":           ("United States", "US", 37.4, -122.0),
    "reddit.com":           ("United States", "US", 37.8, -122.4),
    "linkedin.com":         ("United States", "US", 37.4, -122.0),
    # UK
    "bbc.com":              ("United Kingdom", "GB", 51.5, -0.1),
    "bbc.co.uk":            ("United Kingdom", "GB", 51.5, -0.1),
    "theguardian.com":      ("United Kingdom", "GB", 51.5, -0.1),
    "independent.co.uk":    ("United Kingdom", "GB", 51.5, -0.1),
    "telegraph.co.uk":      ("United Kingdom", "GB", 51.5, -0.1),
    "thetimes.co.uk":       ("United Kingdom", "GB", 51.5, -0.1),
    "dailymail.co.uk":      ("United Kingdom", "GB", 51.5, -0.1),
    "mirror.co.uk":         ("United Kingdom", "GB", 51.5, -0.1),
    "sky.com":              ("United Kingdom", "GB", 51.5, -0.1),
    # Australia
    "abc.net.au":           ("Australia", "AU", -33.8, 151.2),
    "smh.com.au":           ("Australia", "AU", -33.8, 151.2),
    "theage.com.au":        ("Australia", "AU", -37.8, 144.9),
    "news.com.au":          ("Australia", "AU", -33.8, 151.2),
    "theaustralian.com.au": ("Australia", "AU", -33.8, 151.2),
    "9news.com.au":         ("Australia", "AU", -33.8, 151.2),
    "7news.com.au":         ("Australia", "AU", -33.8, 151.2),
    "crikey.com.au":        ("Australia", "AU", -37.8, 144.9),
    # Canada
    "cbc.ca":               ("Canada", "CA", 45.4, -75.7),
    "globeandmail.com":     ("Canada", "CA", 43.6, -79.4),
    "torontostar.com":      ("Canada", "CA", 43.6, -79.4),
    "nationalpost.com":     ("Canada", "CA", 43.6, -79.4),
    # Germany
    "dw.com":               ("Germany", "DE", 52.5, 13.4),
    "spiegel.de":           ("Germany", "DE", 53.5, 10.0),
    "zeit.de":              ("Germany", "DE", 53.5, 10.0),
    # France
    "france24.com":         ("France", "FR", 48.8, 2.3),
    "lemonde.fr":           ("France", "FR", 48.8, 2.3),
    # India
    "thehindu.com":         ("India", "IN", 28.6, 77.2),
    "ndtv.com":             ("India", "IN", 28.6, 77.2),
    "timesofindia.com":     ("India", "IN", 28.6, 77.2),
    "hindustantimes.com":   ("India", "IN", 28.6, 77.2),
    # Russia
    "rt.com":               ("Russia", "RU", 55.7, 37.6),
    "tass.com":             ("Russia", "RU", 55.7, 37.6),
    # Qatar
    "aljazeera.com":        ("Qatar", "QA", 25.3, 51.5),
    # Japan
    "japantimes.co.jp":     ("Japan", "JP", 35.7, 139.7),
    "nhk.or.jp":            ("Japan", "JP", 35.7, 139.7),
    # Singapore
    "straitstimes.com":     ("Singapore", "SG", 1.3, 103.8),
    "channelnewsasia.com":  ("Singapore", "SG", 1.3, 103.8),
}

# Country centroids for coordinate fallback
COUNTRY_COORDS = {
    "US": (38.9, -98.5),  "GB": (54.0, -2.0),   "AU": (-25.3, 133.8),
    "CA": (56.1, -106.3), "DE": (51.2, 10.5),    "FR": (46.2, 2.2),
    "IN": (20.6, 78.9),   "RU": (61.5, 105.3),   "JP": (36.2, 138.3),
    "CN": (35.9, 104.2),  "BR": (-14.2, -51.9),  "MX": (23.6, -102.6),
    "ZA": (-30.6, 22.9),  "NG": (9.1, 8.7),      "EG": (26.8, 30.8),
    "SG": (1.3, 103.8),   "QA": (25.4, 51.2),    "AE": (23.4, 53.8),
    "NZ": (-40.9, 174.9), "PK": (30.4, 69.3),    "BD": (23.7, 90.4),
}

# TLD → country code mapping
TLD_COUNTRY = {
    ".co.uk": "GB",  ".ac.uk": "GB",  ".org.uk": "GB",
    ".com.au": "AU", ".net.au": "AU", ".org.au": "AU", ".gov.au": "AU",
    ".ca": "CA",     ".de": "DE",     ".fr": "FR",     ".ru": "RU",
    ".cn": "CN",     ".jp": "JP",     ".in": "IN",     ".br": "BR",
    ".mx": "MX",     ".za": "ZA",     ".ng": "NG",     ".eg": "EG",
    ".sg": "SG",     ".nz": "NZ",     ".pk": "PK",     ".bd": "BD",
    ".au": "AU",     ".uk": "GB",
}

COUNTRY_NAMES = {
    "US": "United States", "GB": "United Kingdom", "AU": "Australia",
    "CA": "Canada",        "DE": "Germany",        "FR": "France",
    "IN": "India",         "RU": "Russia",         "JP": "Japan",
    "CN": "China",         "BR": "Brazil",         "MX": "Mexico",
    "ZA": "South Africa",  "NG": "Nigeria",        "EG": "Egypt",
    "SG": "Singapore",     "QA": "Qatar",          "AE": "UAE",
    "NZ": "New Zealand",   "PK": "Pakistan",       "BD": "Bangladesh",
}


def _extract_domain(url: str) -> str:
    """Extract base domain from URL."""
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower().replace("www.", "")
        return host
    except Exception:
        return ""


def _lookup_static(domain: str) -> tuple | None:
    """Check static domain table. Returns (country, code, lat, lon) or None."""
    # Exact match
    if domain in DOMAIN_COUNTRY:
        return DOMAIN_COUNTRY[domain]
    # Strip subdomains
    parts = domain.split(".")
    for i in range(len(parts)):
        candidate = ".".join(parts[i:])
        if candidate in DOMAIN_COUNTRY:
            return DOMAIN_COUNTRY[candidate]
    return None


def _lookup_tld(domain: str) -> tuple | None:
    """Infer country from TLD. Returns (country, code, lat, lon) or None."""
    for tld, code in TLD_COUNTRY.items():
        if domain.endswith(tld):
            name   = COUNTRY_NAMES.get(code, code)
            coords = COUNTRY_COORDS.get(code, (0, 0))
            return (name, code, coords[0], coords[1])
    return None


def _lookup_ip_api(domain: str) -> tuple | None:
    """
    Free IP geolocation via ip-api.com (no key, 45 req/min limit).
    Resolves domain to IP then geolocates.
    """
    try:
        ip = socket.gethostbyname(domain)
        r  = requests.get(
            f"http://ip-api.com/json/{ip}",
            params={"fields": "country,countryCode,lat,lon,city"},
            timeout=5,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if data.get("status") == "fail":
            return None
        return (
            data.get("country", ""),
            data.get("countryCode", ""),
            data.get("lat", 0),
            data.get("lon", 0),
        )
    except Exception:
        return None


def enrich_item(item: dict) -> dict:
    """
    Add country/geo fields to a single cascade item.
    Returns the item with geo fields added.
    """
    url = item.get("url", "")
    if not url:
        return item

    domain = _extract_domain(url)
    if not domain:
        return item

    result = None

    # 1. Static lookup (instant)
    result = _lookup_static(domain)

    # 2. TLD inference
    if not result:
        result = _lookup_tld(domain)

    # 3. IP geolocation (network call)
    if not result:
        result = _lookup_ip_api(domain)
        time.sleep(0.5)  # respect ip-api rate limit

    if result:
        item["country"]      = result[0]
        item["country_code"] = result[1]
        item["lat"]          = result[2]
        item["lon"]          = result[3]
    else:
        item["country"]      = None
        item["country_code"] = None
        item["lat"]          = None
        item["lon"]          = None

    return item


def enrich_cascade(record: dict) -> dict:
    """Add geo fields to all items in a cascade record."""
    log.info("Running geographic enrichment...")
    enriched = 0

    for item in record["items"]:
        if not item.get("url"):
            continue
        enrich_item(item)
        if item.get("country"):
            enriched += 1
            log.info(
                f"  {item.get('id','?')}: {item.get('domain','?')} → "
                f"{item['country']} ({item['country_code']})"
            )

    log.info(f"Geo enrichment complete: {enriched} items located")
    return record
