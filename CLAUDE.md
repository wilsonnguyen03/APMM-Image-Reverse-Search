# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

APMM Web Retrieval Module, part of the TruthLens capstone. Given one input image, it finds where the image appears on the web and writes a `cascade.json` (a propagation tree) that the main TruthLens repo consumes in `src/propagation/graph_builder.py` (PyG/BiGCN). That downstream repo is not part of this checkout.

## Commands

Run from this directory (`APMM-Image-Reverse-Search/`). The scripts are not installed as a package.

```
pip install -r requirements.txt
pip install imagehash            # needed by image_matcher.py, but not listed in requirements.txt
playwright install chromium
python test_local.py             # offline sanity tests (no API keys or network); the README says all 5 should pass
python src/main.py --image path/to/img.jpg --case-id case_001 --keywords trump AI deepfake --demo
docker-compose run apmm_retrieval --image /app/cases/test.jpg --case-id test_001   # Docker entrypoint is already `python src/main.py`
```

- `test_local.py` is a plain script, not pytest. It adds `src/` to `sys.path`, and the modules in `src/` use flat imports (`from utils import ...`). Run them from a context where `src/` is on the path.
- To run a single test, call one `test_*` function from `test_local.py`, for example `python -c "import test_local as t; t.test_cascade()"`.
- There is no linter or formatter configuration.
- Tesseract OCR must be installed separately. `relevance_filter._configure_tesseract` auto-detects the Windows default path.
- `.env` keys: `SERPAPI_KEY`, `IMGBB_API_KEY` (Lens), `GOOGLE_VISION_KEY`, `TWITTER_BEARER_TOKEN`, `APIFY_TOKEN`. A source with no key is silently skipped. The README mentions `.env.example`, but that file is not in the repo.

## Architecture

`main.run()` in [src/main.py](src/main.py) is a linear 7-step pipeline. Each step is a separate module, and the record is passed along as plain dicts:

1. `utils.compute_sha256` gives the root ID, `sha256:<hash>`.
   - `.avif`, `.heic`, `.tiff` and `.webp` inputs are first converted to a `*_converted.jpg` next to the original, and that path is used from then on.
2. `retrieval.run_all_searches` runs the sources in order: Google Vision, Google Lens (SerpAPI, with the image hosted on imgbb), Twitter, Apify Instagram, Apify Facebook. It de-duplicates by URL and caps the total at `MAX_TOTAL_HITS`.
   - The social searches take a text query (default `"misinformation"`), not the image, so they are not true reverse image search.
3. `crawler.crawl_all_sync` is an async Playwright crawl. It writes `snapshots/item_NNN/{page.html,screenshot.png}` under the case dir and extracts dates.
4. `cascade.build_cascade` builds the record.
   - The root item is the input image.
   - Every hit defaults to being a child of the root.
   - Edges are upgraded to parent→child only where there is a defensible link: consecutive timestamp-ordered items on the same domain.
   - Keep edges conservative.
   - `content_score`, `crossmodal_score` and `provenance_score` are `BANNED_FIELDS`. This module must never set them, because the downstream detection pipeline owns them.
5. `relevance_filter.apply_to_cascade_with_images` scores each page from keyword hits in HTML text, OCR of the screenshot, title, URL and domain signals, plus a pHash comparison from `image_matcher`. It adds `relevant`, `relevance_score`, `image_match*` and `filter_summary`.
6. `validator.validate_and_exit_on_failure` checks the record against the schema `graph_builder.py` expects. It calls `sys.exit` on failure, so the record is never written if it is invalid.
7. `geo_enricher.enrich_cascade` adds geo data per item. It uses a static domain table, then a TLD fallback, then the ip-api lookup.

The record is saved to `cases/<case-id>/cascade.json`. `--demo` only adds the rich-formatted terminal report. `visualiser.html` is a standalone viewer for the output.

## Gotchas

- `main.SOURCE_META` and the README list Zenserp and Yandex as sources, but `retrieval.py` has neither. Treat `run_all_searches` as the source of truth.
- Reddit and 4chan were evaluated and rejected, per the README. `cascade.py` still carries `board` and `subreddit` fields and the tests use 4chan fixtures.
- Paywalled pages, Instagram and YouTube yield little text, so the relevance filter relies on image matching and URL signals for them.
