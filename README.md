# APMM Web Retrieval Module

Part of the **TruthLens** capstone project. Takes an input image and produces a structured cascade record showing every publicly findable instance of that image across the web, with a propagation tree suitable for downstream BiGCN analysis.

---

## What It Does

1. Hashes the input image (SHA-256) — this becomes the root ID
2. Runs reverse image search via Google Lens (SerpAPI) to find where the image appears online
3. Crawls each hit — saves the full HTML and a screenshot as evidence
4. Builds a cascade record: a JSON tree linking parent→child instances where a defensible propagation link exists
5. Runs a relevance filter on each crawled page using:
   - HTML text extraction (BeautifulSoup)
   - OCR on the screenshot (Tesseract)
   - Visual perceptual hash comparison (pHash) against the input image
6. Validates the cascade record against the schema expected by `graph_builder.py`

---

## Setup

### 1. Install Python dependencies

```
pip install -r requirements.txt
pip install imagehash
```

### 2. Install Tesseract (for OCR)

Download the Windows installer from: https://github.com/UB-Mannheim/tesseract/wiki

Default install path (`C:\Program Files\Tesseract-OCR\tesseract.exe`) is detected automatically.

### 3. Install Playwright browser

```
playwright install chromium
```

### 4. Configure API keys

Copy `.env.example` to `.env` and fill in your keys:

```
SERPAPI_KEY=your_serpapi_key
IMGBB_API_KEY=your_imgbb_key
```

- **SerpAPI** — free trial at serpapi.com (100 searches). Powers Google Lens reverse image search.
- **imgbb** — free at imgbb.com/api. Used to host the input image temporarily so SerpAPI can access it.

### 5. Verify setup

```
python test_local.py
```

All 5 tests should pass before running the real pipeline.

---

## Usage

```
python src/main.py --image path/to/image.jpg --case-id case_001 --keywords trump AI deepfake --demo
```

| Argument | Required | Description |
|---|---|---|
| `--image` | Yes | Path to the input image (JPG, PNG, AVIF, etc.) |
| `--case-id` | Yes | Unique identifier for this run (e.g. `case_001`) |
| `--keywords` | No | Space-separated terms for text relevance filtering |
| `--demo` | No | Print a formatted report in the terminal |
| `--cases-dir` | No | Base output directory (default: `cases/`) |

### Example

```
python src/main.py --image "cases\trump_ai.jpg" --case-id trump_001 --keywords trump AI jesus image generated --demo
```

---

## Output Files

Every run produces a case folder at `cases/<case-id>/`:

```
cases/
└── case_001/
    ├── cascade.json          ← main output — feeds into graph_builder.py
    └── snapshots/
        ├── item_000/
        │   ├── screenshot.png   ← full-page screenshot of the hit
        │   └── page.html        ← raw HTML of the hit
        ├── item_001/
        │   ├── screenshot.png
        │   └── page.html
        └── ...
```

### cascade.json schema

```json
{
  "root_id": "sha256:<hash>",
  "items": [
    {
      "id": "sha256:<hash>",
      "url": null,
      "image_sha256": "<hash>",
      ...
    },
    {
      "id": "item_000",
      "url": "https://...",
      "domain": "nbcnews.com",
      "publisher": "NBC News",
      "timestamp": "2026-04-13T...",
      "matched_image_url": "https://...",
      "match_type": "exact",
      "snapshot_path": "cases/case_001/snapshots/item_000",
      "source_platform": "google_lens",
      "relevant": true,
      "relevance_score": 0.93,
      "relevance_reasons": ["keyword 'trump' found 4x", "keyword 'AI' found 2x"],
      "image_match": false,
      "image_match_type": "no_match",
      "image_match_distance": 26,
      "images_checked": 6
    }
  ],
  "edges": [
    ["sha256:<hash>", "item_000"],
    ["sha256:<hash>", "item_002"],
    ["item_002", "item_003"]
  ],
  "filter_summary": {
    "keywords": ["trump", "AI", "deepfake"],
    "threshold": 0.15,
    "total_items": 10,
    "relevant_items": 8,
    "filtered_items": 2,
    "image_matched": 2
  }
}
```

**Fields never set by this module** (filled downstream by the detection pipeline):
- `content_score`
- `crossmodal_score`
- `provenance_score`

---

## File Structure

```
apmm_retrieval/
├── .env                    ← API keys (never commit)
├── .gitignore
├── requirements.txt
├── test_local.py           ← run this first to verify setup
│
└── src/
    ├── main.py             ← pipeline entrypoint
    ├── retrieval.py        ← reverse image search (Google Lens)
    ├── crawler.py          ← Playwright page crawling + snapshots
    ├── cascade.py          ← builds the cascade JSON
    ├── validator.py        ← validates cascade before output
    ├── relevance_filter.py ← text relevance scoring (HTML + OCR)
    ├── image_matcher.py    ← perceptual hash image comparison
    └── utils.py            ← SHA-256, logging, path helpers
```

---

## Source Coverage and Limitations

### Current — Google Lens (SerpAPI)

The only active search source. Returns pages across the open web where the image appears — news sites, blogs, social media posts that are publicly indexed.

**Free trial:** 100 searches. After that, ~$50/month for 5,000 searches.

### Sources Evaluated and Rejected

| Source | Reason rejected |
|---|---|
| **4chan** | No reverse image matching — returns random recent posts regardless of image content. Results were completely unrelated to input images. |
| **Reddit** | Now requires moderator status to access the API. Not available for research use without special approval. |
| **Zenserp** | Google Image Search by filename/keyword rather than true reverse image search. Results were poor quality and largely unrelated. |

### Gaps — Social Media Propagation

The biggest limitation of the current pipeline is social media coverage. Images often spread primarily through closed or semi-closed platforms before appearing in indexed news articles.

| Platform | Status | Cost to fix |
|---|---|---|
| **Twitter / X** | API configured but not active — token not set | $100/month (Basic API) |
| **Facebook / Instagram** | Crawled via Playwright (public posts only) | Meta Content Library requires academic approval |
| **TikTok** | Not implemented | Research API requires application and approval |
| **WhatsApp / Telegram** | Not possible | No public API for image search |

To enable Twitter/X search: obtain a Bearer Token from developer.twitter.com and add `TWITTER_BEARER_TOKEN=your_token` to `.env`. The `search_twitter()` function in `retrieval.py` is already implemented and will activate automatically.

### Relevance Filtering Limitations

- Paywalled pages (Reuters, Washington Post) often return near-zero text — filtered out even when relevant
- Instagram and YouTube extract minimal text — scoring relies on image matching and URL signals
- OCR on screenshot adds coverage but Tesseract struggles with complex page layouts

---

## Downstream Integration

The `cascade.json` output is consumed by `graph_builder.py` at `/src/propagation/graph_builder.py` in the main TruthLens repo. That file converts the cascade record into a PyG Data object for BiGCN.

Validation check before handing off:
```python
from src.propagation.graph_builder import build_graph
record = json.load(open("cases/case_001/cascade.json"))
data = build_graph(record)
assert data is not None
assert record["root_id"] in [item["id"] for item in record["items"]]
```

---

## Licences

All dependencies use Apache 2.0, MIT, or BSD licences, in compliance with the AEA Ignite grant constraint.
