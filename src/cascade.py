# src/cascade.py
# Builds the cascade record from crawled hits.
# Edges are conservative — only drawn where defensible.
# Never touches content_score, crossmodal_score, provenance_score.

from utils import log

BANNED_FIELDS = {"content_score", "crossmodal_score", "provenance_score"}


def _clean(item: dict) -> dict:
    cleaned = {k: v for k, v in item.items() if k not in BANNED_FIELDS}
    if len(cleaned) != len(item):
        log.warning(f"Stripped banned fields from '{item.get('id')}'")
    return cleaned


def _timestamp_edges(items: list[dict]) -> list[tuple[str, str]]:
    """
    Find defensible parent->child edges using timestamp ordering
    within the same domain. Only chains consecutive items.
    """
    edges = []
    by_domain: dict[str, list[dict]] = {}
    for item in items:
        d = item.get("domain", "")
        if d:
            by_domain.setdefault(d, []).append(item)

    for domain, group in by_domain.items():
        timestamped = [i for i in group if i.get("timestamp")]
        if len(timestamped) < 2:
            continue
        try:
            ordered = sorted(timestamped, key=lambda x: x["timestamp"])
            for i in range(len(ordered) - 1):
                p, c = ordered[i], ordered[i + 1]
                log.info(f"  Edge: {p['id']} → {c['id']} ({domain})")
                edges.append((p["id"], c["id"]))
        except Exception:
            pass

    return edges


def build_cascade(root_sha256: str, crawled_items: list[dict]) -> dict:
    root_id = f"sha256:{root_sha256}"

    items = [{
        "id": root_id, "url": None, "domain": None,
        "publisher": None, "timestamp": None,
        "matched_image_url": None, "match_type": None,
        "snapshot_path": None, "image_sha256": root_sha256,
    }]

    for hit in crawled_items:
        items.append(_clean({
            "id":                hit.get("id"),
            "url":               hit.get("url"),
            "domain":            hit.get("domain"),
            "publisher":         hit.get("publisher"),
            "timestamp":         hit.get("timestamp"),
            "matched_image_url": hit.get("matched_image_url"),
            "match_type":        hit.get("match_type"),
            "snapshot_path":     hit.get("snapshot_path"),
            "image_sha256":      root_sha256,
            "source_platform":   hit.get("source_platform"),
            "page_title":        hit.get("page_title"),
            "crawl_date":        hit.get("crawl_date"),
            "post_text":         hit.get("post_text"),
            "board":             hit.get("board"),
            "subreddit":         hit.get("subreddit"),
        }))

    hit_items = [i for i in items if i["id"] != root_id]

    # Default: all items attach to root
    edges: list[list[str]] = [[root_id, i["id"]] for i in hit_items]

    # Upgrade where defensible timestamp edges exist
    explicit = _timestamp_edges(hit_items)
    if explicit:
        has_real_parent = {c for _, c in explicit}
        edges = [e for e in edges if e[1] not in has_real_parent]
        edges.extend([list(e) for e in explicit])

    log.info(f"Cascade: {len(items)} items, {len(edges)} edges")
    return {"root_id": root_id, "items": items, "edges": edges}
