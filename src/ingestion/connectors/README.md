# Ingestion connectors

Connectors normalize arbitrary sources into `document-ingest-v1` `Document`
records (see `services/ingest/common/document_model.py`) behind one interface, so
the rest of the pipeline does not care whether a document came from a news feed,
a paper repository, a book file, or an audio transcript.

Part of the knowledge-engine pivot; see
`docs/architecture/knowledge-engine-pivot.md`.

## Interface

Every connector implements three steps and inherits `harvest()`:

```
discover(query) -> Iterable[SourceRef]   # what is there to ingest
fetch(ref)      -> RawDocument           # pull raw bytes/text
parse(raw)      -> List[Document]         # normalize to the contract
harvest(query)  -> Iterator[Document]     # discover -> fetch -> parse (resilient)
```

`harvest()` skips sources that fail to fetch or parse so one bad source does not
abort a run.

## Usage

```python
from src.ingestion.connectors import get_connector

for document in get_connector("news").harvest():
    ...  # document is a document-ingest-v1 record (source_type="news")
```

## Registering a connector

Subclass `Connector`, set `source_type`, and register it. Importing the
`connectors` package registers the built-ins.

```python
from src.ingestion.connectors.base import Connector
from src.ingestion.connectors.registry import register_connector

@register_connector
class MyConnector(Connector):
    source_type = "paper"
    def discover(self, query=None): ...
    def fetch(self, ref): ...
    def parse(self, raw): ...
```

## Built-in connectors

| source_type | Module | Notes |
| --- | --- | --- |
| `news` | `news.py` | Wraps the existing RSS/Atom ingest (`scrapy_integration`). Sentiment, which the legacy ingester computes inline, is exposed as an enrichment via `NewsConnector.enrichments_for`, not baked into the core `Document`. |
| `blog` | `blog/` | Atom/RSS subscriptions with readable-body extraction and per-source health. |
| `paper` | `paper/` | arXiv metadata/PDFs and citation references. |
| `book` | `book/` | EPUB/PDF chapter documents with structural locators. |
| `transcript` | `media/` | Audio/video transcription with timestamp evidence locators. |
| `note` | `upload/` | Local PDF, DOCX, mail, HTML, Markdown, text, and pasted notes. |
| `filings` (`note`) | `filings_connector.py` | Regulatory filing documents. |
| `legislative` | `legislative.py` | JSON, JSONL, or CSV roll-call records; configured with `NOESIS_LEGISLATIVE_SOURCES`. |

The legislative connector maps each vote to a cited `note` document whose
metadata retains actor, bill, topic, date, normalized position, external id,
and record URL. Argument mining records it like any other source; the
position-vs-record checker can then return supported/contradicted/unverifiable.

## Dataset connectors (statistical evidence)

`dataset/` is a parallel connector family for **statistical series**, not
documents. Statistical series (World Bank, FRED, Eurostat, ...) are versioned
numeric evidence for checking quantitative claims, so they carry their own
contract (`dataset-series-v1`) and emit `SeriesRecord`s rather than
`Document`s. The interface mirrors this one (`discover` → `fetch` → `parse` →
`harvest`) via `DatasetConnector`, and `ObservationStore` persists series into
DuckDB with vintage-keyed observations. See
[EVIDENCE_DATASETS_PLAN.md](../../../docs/architecture/EVIDENCE_DATASETS_PLAN.md)
and the beyond-text roadmap (#765).
