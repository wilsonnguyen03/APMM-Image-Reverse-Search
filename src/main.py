# src/main.py
# ─────────────────────────────────────────────────────────────────
#  APMM Web Retrieval Pipeline
#
#  Usage:
#    python src/main.py --image path/to/image.jpg --case-id case_001
#    python src/main.py --image path/to/image.jpg --case-id case_001 --demo
#    python src/main.py --image path/to/image.jpg --case-id case_001 \
#        --keywords "trump AI image deepfake" --demo
#
#  --demo      prints a formatted report for supervisor presentations
#  --keywords  space-separated relevance filter terms
# ─────────────────────────────────────────────────────────────────

import argparse
import sys
from pathlib import Path
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.tree import Tree
from rich.progress import Progress, SpinnerColumn, TextColumn

load_dotenv()

from utils import compute_sha256, make_case_dir, save_json, log
from retrieval import run_all_searches
from crawler import crawl_all_sync
from cascade import build_cascade
from validator import validate_and_exit_on_failure
from relevance_filter import apply_to_cascade_with_images
from geo_enricher import enrich_cascade

console = Console()

SOURCE_META = {
    "google_vision": ("Tier 1 — Free",  "1,000/month free"),
    "google_lens":   ("Tier 1 — Free",  "SerpAPI free tier"),
    "twitter":       ("Tier 1 — Apify", "~$0.40 per 1,000 tweets"),
    "instagram":     ("Tier 1 — Apify", "$5 free credit/month"),
    "facebook":      ("Tier 1 — Apify", "$5 free credit/month"),
    "tiktok":        ("Tier 1 — Apify", "~$0.30 per 1,000 results"),
    "reddit":        ("Tier 1 — Apify", "~$1.19 per 1,000 results"),
}


def print_demo_report(record: dict, case_id: str, image_path: str,
                      source_counts: dict, keywords: list[str]) -> None:

    console.print()
    console.print(Panel(
        f"[bold cyan]APMM Web Retrieval — Case Report[/bold cyan]\n"
        f"[dim]Case ID: {case_id}  |  Image: {Path(image_path).name}[/dim]",
        expand=False,
    ))

    # ── Keywords used ─────────────────────────────────────────────
    if keywords:
        console.print(f"\n[bold]Relevance Keywords[/bold]")
        console.print(f"  {', '.join(keywords)}")

    # ── Source coverage ───────────────────────────────────────────
    console.print("\n[bold]Source Coverage[/bold]")
    t = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    t.add_column("Source",   style="cyan",  min_width=16)
    t.add_column("Tier",     style="dim",   min_width=18)
    t.add_column("Hits",     style="green", min_width=6)
    t.add_column("Note",     min_width=22)

    for src, (tier, note) in SOURCE_META.items():
        count = source_counts.get(src, None)
        if count is not None and count > 0:
            status = f"[green]✓[/green]  {note}"
        elif count == 0:
            status = f"[dim]0 hits[/dim]"
        else:
            status = f"[dim]— not configured[/dim]"
        t.add_row(src, tier, str(count) if count is not None else "—", status)
    console.print(t)

    # ── Hit breakdown ─────────────────────────────────────────────
    items = [i for i in record["items"] if i.get("source_platform")]
    if items:
        console.print(f"\n[bold]Hits ({len(items)} total)[/bold]")
        ht = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        ht.add_column("ID",        style="dim",    min_width=10)
        ht.add_column("Platform",  style="cyan",   min_width=14)
        ht.add_column("Domain",    style="white",  min_width=26)
        ht.add_column("Relevant",  min_width=10)
        ht.add_column("Score",     style="yellow", min_width=7)
        ht.add_column("Date",      style="dim",    min_width=12)

        for item in items[:25]:
            date = str(
                item.get("timestamp") or item.get("crawl_date") or "—"
            )[:10]
            relevant = item.get("relevant", True)
            rel_str  = "[green]✓[/green]" if relevant else "[red]✗[/red]"
            score    = f"{item.get('relevance_score', 0):.2f}" if "relevance_score" in item else "—"
            ht.add_row(
                item.get("id", "?"),
                item.get("source_platform", "?"),
                (item.get("domain") or "?")[:30],
                rel_str,
                score,
                date,
            )
        console.print(ht)
        if len(items) > 25:
            console.print(f"  [dim]... and {len(items) - 25} more in cascade.json[/dim]")

    # ── Cascade tree ──────────────────────────────────────────────
    console.print(f"\n[bold]Cascade Structure (relevant items only)[/bold]")
    root_id = record.get("root_id", "")
    edges   = record.get("edges", [])
    tree    = Tree(
        f"[bold cyan]{root_id[:24]}...[/bold cyan] [dim](root)[/dim]"
    )

    children = {e[1] for e in edges if e[0] == root_id}
    for child_id in list(children)[:12]:
        item = next((i for i in record["items"] if i["id"] == child_id), {})
        platform = item.get("source_platform", "?")
        domain   = (item.get("domain") or "?")[:30]
        score    = item.get("relevance_score", "?")
        grandchildren = {e[1] for e in edges if e[0] == child_id}
        branch = tree.add(
            f"[green]{child_id}[/green]  "
            f"[dim]{platform} · {domain} · score={score}[/dim]"
        )
        for gc_id in list(grandchildren)[:3]:
            gc = next((i for i in record["items"] if i["id"] == gc_id), {})
            branch.add(
                f"[yellow]{gc_id}[/yellow]  "
                f"[dim]{gc.get('source_platform','?')} · "
                f"{(gc.get('domain') or '?')[:28]}[/dim]"
            )
    if len(children) > 12:
        tree.add(f"[dim]... {len(children) - 12} more[/dim]")
    console.print(tree)

    # ── Filter summary ────────────────────────────────────────────
    fs = record.get("filter_summary", {})

    # ── Summary panel ─────────────────────────────────────────────
    total  = len(record["items"]) - 1
    snaps  = sum(
        1 for i in record["items"]
        if i.get("snapshot_path") and Path(i["snapshot_path"]).exists()
    )
    console.print(Panel(
        f"  [bold]Items found:[/bold]      [cyan]{total}[/cyan]\n"
        f"  [bold]Relevant items:[/bold]   [green]{fs.get('relevant_items', '?')}[/green]\n"
        f"  [bold]Filtered out:[/bold]     [red]{fs.get('filtered_items', '?')}[/red]\n"
        f"  [bold]Sources queried:[/bold]  [cyan]{len(source_counts)}[/cyan]\n"
        f"  [bold]Edges:[/bold]            [cyan]{len(edges)}[/cyan]  "
        f"[dim](defensible propagation links)[/dim]\n"
        f"  [bold]Snapshots saved:[/bold]  [cyan]{snaps}[/cyan]\n"
        f"  [bold]Output:[/bold]           "
        f"[green]cases/{case_id}/cascade.json[/green]",
        title="Summary",
        expand=False,
    ))


def run(image_path: str, case_id: str, cases_base: str = "cases",
        demo: bool = False, keywords: list[str] = None,
        since: str = None, until: str = None) -> dict:

    # "+trump" marks a keyword as required: pages must contain it to pass on text alone
    keywords = keywords or []
    required = tuple(k[1:] for k in keywords if k.startswith("+") and len(k) > 1)
    keywords = [k.lstrip("+") for k in keywords]

    console.print(Panel(
        f"[bold cyan]APMM Retrieval Pipeline[/bold cyan]\n"
        f"[dim]{image_path}  →  case: {case_id}[/dim]",
        expand=False,
    ))

    with Progress(SpinnerColumn(), TextColumn("{task.description}"),
                  transient=True) as progress:

        t = progress.add_task("Step 1 — Computing SHA-256...", total=None)
        sha256 = compute_sha256(image_path)
        progress.remove_task(t)
        console.print(f"  [dim]SHA-256:[/dim] {sha256}")

        _ext = Path(image_path).suffix.lower()
        if _ext in ('.avif', '.heic', '.tiff', '.tif', '.webp'):
            from PIL import Image as _PIL
            _jpg_path = str(Path(image_path).with_suffix('')) + '_converted.jpg'
            _PIL.open(image_path).convert('RGB').save(_jpg_path, 'JPEG', quality=95)
            log.info(f"  Auto-converted {_ext} → .jpg for API compatibility")
            image_path = _jpg_path

        t = progress.add_task("Step 2 — Reverse image search...", total=None)
        hits = run_all_searches(image_path, sha256,
                                " ".join(keywords) or "misinformation",
                                since, until)
        progress.remove_task(t)

        source_counts: dict[str, int] = {}
        for hit in hits:
            src = hit.get("source_platform", "unknown")
            source_counts[src] = source_counts.get(src, 0) + 1

        console.print(
            f"  [dim]Hits:[/dim] [green]{len(hits)}[/green] unique across "
            f"[cyan]{len(source_counts)}[/cyan] source(s)"
        )

        t = progress.add_task(f"Step 3 — Crawling {len(hits)} pages...", total=None)
        case_dir = make_case_dir(cases_base, case_id)
        crawled  = crawl_all_sync(hits, case_dir)
        progress.remove_task(t)

        t = progress.add_task("Step 4 — Building cascade...", total=None)
        record = build_cascade(sha256, crawled)
        progress.remove_task(t)

        t = progress.add_task("Step 5 — Relevance filtering (OCR + HTML)...", total=None)
        record = apply_to_cascade_with_images(record, keywords, case_dir,
                                              input_image_path=image_path,
                                              required=required)
        progress.remove_task(t)

        t = progress.add_task("Step 6 — Validating...", total=None)
        validate_and_exit_on_failure(record)
        progress.remove_task(t)

        t = progress.add_task("Step 7 — Geographic enrichment...", total=None)
        record = enrich_cascade(record)
        progress.remove_task(t)

    save_json(record, case_dir / "cascade.json")

    if demo:
        print_demo_report(record, case_id, image_path, source_counts, keywords)

    return record
    # Convert unsupported formats to JPEG for Vision API



def main():
    parser = argparse.ArgumentParser(
        description="APMM Web Retrieval — cascade record from a single image."
    )
    parser.add_argument("--image",     required=True, help="Path to input image")
    parser.add_argument("--case-id",   required=True, help="Unique case ID")
    parser.add_argument("--cases-dir", default="cases", help="Output base directory")
    parser.add_argument("--demo",      action="store_true",
                        help="Print formatted report for demos")
    parser.add_argument("--keywords",  nargs="+", default=[],
                        help="Relevance keywords; prefix one with + to require it "
                             "e.g. --keywords +trump jesus AI")
    parser.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                        help="Search X/Twitter and Reddit posts from this date; "
                             "without --until, only the 14 days after it")
    parser.add_argument("--until", default=None, metavar="YYYY-MM-DD",
                        help="Search X/Twitter and Reddit posts before this date")
    args = parser.parse_args()

    if not Path(args.image).exists():
        console.print(f"[red]Image not found: {args.image}[/red]")
        sys.exit(1)

    run(args.image, args.case_id, args.cases_dir,
        demo=args.demo, keywords=args.keywords,
        since=args.since, until=args.until)


if __name__ == "__main__":
    main()
