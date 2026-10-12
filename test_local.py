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


SOURCES = ["google_vision", "google_lens", "twitter", "instagram", "facebook"]
KEY_VARS = ["GOOGLE_VISION_KEY", "SERPAPI_KEY", "IMGBB_API_KEY",
            "TWITTER_BEARER_TOKEN", "APIFY_TOKEN"]


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
    finally:
        os.unlink(img)
    print("  PASS")


def test_twitter():
    print("\n── Test 7: Twitter / X ──")
    payload = {
        "data": [{"id": "9", "created_at": "2024-02-01T00:00:00Z", "author_id": "u1",
                  "text": "hello", "attachments": {"media_keys": ["m1"]}}],
        "includes": {"media": [{"media_key": "m1", "url": "https://pbs.twimg.com/m1.jpg"}],
                     "users": [{"id": "u1", "username": "bob", "location": "Sydney"}]},
    }
    with _env(TWITTER_BEARER_TOKEN="t"), \
         patch.object(retrieval.requests, "get", return_value=_resp(payload)) as g:
        hits = retrieval.search_twitter("deepfake")
        assert len(hits) == 1
        h = hits[0]
        assert h["source_platform"] == "twitter" and h["url"].endswith("/9")
        assert h["author"] == "bob" and h["location"] == "Sydney"
        assert h["matched_image_url"] == "https://pbs.twimg.com/m1.jpg"
        assert h["timestamp"] == "2024-02-01T00:00:00Z"
        assert "deepfake" in g.call_args.kwargs["params"]["query"]
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


def test_apify_failure():
    print("\n── Test 10: Apify run failure ──")
    post = MagicMock(return_value=_resp({"data": {"id": "run1"}}))
    get = MagicMock(return_value=_resp({"data": {"status": "FAILED"}}))
    with _env(APIFY_TOKEN="t"), patch.object(retrieval.time, "sleep"), \
         patch.object(retrieval.requests, "post", post), patch.object(retrieval.requests, "get", get):
        assert retrieval.search_apify_instagram("x y z") == []
        assert retrieval.search_apify_facebook("x") == []
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
            assert not boom.called, "a keyless source made an HTTP call"
        with _env(GOOGLE_VISION_KEY="k", SERPAPI_KEY="k", IMGBB_API_KEY="k",
                  TWITTER_BEARER_TOKEN="k", APIFY_TOKEN="k"), \
             patch.object(retrieval.requests, "post", boom), \
             patch.object(retrieval.requests, "get", boom), \
             patch.object(retrieval.time, "sleep"):
            assert retrieval.search_google_vision(img) == []
            assert retrieval.search_google_lens(img) == []
            assert retrieval.search_twitter() == []
            assert retrieval.search_apify_instagram("x") == []
            assert retrieval.search_apify_facebook("x") == []
    finally:
        os.unlink(img)
    print("  PASS")


def test_run_all_searches():
    print("\n── Test 12: Merge, dedup and total cap ──")
    mk = lambda src, *urls: [retrieval._hit(u, src) for u in urls]
    fakes = {
        "search_google_vision":  mk("google_vision", "https://a.com", "https://b.com"),
        "search_google_lens":    mk("google_lens", "https://b.com", "https://c.com"),   # b.com duplicate
        "search_twitter":        mk("twitter", "https://t.com"),
        "search_apify_instagram": mk("instagram", "https://i.com"),
        "search_apify_facebook": mk("facebook", "https://f.com"),
    }
    with ExitStack() as st:
        for name, val in fakes.items():
            st.enter_context(patch.object(retrieval, name, return_value=val))
        merged = retrieval.run_all_searches("img.jpg", "sha")
        urls = [h["url"] for h in merged]
        assert urls == ["https://a.com", "https://b.com", "https://c.com",
                        "https://t.com", "https://i.com", "https://f.com"], urls
        with patch.object(retrieval, "MAX_TOTAL_HITS", 3):
            capped = retrieval.run_all_searches("img.jpg", "sha")
            assert [h["url"] for h in capped] == ["https://a.com", "https://b.com", "https://c.com"]
            # Priority order: earlier sources fill the cap first, later ones are dropped.
            assert {h["source_platform"] for h in capped} == {"google_vision", "google_lens"}
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
    test_apify_failure()
    test_missing_keys_and_errors()
    test_run_all_searches()
    print("\n" + "=" * 40)
    print("✓ All tests passed.")
    print()
    print("Next:")
    print("  python src/main.py --image path/to/image.jpg --case-id demo_001 --demo")
