---
name: scaffold-document-connector
description: Scaffold a new document connector for the Noesis knowledge-engine pipeline. Use when adding a new media type (DOCX, database snapshot, API dump, audio, video, web page, …) that should produce document-ingest-v1 records via the Connector base class. Covers the connector module, the Document model, and the verify loop using the pipeline + contracts + lineage MCP servers.
---

# Scaffold a Noesis document connector

The knowledge-engine pipeline ingests arbitrary media types through a single
interface: **discover → fetch → parse → `Document`**. A document connector
subclasses `Connector`, sets `source_type`, and registers itself with
`@register_connector` so the whole pipeline (and the pipeline MCP server) can
drive it without any glue code.

> **Use this skill** for any source that is *not* an RSS/Atom feed. For RSS
> feeds, use the `scaffold-connector` skill instead (it targets the simpler
> `article-ingest-v1` path).

## The interface you implement

```python
# src/ingestion/connectors/<name>/connector.py
from src.ingestion.connectors.base import Connector, RawDocument, SourceRef
from src.ingestion.connectors.registry import register_connector
from services.ingest.common.document_model import Document

@register_connector
class MyConnector(Connector):
    source_type = "web"   # must be in SOURCE_TYPES (see below)

    def discover(self, query=None) -> Iterable[SourceRef]:
        """Enumerate what to ingest. query = user-supplied paths/IDs."""

    def fetch(self, ref: SourceRef) -> RawDocument:
        """Pull the raw bytes for one SourceRef."""

    def parse(self, raw: RawDocument) -> List[Document]:
        """Normalize raw bytes → one or more Documents."""
```

`Connector.harvest(query)` chains the three for you: any source that fails
`fetch` or `parse` is skipped and logged so one bad file doesn't abort a batch.

## Valid source types

```python
# services/ingest/common/document_model.py
SOURCE_TYPES = ("news", "blog", "book", "paper", "transcript", "web", "note")
```

If you need a new type, add it to `SOURCE_TYPES` first — the `Document.__post_init__`
will raise `ValueError` otherwise.

## Minimal working connector

```python
# src/ingestion/connectors/web/connector.py
"""Web page connector → document-ingest-v1 records."""
from __future__ import annotations
import hashlib, time, urllib.request
from pathlib import Path
from typing import Any, Iterable, List, Optional

from services.ingest.common.document_model import Document
from src.ingestion.connectors.base import Connector, RawDocument, SourceRef
from src.ingestion.connectors.registry import register_connector


@register_connector
class WebConnector(Connector):
    source_type = "web"

    def discover(self, query: Optional[Any] = None) -> Iterable[SourceRef]:
        """query = a URL string or list of URL strings."""
        if query is None:
            return
        urls = [query] if isinstance(query, str) else list(query)
        for url in urls:
            yield SourceRef(locator=url, title=url)

    def fetch(self, ref: SourceRef) -> RawDocument:
        with urllib.request.urlopen(ref.locator, timeout=30) as resp:
            return RawDocument(ref=ref, content=resp.read(), content_type="text/html")

    def parse(self, raw: RawDocument) -> List[Document]:
        html = raw.content if isinstance(raw.content, str) else raw.content.decode("utf-8", errors="replace")
        # TODO: extract text from HTML (BeautifulSoup, trafilatura, …)
        text = html  # placeholder
        doc_id = "web:" + hashlib.md5(raw.ref.locator.encode()).hexdigest()[:12]
        return [Document(
            document_id=doc_id,
            source_type="web",
            language="en",
            ingested_at=int(time.time() * 1000),
            url=raw.ref.locator,
            title=raw.ref.title,
            content=text,
            content_ref=raw.ref.locator,
        )]
```

## File layout

Follow the same package structure as the existing connectors:

```
src/ingestion/connectors/<name>/
    __init__.py          # exports + registers via importing connector.py
    connector.py         # Connector subclass + @register_connector
    models.py            # internal data classes (optional but recommended)
    <format>_parser.py   # format-specific extraction (optional)
    README.md
tests/ingestion/
    test_<name>_connector.py
```

`__init__.py` must import `connector.py` so the `@register_connector` decorator
runs on package import:

```python
# src/ingestion/connectors/<name>/__init__.py
from src.ingestion.connectors.<name>.connector import MyConnector
```

Then add one line to `src/ingestion/connectors/__init__.py`:

```python
from src.ingestion.connectors import <name>  # noqa: E402,F401
```

## The Document model

```python
@dataclass
class Document:
    document_id: str        # required — stable, unique id for this doc
    source_type: str        # required — must be in SOURCE_TYPES
    language: str           # required — ISO 639-1 code, e.g. "en"
    ingested_at: int        # required — milliseconds since epoch

    source_id: Optional[str] = None   # publisher/origin id
    url: Optional[str] = None         # canonical URL or file:// URI
    title: Optional[str] = None
    content: Optional[str] = None     # inline text (small docs)
    content_ref: Optional[str] = None # URI to large content in object store
    authors: List[str] = field(default_factory=list)
    created_at: Optional[int] = None  # ms since epoch, if known
    metadata: Dict[str, Any] = field(default_factory=dict)  # anything extra
```

**Rules:**
- `content` is for inline text. Large documents (books, long transcripts) can
  put a `file://` or `s3://` URI in `content_ref` and omit or truncate `content`.
- `metadata` is your escape hatch — put type-specific fields here
  (e.g. `{"section_path": ["Chapter 3"], "start_s": 12.5}`).
- `document_id` must be stable across runs; use a hash of the canonical key
  (ISBN, arXiv ID, URL, file path + offset).

## Graceful degradation pattern

If your parser requires an optional library, try-import it inside the function
and return a safe default when missing:

```python
def parse_with_backend(data: bytes):
    try:
        import mylib  # optional dep
    except ImportError:
        return None   # caller falls through to the next backend

    return mylib.parse(data)
```

This keeps the connector importable and testable without the optional lib
installed. The three existing connectors (book, paper, transcript) all follow
this pattern.

## Verify loop

Once the connector is scaffolded, use the three MCP servers to verify it
end-to-end without writing to the warehouse:

### 1. Confirm it is registered
```
list_connector_types()
# → {"source_types": ["news", "paper", "book", "transcript", "<name>"], ...}
```

### 2. Run it against a real sample
```
run_connector("<source_type>", query='["<path-or-id>"]', sample=3)
# → {"source_type": "...", "harvested": 3, "documents": [...], "errors": []}
```

### 3. Validate the output against the document contract
```
validate("document", <record_dict>)
# → {"jsonschema": "valid", "avro": "...", "agree": true/false}
```

### 4. Check downstream impact
```
impact("ingest.<source_type>", "downstream")
# → what pipeline stages run after this connector emits
```

### Full verify checklist

1. `python3 -c "from src.ingestion.connectors import get_connector; get_connector('<name>')"` — no ImportError.
2. `list_connector_types()` → your type appears.
3. `run_connector("<name>", query='[...]', sample=3)` → `harvested > 0`, `errors == []`.
4. `validate("document", doc)` → `jsonschema.valid: true`.
5. `impact("ingest.<name>", "downstream")` → lineage flows to expected stages.

## Gotchas

- **`source_type` must be in `SOURCE_TYPES`** — `Document.__post_init__` raises
  `ValueError` on construction otherwise. Add your type there first.
- **`ingested_at` is epoch-milliseconds** (`int(time.time() * 1000)`), not a
  `datetime`. `created_at` follows the same convention.
- **`language` is required and must be a 2-letter ISO 639-1 code** (`"en"`, `"fr"`,
  `"de"`, …). Easy to forget when parsing files that don't declare a language.
- **`document_id` must be stable** — if your connector runs twice over the same
  source, the same logical document must get the same id. Use a hash of the
  canonical key, not `uuid4()`.
- **`metadata` values must be JSON-serialisable** — no `datetime` objects; use
  epoch-ms ints or ISO strings.
- **The `"news"` source_type** dispatches via the existing `NewsConnector`
  (RSS feeds). Don't create a new connector for RSS; use `scaffold-connector`.
