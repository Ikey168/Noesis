# Political knowledge

The `political` pack adds typed political entities and relations to Noesis'
canonical knowledge model. It does not create a separate graph: people,
institutions, proposals, and instruments remain normal temporal entities;
political object and relation types are explicit ontology extensions.

The official-source catalog is `config/political_sources.json`. Its four
representative manifests cover executive, regulatory, electoral, and
parliamentary material. Each records jurisdiction, issuing institution,
document and identifier types, reuse terms, update cadence, a canonical HTTPS
URL, an offline fixture, and a live-fetch policy. The connector rejects unknown
document types, manifest/payload mismatches, malformed times, and non-HTTPS
record locators.

Offline ingestion is the default:

```python
from src.ingestion.connectors.political_official import PoliticalOfficialConnector

connector = PoliticalOfficialConnector()
documents = list(connector.harvest({"offline": True}))
```

Live reachability checks require both `NOESIS_POLITICAL_LIVE=1` and
`live.enabled=true` for the specific manifest. They are intentionally absent
from offline CI. Production adapters must transform upstream formats into the
strict `political-records-v1` input before parsing.

`kb_political` and `POST /api/v1/kb/political` support `officeholder_at_date`,
`proposal_lifecycle`, `vote_records`, `institutional_positions`, and
`policy_changes`.

Every request requires a jurisdiction. Responses expose valid-time and
observation-time cutoffs, domain and official-source coverage, cited document
locators, evidence-origin independence, and an explicit supported, partial, or
unsupported uncertainty state. Private domains use the same grant gate as
other KB operations via `/political/private` or MCP `principal_id` plus
`include_private=true`.
