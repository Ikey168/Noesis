# System architecture

Noesis is a **headless knowledge engine**: it ingests documents from many
source types, mines evidence and arguments out of them, stores everything in a
local DuckDB warehouse, and exposes the results through two machine-facing
surfaces — **16 MCP tool servers** and a **FastAPI REST API**. There is no
bundled UI; consumers are agent hosts (Claude Desktop, custom agents) and
other services.

## The system at a glance

```mermaid
flowchart LR
    subgraph Sources
        RSS["News RSS/Atom feeds"]
        BLOG[Blogs]
        DOCS["Papers · books · transcripts"]
        DATA["Datasets · EDGAR · polls · legislative"]
    end

    subgraph Ingestion["Ingestion (src/ingestion)"]
        CONN["Connector framework<br/>discover → fetch → parse"]
        ADAPT["Adaptive layer<br/>retry · health · extraction cascade"]
        CONTRACTS["Data contracts<br/>(contracts/schemas)"]
    end

    WH[("DuckDB warehouse<br/>articles · documents · observations<br/>revisions · snapshots")]

    subgraph Analysis["Analysis subsystems (src/)"]
        AM["Argument mining<br/>claims · stances · frames"]
        KG["Knowledge graph"]
        STATS["Statistics<br/>claim-vs-data checks"]
        OSINT["OSINT<br/>corroboration · contradiction"]
        RAG["RAG retrieval"]
    end

    subgraph Exposure["Capability plane"]
        MCP["16 MCP servers<br/>(tools/*_mcp)"]
        API["REST API<br/>(src/api)"]
    end

    subgraph Consumers
        HOSTS[Agent hosts]
        SVCS[Other services]
    end

    Sources --> CONN --> CONTRACTS --> WH
    CONN <--> ADAPT
    WH --> Analysis
    Analysis --> WH
    WH --> MCP & API
    MCP --> HOSTS
    API --> SVCS
```

Key properties, enforced in code rather than by convention:

- **Read-only exposure.** MCP tools open the warehouse read-only and degrade
  to an empty-but-valid payload when a table is missing — they can never
  collide with a writer.
- **Statistical honesty.** Analytic tools return `n` / `method` /
  `assumptions` and an interval on any headline figure
  (`src/analytics/honesty.py`).
- **Evidence discipline.** OSINT outputs cite every rendered line; uncited
  items are flagged, never hidden (`src/osint/evidence.py`).
- **Three-valued verdicts.** Claim checks answer `supported` /
  `contradicted` / `unverifiable` — a claim with no matching data is
  *unverifiable*, never a guess.
- **Review gate.** Sensitive OSINT tools stay off unless
  `NOESIS_OSINT_GATED_TOOLS` is set ([osint-review-gate](../osint-review-gate.md)).

## Ingestion pipeline

Every source type implements the same connector contract
(`src/ingestion/connectors/base.py`), so the pipeline downstream of a
connector is uniform:

```mermaid
flowchart LR
    Q[query / watchlist] --> D[discover]
    D -->|SourceRef| F["fetch"]
    F -->|RawDocument| P[parse]
    P -->|"Document(s)"| V{"contract valid?"}
    V -- yes --> S[(warehouse)]
    V -- no --> R["rejected + logged"]
    S --> SNAP["snapshot / revision<br/>tracking"]
    SNAP -.->|staged re-fetch 1d/7d/30d| F
```

- Connectors exist for news feeds, blogs, papers, books, audio/video
  transcripts (with keyframe OCR), datasets, EDGAR filings, polls, and
  legislative sources; each registers via `@register_connector`.
- Fetched pages are snapshotted and revision-tracked
  (`src/ingestion/snapshots.py`, `corrections.py`); a staged re-fetch
  scheduler (`refetch.py`) revisits documents at 1d/7d/30d to catch silent
  edits and takedowns.
- The adaptive layer (health tracking, extraction fallback, escalation) is
  documented separately in [adaptive-scraping.md](adaptive-scraping.md).

## Capability plane: MCP + REST

Each subsystem ships as a standalone FastMCP server under
`tools/<name>_mcp/server.py`, declared in `.mcp.json`. Transport is decided
per process by `src/mcp_host/transport.py`:

```mermaid
flowchart TB
    subgraph Host["Noesis host"]
        subgraph Servers["tools/*_mcp (16 servers)"]
            S1[statistics] ~~~ S2[osint] ~~~ S3[kg] ~~~ S4[pipeline] ~~~ S5[…]
        end
        WH[(DuckDB<br/>read-only)]
        Servers --> WH
    end

    LOCAL[Local agent host] -->|"stdio (default)"| Servers
    REMOTE[Remote consumer] -->|"Streamable HTTP<br/>+ Bearer token (fail-closed)"| Servers
    SVC[Service integration] -->|REST| API["FastAPI (src/api)"]
    API --> WH
```

- **stdio is the default** — a consuming project spawns the server process.
- **HTTP is opt-in** via `NOESIS_MCP_TRANSPORT=http`, one port per server;
  setting `NOESIS_MCP_AUTH_TOKEN` attaches a Bearer verifier and the server
  *refuses to start* if the installed fastmcp cannot enforce it (fail-closed).
- The REST API mirrors the same subsystems for non-MCP consumers; routes are
  feature-flag imported so a missing optional dependency degrades gracefully
  (`src/api/app.py`).

Connection examples and the full server table live in
[integrate-via-mcp.md](../integrate-via-mcp.md).

## Worked flow: checking a quantitative claim

The statistics plane shows how the disciplines compose end to end:

```mermaid
sequenceDiagram
    participant C as Consumer (agent)
    participant M as neuronews-statistics (MCP)
    participant X as QuantityExtractor
    participant W as DuckDB (read-only)

    C->>M: check "Unemployment in Germany rose in 2024"
    M->>X: extract quantity assertion
    X-->>M: {metric, geo, period, direction}
    M->>W: resolve series (vintage-keyed observations)
    alt series found
        W-->>M: observations for period
        M-->>C: verdict supported/contradicted + n, method,<br/>assumptions, interval (honesty envelope)
    else no matching series
        M-->>C: verdict unverifiable (never a guess)
    end
```

Observations are stored **vintage-keyed** (`series_id, period, as_of`), so a
check can be replayed against the data as it existed at any point in time.

## Where things live

| Layer | Code |
|---|---|
| Connectors + adaptive ingestion | `src/ingestion/` |
| Scraper engines (Scrapy + async/Playwright) | `src/scraper/` |
| Analysis subsystems | `src/argument_mining/`, `src/knowledge_graph/`, `src/analytics/`, `src/osint/`, `src/nlp/` |
| Warehouse access | `src/database/` |
| MCP servers | `tools/*_mcp/`, `.mcp.json`, `src/mcp_host/` |
| REST API | `src/api/` |
| Data contracts | `contracts/schemas/` |
| Agent host (provisioning + osint planes) | `src/agent/` |
