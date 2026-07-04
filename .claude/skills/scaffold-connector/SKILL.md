---
name: scaffold-connector
description: Scaffold a new data connector / news source for the Noesis ingestion pipeline. Use when adding an RSS feed or a new source that produces article-ingest-v1 records. Covers the connector module, the contract it must satisfy, and the verify loop using the pipeline + contracts + lineage MCP servers. For non-RSS media types (books, audio, papers, web pages) use scaffold-document-connector instead.
---

# Scaffold a Noesis connector

> **Choosing the right skill:** Use this skill for RSS/Atom feeds and sources
> that emit `article-ingest-v1` records. For non-RSS media types (EPUB/PDF
> books, audio/video, academic papers, web pages, database dumps) use the
> **`scaffold-document-connector`** skill instead — it targets the newer
> `document-ingest-v1` / `Connector` base class.

A connector fetches from a source and emits **article-ingest-v1** records — the
governed boundary the rest of the pipeline consumes. The cleanest shape emits
instances of the generated contract model
(`services/generated/generated/jsonschema/article_ingest_v1_models.py`,
class `ArticleingestV1`), so the output is contract-shaped by construction.

This skill pairs with the three MCP servers (in `.mcp.json`):
- **`neuronews-pipeline`** `run_connector` — run an RSS source against a live sample
- **`neuronews-contracts`** `validate("ingest", …)` — does the output satisfy the contract
- **`neuronews-lineage`** `impact` — what's downstream once it emits

**Paths below are relative to the repo root** (`/home/Ikey/NeuroNews`).

## The fast path: a new RSS source

If the source is an RSS/Atom feed, the existing pipeline already handles
fetch/parse/sentiment/store — just add a `Feed(...)` to `DEFAULT_FEEDS` in
`src/ingestion/scrapy_integration.py`:

```python
Feed("My Source", "https://example.com/rss.xml", "Technology"),
```

Then run it live via the pipeline MCP tool `run_connector("My Source", sample=3)`
(or `list_sources()` to confirm it's registered). That's the whole job for an
RSS source feeding the warehouse pipeline.

## The full path: a connector that emits the contract

For a non-RSS source, or to feed the **contract boundary** (article-ingest-v1),
scaffold a connector module under `connectors/`:

```python
# connectors/<name>_connector.py
"""<Name> connector → article-ingest-v1 records."""
from __future__ import annotations
import json, sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.ingestion.scrapy_integration import Feed, _http_get, parse_feed
from services.generated.generated.jsonschema.article_ingest_v1_models import ArticleingestV1

SOURCE = Feed("My Source", "https://example.com/rss.xml", "Technology")
SOURCE_ID = "my-source"
LANGUAGE = "en"

def fetch(sample: int = 5):
    # Replace with your source's fetch (HTTP API, HTML scrape, …). For RSS,
    # reuse the pipeline's parser:
    return parse_feed(_http_get(SOURCE.url), SOURCE, limit=sample)

def to_contract(item) -> ArticleingestV1:
    return ArticleingestV1(
        article_id=item.id,
        source_id=SOURCE_ID,
        url=item.url,
        title=item.title,
        body=item.content,
        language=LANGUAGE,
        published_at=item.publish_date,
        ingested_at=datetime.now(timezone.utc),
        sentiment_score=item.sentiment_score,
        topics=[item.category] if item.category else [],
    )

def emit(sample: int = 5) -> List[dict]:
    # ISO-timestamp JSON dicts, ready for validate(). .json() serializes the
    # datetime fields to ISO-8601 strings (what the JSON Schema expects).
    return [json.loads(to_contract(i).json()) for i in fetch(sample)]

if __name__ == "__main__":
    print(json.dumps(emit(2), indent=2))
```

Run it to see live output:

```bash
python3 connectors/<name>_connector.py
```

## Verify against the contract (the governed boundary)

Feed the connector's output to the contracts MCP `validate("ingest", record)`.
Verified end-to-end with a real Ars Technica connector — the emitted record
returns:

```
jsonschema: valid (drift clean)
avro:       invalid  -> published_at/ingested_at "expected integer, got string"
agree:      false
```

That `agree: false` is **not your connector's bug** — it's the known
`article-ingest-v1` divergence: the JSON Schema types the timestamps as
`string` (date-time, what `ArticleingestV1` emits) while the Avro schema types
them as `long` (epoch-millis). Your connector is correct against the JSON Schema
contract; the two contracts need reconciling (see `tools/contract_mcp/README.md`).
If you target the Avro/Kafka path instead, emit epoch-millis for the two
timestamps and the verdicts flip.

## Gotchas

- **The warehouse `Article` shape ≠ the `article-ingest-v1` contract.** The
  pipeline's `Article` (`id, content, publish_date, source, …`) is the DuckDB
  row; the contract uses `article_id, body, published_at, source_id, language,
  ingested_at, …`. `to_contract()` is where you bridge them — don't assume the
  field names line up.
- **Emit via `.json()`, not `.dict()`.** `ArticleingestV1.dict()` leaves
  `published_at`/`ingested_at` as `datetime` objects, which fail JSON Schema
  validation (it wants strings). `json.loads(model.json())` gives ISO strings.
- **The model forbids extra fields** (`extra = "forbid"`). An unknown kwarg to
  `ArticleingestV1(...)` raises — good, it catches drift at construction.
- **`language` is required** and must be a 2-letter ISO-639-1 code; `country`
  (optional) must be 2-letter uppercase. Easy to miss — they're not in the
  warehouse `Article`.

## Verify checklist

1. `python3 connectors/<name>_connector.py` → prints contract-shaped JSON.
2. contracts MCP `validate("ingest", <record>)` → `jsonschema.valid: true`.
3. (RSS) add the `Feed` to `DEFAULT_FEEDS`; pipeline MCP
   `run_connector("<name>")` → live fetch summary.
4. lineage MCP `impact("warehouse.news_articles", "downstream")` → confirm where
   it lands once the pipeline runs.
