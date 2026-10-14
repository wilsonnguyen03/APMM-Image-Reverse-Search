# test_local.py
# Run from project root: python test_local.py
# Verifies all logic without needing API keys, Docker, or network.

import sys
import json
import os
import tempfile
import struct
import zlib
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent / "src"))

import retrieval
from utils import compute_sha256, log
from cascade import build_cascade
from validator import validate_cascade


def _make_png() -> str:
    """Minimal valid 1x1 PNG for testing."""
    def chunk(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
           + chunk(b"IEND", b""))
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.write(png)
    tmp.close()
    return tmp.name


def test_sha256():
    print("\n── Test 1: SHA-256 ──")
    path = _make_png()
    try:
        h1 = compute_sha256(path)
        h2 = compute_sha256(path)
        assert h1 == h2 and len(h1) == 64
        print(f"  {h1}")
        print("  PASS")
    finally:
        os.unlink(path)


def test_cascade():
    print("\n── Test 2: Cascade builder ──")
    sha = "b" * 64
    hits = [
        {
            "id": "item_000", "url": "https://x.com/i/web/status/1",
            "domain": "x.com", "source_platform": "twitter",
            "match_type": "near-duplicate", "timestamp": "2024-01-01T08:00:00Z",
            "matched_image_url": "https://pbs.twimg.com/media/1.jpg",
            "snapshot_path": None, "post_text": "test",
        },
        {
            "id": "item_001", "url": "https://bbc.com/news/a",
            "domain": "bbc.com", "source_platform": "google_lens",
            "match_type": "exact", "timestamp": "2024-01-01T10:00:00Z",
            "matched_image_url": "https://bbc.com/img/a.jpg",
            "snapshot_path": None,
        },
        {
            "id": "item_002", "url": "https://bbc.com/news/b",
            "domain": "bbc.com", "source_platform": "google_vision",
            "match_type": "exact", "timestamp": "2024-01-01T12:00:00Z",
            "matched_image_url": "https://bbc.com/img/b.jpg",
            "snapshot_path": None,
        },
        {
            "id": "item_003", "url": "https://rt.com/news/x",
            "domain": "rt.com", "source_platform": "instagram",
            "match_type": "near-duplicate", "timestamp": None,
            "snapshot_path": None,
        },
    ]

    record = build_cascade(sha, hits)
    assert record["root_id"] == f"sha256:{sha}"
    assert any(i["id"] == record["root_id"] for i in record["items"])
    assert len(record["items"]) == 5

    # BBC items should be timestamp-chained
    edge_pairs = [(e[0], e[1]) for e in record["edges"]]
    assert ("item_001", "item_002") in edge_pairs, "BBC timestamp edge missing"

    platforms = {i.get("source_platform") for i in record["items"] if i.get("source_platform")}
    print(f"  Items: {len(record['items'])}, Edges: {len(record['edges'])}")
    print(f"  Platforms: {platforms}")
    print(f"  Edges: {record['edges']}")
    print("  PASS")
    return record


def test_validator(valid_record: dict):
    print("\n── Test 3: Validator ──")

    ok, _ = validate_cascade(valid_record)
    assert ok
    print("  3a Valid record: PASS")

    bad = json.loads(json.dumps(valid_record))
    bad["root_id"] = "sha256:" + "z" * 64
    ok, _ = validate_cascade(bad)
    assert not ok
    print("  3b Bad root_id: PASS")

    bad = json.loads(json.dumps(valid_record))
    bad["items"][1]["content_score"] = 0.99
    ok, _ = validate_cascade(bad)
    assert not ok
    print("  3c Banned field: PASS")

    bad = json.loads(json.dumps(valid_record))
    bad["edges"].append(["ghost", "item_000"])
    ok, _ = validate_cascade(bad)
    assert not ok
    print("  3d Dangling edge: PASS")

    degen = {
        "root_id": "sha256:" + "c" * 64,
        "items":   [{"id": "sha256:" + "c" * 64, "url": None}],
        "edges":   [],
    }
    ok, _ = validate_cascade(degen)
    assert ok
    print("  3e Degenerate case: PASS")


SOURCES = ["google_vision", "google_lens", "twitter", "instagram", "facebook", "tiktok", "reddit"]
KEY_VARS = ["GOOGLE_VISION_KEY", "SERPAPI_KEY", "IMGBB_API_KEY",
            "APIFY_TOKEN"]


def test_platforms():
    print("\n── Test 4: All platforms tracked ──")
    sha = "d" * 64
    hits = [
        {"id": f"item_{i:03d}", "url": f"https://example{i}.com/p",
         "domain": f"example{i}.com", "source_platform": src,
         "match_type": "exact", "timestamp": None, "snapshot_path": None}
        for i, src in enumerate(SOURCES)
    ]
    record = build_cascade(sha, hits)
    platforms = {i.get("source_platform") for i in record["items"] if i.get("source_platform")}
    assert platforms == set(SOURCES)
    print(f"  {sorted(platforms)}")
    print("  PASS")


# ── Search sources (HTTP mocked — no keys or network needed) ──────

def _resp(payload):
    r = MagicMock()
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


def _env(**keys):
    """Environment with every API key cleared, then `keys` set."""
    env = {k: "" for k in KEY_VARS}
    env.update(keys)
    return patch.dict(os.environ, env)


def _apify_router(dataset, calls):
    """Fake Apify: start run -> status SUCCEEDED -> dataset items."""
    def post(url, **kw):
        calls.append(("post", url, kw.get("json")))
        return _resp({"data": {"id": "run1"}})

    def get(url, **kw):
        if "/actor-runs/" in url:
            return _resp({"data": {"status": "SUCCEEDED", "defaultDatasetId": "ds1"}})
        if "/datasets/" in url:
            return _resp(dataset)
        raise AssertionError(f"unexpected GET {url}")
    return post, get


def test_vision():
    print("\n── Test 5: Google Vision ──")
    img = _make_png()
    page = lambda u, full=True: {
        "url": u, "pageTitle": "t",
        ("fullMatchingImages" if full else "partialMatchingImages"): [{"url": u + "/i.jpg"}],
    }
    payload = {"responses": [{"webDetection": {
        "bestGuessLabels": [{"label": "x"}],
        "pagesWithMatchingImages": [
            page("https://a.com/1"), page("https://b.com/2", full=False),
            {"url": "https://c.com/pic.jpg"},          # raw image URL: dropped
            page("https://d.com/4"),
        ],
    }}]}
    try:
        with _env(GOOGLE_VISION_KEY="k"), \
             patch.object(retrieval.requests, "post", return_value=_resp(payload)):
            hits = retrieval.search_google_vision(img)
            assert [h["url"] for h in hits] == ["https://a.com/1", "https://b.com/2", "https://d.com/4"]
            assert hits[0]["match_type"] == "exact"
            assert hits[1]["match_type"] == "near-duplicate"
            assert all(h["source_platform"] == "google_vision" for h in hits)
            with patch.object(retrieval, "MAX_VISION_HITS", 2):
                assert len(retrieval.search_google_vision(img)) == 2
    finally:
        os.unlink(img)
    print("  PASS")


def test_lens():
    print("\n── Test 6: Google Lens ──")
    img = _make_png()
    matches = [{"link": f"https://s{i}.com/p", "title": "t", "thumbnail": "th"} for i in range(5)]
    matches.insert(1, {"link": "https://s.com/x.png"})   # image URL: dropped
    post = MagicMock(return_value=_resp({"data": {"url": "https://i.ibb.co/x.jpg"}}))
    get = MagicMock(return_value=_resp({"visual_matches": matches}))
    try:
        with _env(SERPAPI_KEY="k", IMGBB_API_KEY="k"), \
             patch.object(retrieval.requests, "post", post), \
             patch.object(retrieval.requests, "get", get):
            hits = retrieval.search_google_lens(img)
            assert len(hits) == 5 and all(h["source_platform"] == "google_lens" for h in hits)
            assert get.call_args.kwargs["params"]["url"] == "https://i.ibb.co/x.jpg"
            with patch.object(retrieval, "MAX_LENS_HITS", 2):
                assert len(retrieval.search_google_lens(img)) == 1   # cap applies before image-URL filter
        with _env(SERPAPI_KEY="k"):                                   # imgbb key missing
            assert retrieval.search_google_lens(img) == []
        # imgbb times out twice, then works: Lens still runs
        flaky = MagicMock(side_effect=[retrieval.requests.Timeout("slow"),
                                       retrieval.requests.Timeout("slow"),
                                       _resp({"data": {"url": "https://i.ibb.co/x.jpg"}})])
        with _env(SERPAPI_KEY="k", IMGBB_API_KEY="k"), patch.object(retrieval.time, "sleep"), \
             patch.object(retrieval.requests, "post", flaky), \
             patch.object(retrieval.requests, "get", get):
            assert len(retrieval.search_google_lens(img)) == 5 and flaky.call_count == 3
        # imgbb down for every attempt: gives up after IMGBB_ATTEMPTS, no SerpAPI call
        down = MagicMock(side_effect=retrieval.requests.Timeout("slow"))
        get2 = MagicMock()
        with _env(SERPAPI_KEY="k", IMGBB_API_KEY="k"), patch.object(retrieval.time, "sleep"), \
             patch.object(retrieval.requests, "post", down), \
             patch.object(retrieval.requests, "get", get2):
            assert retrieval.search_google_lens(img) == []
            assert down.call_count == retrieval.IMGBB_ATTEMPTS and not get2.called
    finally:
        os.unlink(img)
    print("  PASS")


def test_twitter():
    print("\n── Test 7: Apify Twitter / X ──")
    # Shape from the apidojo/tweet-scraper docs
    dataset = [
        {"url": "https://x.com/bob/status/9", "id": "9", "text": "hello",
         "createdAt": "Fri Nov 24 17:49:36 +0000 2023", "likeCount": 7, "retweetCount": 2,
         "replyCount": 1, "author": {"userName": "bob", "location": "Sydney"},
         "extendedEntities": {"media": [{"media_url_https": "https://pbs.twimg.com/m1.jpg"}]}},
        {"url": "https://x.com/amy/status/10", "text": "no date or media", "author": None},
        {"text": "no url"},                               # dropped
    ]
    calls = []
    post, get = _apify_router(dataset, calls)
    with _env(APIFY_TOKEN="t"), patch.object(retrieval.time, "sleep"), \
         patch.object(retrieval.requests, "post", post), patch.object(retrieval.requests, "get", get):
        hits = retrieval.search_twitter("deepfake", since="2023-03-01", until="2023-04-01")
        assert len(hits) == 2
        h = hits[0]
        assert h["source_platform"] == "twitter" and h["url"] == "https://x.com/bob/status/9"
        assert h["author"] == "bob" and h["location"] == "Sydney"
        assert h["matched_image_url"] == "https://pbs.twimg.com/m1.jpg"
        assert h["timestamp"] == "2023-11-24T17:49:36+00:00"
        assert (h["likes"], h["shares"], h["comments"]) == (7, 2, 1)
        assert hits[1]["timestamp"] == "" and hits[1]["author"] == "" and hits[1]["matched_image_url"] is None
        q = calls[0][2]["searchTerms"][0]
        assert "deepfake" in q and "since:2023-03-01" in q and "until:2023-04-01" in q
        assert "apidojo~tweet-scraper" in calls[0][1]
    print("  PASS")

def test_instagram():
    print("\n── Test 8: Apify Instagram ──")
    dataset = [
        {"shortCode": "AbC", "caption": "cap", "displayUrl": "https://cdn/i.jpg",
         "timestamp": "2024-03-01T00:00:00Z", "ownerUsername": "amy",
         "likesCount": 5, "commentsCount": 2},
        {"url": "https://www.instagram.com/p/XyZ/", "caption": None},
        {"caption": "no url or code"},                 # dropped
    ]
    calls = []
    post, get = _apify_router(dataset, calls)
    with _env(APIFY_TOKEN="t"), patch.object(retrieval.time, "sleep"), \
         patch.object(retrieval.requests, "post", post), patch.object(retrieval.requests, "get", get):
        hits = retrieval.search_apify_instagram("Trump deepfake ab")   # "ab" too short: no hashtag
        assert [h["url"] for h in hits] == ["https://www.instagram.com/p/AbC/",
                                            "https://www.instagram.com/p/XyZ/"]
        assert hits[0]["author"] == "amy" and hits[0]["likes"] == 5
        assert all(h["source_platform"] == "instagram" for h in hits)
        assert calls[0][2]["directUrls"] == [
            "https://www.instagram.com/explore/tags/trump/",
            "https://www.instagram.com/explore/tags/deepfake/"]
        assert "apify~instagram-scraper" in calls[0][1]
    print("  PASS")


def test_facebook():
    print("\n── Test 9: Apify Facebook ──")
    # Shape captured from a live scraper_one/facebook-posts-search run
    dataset = [
        {"url": "https://facebook.com/p/1", "postText": "t1", "timestamp": 1776096008000,
         "attachments": [{"type": "video", "url": "https://fb/v.mp4"},
                         {"type": "photo", "url": "https://fb/img1.jpg"}],
         "author": {"id": "1", "name": "Page"}, "reactionsCount": 3,
         "sharesCount": 4, "commentsCount": 5},
        {"url": "https://facebook.com/p/2", "postText": "t2", "attachments": None},
        {"postText": "no url"},                         # dropped
    ]
    calls = []
    post, get = _apify_router(dataset, calls)
    with _env(APIFY_TOKEN="t"), patch.object(retrieval.time, "sleep"), \
         patch.object(retrieval.requests, "post", post), patch.object(retrieval.requests, "get", get):
        hits = retrieval.search_apify_facebook("deepfake")
        assert len(hits) == 2
        h = hits[0]
        assert h["matched_image_url"] == "https://fb/img1.jpg" and h["author"] == "Page"
        assert h["timestamp"].startswith("2026-04-13") and h["post_text"] == "t1"
        assert (h["likes"], h["shares"], h["comments"]) == (3, 4, 5)
        assert hits[1]["matched_image_url"] == "" and hits[1]["timestamp"] == ""
        assert all(x["source_platform"] == "facebook" for x in hits)
        # input fields must match the actor's schema (query is required)
        assert calls[0][2]["query"] == "deepfake" and calls[0][2]["searchType"] == "top"
    print("  PASS")


def test_tiktok():
    print("\n── Test 9b: Apify TikTok ──")
    # Shape from the xmolodtsov/tiktok-search-scraper docs
    dataset = [
        {"postPage": "https://www.tiktok.com/@a/video/1", "title": "pope puffer",
         "uploadedAtFormatted": "2023-03-25T10:00:00.000Z", "views": 9, "likes": 3,
         "shares": 2, "comments": 1, "channel": {"username": "a"},
         "video": {"url": "https://cdn/signed", "cover": "https://cdn/c.jpg"}},
        {"postPage": "https://www.tiktok.com/@b/video/2", "title": None, "video": None},
        {"title": "no url"},                              # dropped
    ]
    calls = []
    post, get = _apify_router(dataset, calls)
    with _env(APIFY_TOKEN="t"), patch.object(retrieval.time, "sleep"), \
         patch.object(retrieval.requests, "post", post), patch.object(retrieval.requests, "get", get):
        hits = retrieval.search_apify_tiktok("pope puffer")
        assert len(hits) == 2
        h = hits[0]
        assert h["source_platform"] == "tiktok" and h["url"].endswith("/video/1")
        assert h["matched_image_url"] == "https://cdn/c.jpg" and h["author"] == "a"
        assert h["timestamp"] == "2023-03-25T10:00:00.000Z"
        assert (h["views"], h["likes"], h["shares"], h["comments"]) == (9, 3, 2, 1)
        assert hits[1]["timestamp"] == "" and hits[1]["matched_image_url"] == ""
        assert calls[0][2]["keywords"] == ["pope puffer"]
        assert "xmolodtsov~tiktok-search-scraper" in calls[0][1]
    print("  PASS")

def test_reddit():
    print("\n── Test 9c: Apify Reddit ──")
    # Shape from the fatihtahta/reddit-scraper-search-fast docs
    dataset = [
        {"kind": "post", "url": "https://www.reddit.com/r/pics/comments/a/x/", "title": "Pope puffer",
         "body": "is it real?", "created_utc": "2023-03-25T10:09:56.000Z", "author": "u1",
         "subreddit": "pics", "score": 99, "num_comments": 12, "thumbnail": "https://b.thumbs/t.jpg",
         "gallery_images": [{"url": "https://preview.redd.it/g.jpg"}]},
        {"kind": "post", "url": "https://www.reddit.com/r/x/comments/b/y/", "title": "t",
         "thumbnail": "self", "url_overridden_by_dest": "https://i.redd.it/pic.png"},
        {"kind": "post", "url": "https://www.reddit.com/r/x/comments/c/z/", "title": "t",
         "thumbnail": "self"},
        {"kind": "comment", "url": "https://www.reddit.com/r/x/comments/c/z/d/"},   # dropped
        {"title": "no url"},                                                         # dropped
    ]
    calls = []
    post, get = _apify_router(dataset, calls)
    with _env(APIFY_TOKEN="t"), patch.object(retrieval.time, "sleep"),          patch.object(retrieval.requests, "post", post), patch.object(retrieval.requests, "get", get):
        hits = retrieval.search_apify_reddit("pope puffer", since="2023-03-01", until="2023-04-01")
        assert len(hits) == 3
        h = hits[0]
        assert h["source_platform"] == "reddit" and h["subreddit"] == "pics" and h["author"] == "u1"
        assert h["matched_image_url"] == "https://preview.redd.it/g.jpg"       # gallery first
        assert h["timestamp"] == "2023-03-25T10:09:56.000Z"
        assert (h["likes"], h["comments"]) == (99, 12)
        assert hits[1]["matched_image_url"] == "https://i.redd.it/pic.png"     # direct image next
        assert hits[2]["matched_image_url"] == ""                              # "self" thumbnail ignored
        inp = calls[0][2]
        assert inp["queries"] == ["pope puffer"] and inp["dateFrom"] == "2023-03-01" and inp["dateTo"] == "2023-04-01"
        assert "fatihtahta~reddit-scraper-search-fast" in calls[0][1]
        calls.clear()
        retrieval.search_apify_reddit("x")
        assert "dateFrom" not in calls[0][2] and "dateTo" not in calls[0][2]
    print("  PASS")


def test_apify_failure():
    print("\n── Test 10: Apify run failure ──")
    post = MagicMock(return_value=_resp({"data": {"id": "run1"}}))
    get = MagicMock(return_value=_resp({"data": {"status": "FAILED"}}))
    with _env(APIFY_TOKEN="t"), patch.object(retrieval.time, "sleep"), \
         patch.object(retrieval.requests, "post", post), patch.object(retrieval.requests, "get", get):
        assert retrieval.search_apify_instagram("x y z") == []
        assert retrieval.search_apify_facebook("x") == []
        assert retrieval.search_apify_tiktok("x") == []
        assert retrieval.search_apify_reddit("x") == []
    print("  PASS")


def test_missing_keys_and_errors():
    print("\n── Test 11: No keys skip, HTTP errors swallowed ──")
    img = _make_png()
    try:
        boom = MagicMock(side_effect=RuntimeError("network down"))
        with _env(), patch.object(retrieval.requests, "post", boom), \
             patch.object(retrieval.requests, "get", boom):
            assert retrieval.search_google_vision(img) == []
            assert retrieval.search_google_lens(img) == []
            assert retrieval.search_twitter() == []
            assert retrieval.search_apify_instagram("x") == []
            assert retrieval.search_apify_facebook("x") == []
            assert retrieval.search_apify_tiktok("x") == []
            assert retrieval.search_apify_reddit("x") == []
            assert not boom.called, "a keyless source made an HTTP call"
        with _env(GOOGLE_VISION_KEY="k", SERPAPI_KEY="k", IMGBB_API_KEY="k",
                  APIFY_TOKEN="k"), \
             patch.object(retrieval.requests, "post", boom), \
             patch.object(retrieval.requests, "get", boom), \
             patch.object(retrieval.time, "sleep"):
            assert retrieval.search_google_vision(img) == []
            assert retrieval.search_google_lens(img) == []
            assert retrieval.search_twitter() == []
            assert retrieval.search_apify_instagram("x") == []
            assert retrieval.search_apify_facebook("x") == []
            assert retrieval.search_apify_tiktok("x") == []
            assert retrieval.search_apify_reddit("x") == []
    finally:
        os.unlink(img)
    print("  PASS")


def test_run_all_searches():
    print("\n── Test 12: Merge, dedup and caps ──")
    mk = lambda src, *urls: [retrieval._hit(u, src) for u in urls]
    names = {"search_google_vision": "google_vision", "search_google_lens": "google_lens",
             "search_twitter": "twitter", "search_apify_instagram": "instagram",
             "search_apify_facebook": "facebook", "search_apify_tiktok": "tiktok",
             "search_apify_reddit": "reddit"}

    def run(fakes, **limits):
        with ExitStack() as st:
            for name, src in names.items():
                st.enter_context(patch.object(retrieval, name, return_value=mk(src, *fakes.get(src, []))))
            for k, v in limits.items():
                st.enter_context(patch.object(retrieval, k, v))
            return retrieval.run_all_searches("img.jpg", "sha")

    # Dedup: same page in different spellings counts once, Google wins; same domain is fine
    merged = run({"google_vision": ["https://a.com/p", "https://a.com/q"],
                  "google_lens":   ["https://www.a.com/p/?utm_source=x", "https://c.com"],
                  "twitter":       ["https://twitter.com/u/status/1"],
                  "instagram":     ["https://instagram.com/p/1"],
                  "facebook":      ["https://facebook.com/p/1", "https://m.facebook.com/p/1"],
                  "tiktok":        ["https://tiktok.com/@a/video/1"],
                  "reddit":        ["https://reddit.com/r/x/1", "https://old.reddit.com/r/x/1"]})
    urls = [h["url"] for h in merged]
    assert urls == ["https://a.com/p", "https://a.com/q", "https://c.com",
                    "https://twitter.com/u/status/1", "https://instagram.com/p/1",
                    "https://facebook.com/p/1", "https://tiktok.com/@a/video/1",
                    "https://reddit.com/r/x/1"], urls
    assert retrieval._url_key("https://twitter.com/a/status/1/") == retrieval._url_key("https://x.com/a/status/1")
    assert retrieval._url_key("https://youtube.com/watch?v=1") != retrieval._url_key("https://youtube.com/watch?v=2")

    # Cap: Google is the majority, each social platform keeps its quota
    google = [f"https://g{i}.com" for i in range(80)]
    social = {p: [f"https://{p}.com/{i}" for i in range(9)] for p in retrieval.SOCIAL_PLATFORMS}
    merged = run({"google_vision": google, **social})
    by = {}
    for h in merged:
        by[h["source_platform"]] = by.get(h["source_platform"], 0) + 1
    assert len(merged) == 100 and by["google_vision"] == 70, by
    assert all(by[p] == retrieval.SOCIAL_QUOTA for p in retrieval.SOCIAL_PLATFORMS), by

    # Social results beyond the quota only fill slots Google leaves unused
    merged = run({"google_vision": google[:10], **social})
    by = {}
    for h in merged:
        by[h["source_platform"]] = by.get(h["source_platform"], 0) + 1
    assert len(merged) == 10 + 45 and by["google_vision"] == 10 and by["reddit"] == 9, by
    assert [h["url"] for h in merged][:10] == google[:10]       # Google listed first

    # A platform with no results does not eat its slots: Google takes them
    merged = run({"google_vision": google, "reddit": ["https://reddit.com/r/x/1"]})
    assert len(merged) == 81, len(merged)
    print("  PASS")


def test_earliest_window():
    print("\n\u2500\u2500 Test 13: --since alone searches only the following 14 days \u2500\u2500")
    seen = {}
    def fake(name):
        def f(q, since=None, until=None):
            seen[name] = (since, until)
            return []
        return f
    def run(since, until):
        with ExitStack() as st:
            for n in ("search_google_vision", "search_google_lens"):
                st.enter_context(patch.object(retrieval, n, return_value=[]))
            for n in ("search_apify_instagram", "search_apify_facebook", "search_apify_tiktok"):
                st.enter_context(patch.object(retrieval, n, return_value=[]))
            st.enter_context(patch.object(retrieval, "search_twitter", side_effect=fake("twitter")))
            st.enter_context(patch.object(retrieval, "search_apify_reddit", side_effect=fake("reddit")))
            retrieval.run_all_searches("img.jpg", "sha", "q", since, until)
    run("2023-03-20", None)
    assert seen["twitter"] == seen["reddit"] == ("2023-03-20", "2023-04-03"), seen
    run("2023-03-20", "2023-12-31")                      # explicit --until wins
    assert seen["twitter"] == ("2023-03-20", "2023-12-31"), seen
    run(None, None)                                      # no dates: unrestricted
    assert seen["twitter"] == (None, None), seen
    run("not-a-date", None)                              # bad date: no crash, no window
    assert seen["twitter"] == ("not-a-date", None), seen
    print("  PASS")


def test_in_window():
    print("\n\u2500\u2500 Test 14: Social hits outside the date window are dropped \u2500\u2500")
    mk = lambda ts: retrieval._hit("https://x.com/" + str(ts), "instagram", extra={"timestamp": ts})
    hits = [mk("2023-03-25T10:00:00.000Z"), mk("2026-10-13T21:10:28.000Z"), mk(""), mk("garbage"),
            mk("2023-04-03T23:59:00Z"), mk("2023-04-04T00:00:01Z")]
    kept = [h["timestamp"] for h in retrieval._in_window(hits, "2023-03-20", "2023-04-03")]
    assert kept == ["2023-03-25T10:00:00.000Z", "", "garbage", "2023-04-03T23:59:00Z"], kept   # undated kept
    assert retrieval._in_window(hits, None, None) == hits                                        # no window: untouched
    assert len(retrieval._in_window(hits, "2026-01-01", None)) == 3                              # open-ended
    assert retrieval._in_window([], "2023-03-20", "2023-04-03") == []
    print("  PASS")


if __name__ == "__main__":
    print("APMM Retrieval — local tests")
    print("=" * 40)
    test_sha256()
    record = test_cascade()
    test_validator(record)
    test_platforms()
    test_vision()
    test_lens()
    test_twitter()
    test_instagram()
    test_facebook()
    test_tiktok()
    test_reddit()
    test_apify_failure()
    test_missing_keys_and_errors()
    test_run_all_searches()
    test_earliest_window()
    test_in_window()
    print("\n" + "=" * 40)
    print("✓ All tests passed.")
    print()
    print("Next:")
    print("  python src/main.py --image path/to/image.jpg --case-id demo_001 --demo")
