# Consuming the knowledge bases: noesis-kb-v1

The KB contract is the one interface applications build against — the daily
brief, alerting, research assistants, and your own tools all compose the
same versioned calls. Full contract: [`contracts/noesis-kb-v1.md`](../../contracts/noesis-kb-v1.md).

## Over MCP

The `noesis-kb` server is declared in `.mcp.json` and runs standalone:

```bash
python3 tools/kb_mcp/server.py
```

Connect any MCP host (Claude Desktop, an agent, your own service) and call:

```
kb_domains()
kb_diff("economics", since="2026-07-20")        # what changed since yesterday
kb_claims("web3", limit=10)                     # clustered, cited claims
kb_contradictions("news")                       # where the record disagrees
kb_search("papers", "inflation lags")
kb_search_domains("inflation lags", domains=["economics", "papers"])
kb_answer_domains("What does research say about inflation lags?",
                  domains=["economics", "papers"])
kb_cross_links(domains=["economics", "papers"])
kb_coverage("technology")                       # is coverage thin?
```

## Over REST

The same surface, mirrored (mounted automatically by the API app):

```bash
NEURONEWS_DEV_MODE=true NOESIS_DB_PATH=/tmp/noesis-dev.duckdb \
  uvicorn src.api.app:app --port 8012

curl 'http://localhost:8012/api/v1/kb/domains'
curl 'http://localhost:8012/api/v1/kb/economics/diff?since=2026-07-20'
curl 'http://localhost:8012/api/v1/kb/web3/claims?limit=10'
```

## The rules the contract keeps

- Answer shapes are identical whichever backing serves the domain
  (corpus-view or provisioned namespace) — enforced by
  `tests/unit/kb/test_contract.py`, which runs one suite against both.
- Every analytic entry is cited and carries `prediction_mode`/confidence.
- `since` filters on ingestion time, offsets honoured, naive = UTC.
- Errors are typed: `unknown_domain` (404), `bad_request`/`bad_since` (400).
- Cross-domain calls require an explicit ordered domain list or
  `all_authorized=true`. They deduplicate shared documents, retain per-domain
  provenance, and report partial backing failures instead of hiding them.

## Feeding the domains

`make kb-bootstrap` seeds feeds, harvests, and runs the membership pass;
the consolidation passes (`src/kb/claim_links.py`, `src/kb/entities.py`,
`src/kb/clusters.py`) turn the stream into the linked knowledge these calls
serve. See [`docs/kb/starter-domains.md`](../kb/starter-domains.md).
