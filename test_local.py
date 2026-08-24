# test_local.py
# Run from project root: python test_local.py
# Verifies all logic without needing API keys, Docker, or network.

import sys
import json
import os
import tempfile
import struct
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

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
            "id": "item_000", "url": "https://boards.4chan.org/pol/thread/1#p1",
            "domain": "boards.4chan.org", "source_platform": "4chan",
            "match_type": "exact", "timestamp": "2024-01-01T08:00:00Z",
            "matched_image_url": "https://i.4cdn.org/pol/1.jpg",
            "snapshot_path": None, "board": "pol", "post_text": "test",
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
            "domain": "bbc.com", "source_platform": "tineye",
            "match_type": "exact", "timestamp": "2024-01-01T12:00:00Z",
            "matched_image_url": "https://bbc.com/img/b.jpg",
            "snapshot_path": None,
        },
        {
            "id": "item_003", "url": "https://rt.com/news/x",
            "domain": "rt.com", "source_platform": "yandex",
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


def test_platforms():
    print("\n── Test 4: All platforms tracked ──")
    sha = "d" * 64
    hits = [
        {"id": f"item_{i:03d}", "url": f"https://example{i}.com/p",
         "domain": f"example{i}.com", "source_platform": src,
         "match_type": "exact", "timestamp": None, "snapshot_path": None}
        for i, src in enumerate(["4chan", "yandex", "google_lens", "zenserp"])
    ]
    record = build_cascade(sha, hits)
    platforms = {i.get("source_platform") for i in record["items"] if i.get("source_platform")}
    assert len(platforms) == 4
    print(f"  {platforms}")
    print("  PASS")


if __name__ == "__main__":
    print("APMM Retrieval — local tests")
    print("=" * 40)
    test_sha256()
    record = test_cascade()
    test_validator(record)
    test_platforms()
    print("\n" + "=" * 40)
    print("✓ All tests passed.")
    print()
    print("Next:")
    print("  pip install -r requirements.txt")
    print("  playwright install chromium")
    print("  python src/main.py --image path/to/image.jpg --case-id demo_001 --demo")
