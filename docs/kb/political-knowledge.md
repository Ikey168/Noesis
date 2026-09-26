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

## Legislative dossiers

`LegislativeDossierStore` builds a saved, revisioned procedure dossier from
committed `document_revision_records`. Inputs are exact `document_id` and
`revision_id` pairs from the acquired Bundestag DIP or EUR-Lex documents.
The caller needs `knowledge:political:dossier:write` or `:read`, the matching
namespace scope, and `document:<id>:read` for every pinned record. Source
access is checked again for every read and export.

This offline example starts with the supplied Bundestag fixture. It is a
format and workflow example, not a live DIP response or legal finding:

```python
import duckdb

from src.domains.political.legislative_dossiers import LegislativeDossierStore
from src.ingestion.connectors.political_official import PoliticalOfficialConnector
from src.ingestion.document_store import DocumentStore

conn = duckdb.connect(":memory:")
documents = list(PoliticalOfficialConnector().harvest({
    "offline": True, "source_ids": ["de-bundestag-dip"],
}))
DocumentStore(conn).upsert(documents)
document_id = documents[0].document_id
revision_id = conn.execute(
    "SELECT revision_id FROM document_current_revisions WHERE document_id=?",
    [document_id],
).fetchone()[0]
scopes = {
    "knowledge:political:dossier:read",
    "knowledge:political:dossier:write",
    "namespace:research:read", "namespace:research:write",
    f"document:{document_id}:read",
}
store = LegislativeDossierStore(conn)
first = store.save(
    "research", "clean-heating", "DE", "proposal:de:bt-test-42",
    [{"document_id": document_id, "revision_id": revision_id}],
    principal_id="alice", scopes=scopes,
)
timeline = store.timeline(
    "research", first["dossier_id"], principal_id="alice", scopes=scopes,
)
```

After acquiring another explicitly linked record, call `save` with the same
request key and dossier ID, both pinned references, and
`expected_revision=first["revision"]`. `compare` and
`export_change_summary` classify added, changed, and unavailable stages and
include exact before/after citations. `dependencies` returns source
dependencies accepted by authored reports, evidence links accepted by research
projects, and evaluations through the existing evidence-change resolver.
Citation subscriptions remain attached to the resulting report or project.

The Knowledge Engine MCP exposes `save_legislative_dossier`,
`inspect_legislative_dossier`, `legislative_dossier_timeline`,
`compare_legislative_dossier`, `export_legislative_dossier_changes`, and
`legislative_dossier_dependencies`. Reads are paginated at 100 entries and a
saved revision accepts at most 100 source records.

Timeline entries keep event, publication, and observation times separate. An
observation cutoff hides documents acquired later even if their event dates
are older. Missing stages are explicit; uncertain procedure links remain
review candidates. Commencement, partial commencement, amendment, and repeal
are shown only when captured source fields or text-supported legal events
state them. Conflicting dates remain separate cited assertions. The
EUR-Lex fixture has an instrument identifier but no legislative procedure
identifier, so its dossier is explicitly marked `instrument_anchor_only`.
National and EU dossiers stay separate. A source can add a
`related_procedures` relationship only with a verbatim quote present in its
pinned text; the cited relationship appears on the stage without merging the
procedures.
`legal_effect_state` and `interpretation` fields deliberately leave legal
conclusions to review. The included provider fixtures are fictional, and no
live-provider accuracy or independent human legal review is implied.
