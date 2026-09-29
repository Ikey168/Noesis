# Corporate Ownership and Registries

Given an explicit company identifier (LEI, register number, CIK, or name plus
country), assemble a source-cited ownership and control picture: legal-entity
records, consolidation parents and control assertions, officers, filings,
corporate events and a timeline, with conflicts, reporting exceptions and
unknowns visible. Tracker:
[Ikey168/Noesis#1846](https://github.com/Ikey168/Noesis/issues/1846).

**Boundaries.** Ownership assertions are what a source states. No
beneficial-ownership, sanctions or AML determination is inferred or stored
(`determination`, `sanctions`, `aml` and `risk_score` fields are rejected by
the record validator). Identity matches are reviewable, reversible decisions,
never automatic merges. Conflicting assertions coexist. Unknowns stay unknown.
Registers whose terms or access forbid automated retrieval are
`not-implemented` with a reason; there is no scraper.

The bundle reuses existing owners: the source-pack runtime (cursors, budgets,
retries, run receipts, document evidence), `src/kb/lei.py` (market.lei) for
LEI Level 1 and Level 2 data, `src/kb/entities.py` (`canonical_entities`) and
`src/kb/entity_history.py` (identity decisions), the market instrument master
(CIK to LEI), market corporate actions, temporal assertions, the schema
registry and `AuthoredReportStore` for dossier export. It adds no scheduler,
permission ledger, project store, entity store or database.

| Concern | Module |
| --- | --- |
| Provider contracts, identifiers, live state, parsers and runtime adapters | `src/ingestion/ownership_providers.py` |
| Record contract (`noesis-ownership-record-v1`) and schema registration | `src/kb/ownership_records.py`, `contracts/schemas/jsonschema/noesis-ownership-{record,part,dossier}-v1.json` |
| Revisioned store, runtime projector, GLEIF projection, register documents | `src/kb/ownership_store.py` |
| Identity candidates and reviews | `src/kb/ownership_identity.py` |
| Graph queries (parents, subsidiaries, chains, conflicts, replay) | `src/kb/ownership_graph.py` |
| Timeline and as-of state | `src/kb/ownership_timeline.py` |
| Dossier assembly and export | `src/kb/ownership_dossier.py` |
| Bundle declaration, acquisition, enablement, readiness | `src/kb/ownership_bundle.py` |
| MCP entry points (knowledge-engine server) | `tools/knowledge_engine_mcp/ownership.py` |
| Source pack | `config/source_packs/corporate-ownership.json` |
| Composition | `packs/corporate-ownership/manifest.json`, `packs/corporate-ownership/providers/ownership.core.json`, `packs/platform/providers/platform.entity-identity.json` |

Extensions to existing owners: `LeiStore.level2()` (`src/kb/lei.py`) exposes
the stored LEI record and Level 2 rows for projection, and
`register_canonical_entity()` (`src/kb/entities.py`) registers a canonical
entity under an identifier-based id without writing any name alias, so two
companies that share a name never converge.

## Source audit and access decisions (O01)

| Provider | Status | Access | Auth | Rate limits | Pagination | Cadence | Retained evidence | Identifiers |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| GLEIF Level 2 | implemented (existing `gleif` connector into market.lei) | API v1 JSON:API: record, direct/ultimate parent relationship, direct/ultimate reporting exception | none | fair use; HTTP 429 honoured | one part of one LEI per page | ≤ daily | response digest per part; LEI revisions | LEI; registration authority + registered-as number |
| UK Companies House | implemented (`companies-house`) | public data API: profile, officers, PSC, PSC statements, exemptions, filing history | API key (HTTP Basic), `NOESIS_COMPANIES_HOUSE_API_KEY` | documented 600 requests / 5 min; live requests spaced; 429 honoured | `items_per_page`/`start_index` within the page budget | ≤ daily | each JSON response retained as the runtime document with SHA-256 | company number, officer id, PSC id |
| SEC EDGAR | implemented (`sec-edgar-ownership`) | data.sec.gov submissions and companyfacts; selected Schedule 13D/13G primary documents on www.sec.gov | declared User-Agent contact, `NOESIS_SEC_CONTACT` | fair access: ≤ 10 requests/s; live requests spaced | submissions `recent` block, bounded by `max_filings` | ≤ daily | JSON and XML responses with SHA-256 | CIK, ticker (as listed), LEI when present |
| Open Ownership BODS | implemented (`bods`) | selected BODS 0.2/0.3 statement files (JSON array or JSON Lines) | none | byte and page budget | statement offset per file | per dataset release, ≤ weekly | the page's statements with the file SHA-256 | statementID, GB-COH, XI-LEI, other org-id schemes |
| OpenCorporates | reused (#1483 regional provider) | existing provider | API token | per plan | — | — | existing documents | jurisdiction + company number |
| Handelsregister | not implemented | interactive portal of the federal states | — | portal terms restrict automated and bulk retrieval | — | — | — | register court + HRA/HRB number |
| Unternehmensregister | not implemented | interactive portal | account for some documents | no API | — | — | — | — |
| BRIS | not implemented | e-Justice portal search | — | no public third-party API | — | — | — | — |

OpenCorporates is documented as an **aggregator**: its records are enrichment
and identity evidence, never an authoritative substitute for a national
register. `IDENTIFIER_OVERLAP` records where LEIs, register numbers, CIKs and
tickers meet `src/kb/lei.py`, the OpenCorporates links and the market
instrument master. The unavailable-access fallback is explicit per provider:
a 404 is "none reported", a failed run leaves earlier revisions current and the
run receipt records the failure code.

## Records (O02)

`noesis-ownership-record-v1` kinds: `legal_entity`, `person`, `registration`,
`officer_role`, `ownership_assertion`, `corporate_event`, `filing_reference`.
Assertion kinds keep consolidation parents (`direct_parent`,
`ultimate_parent`), control (`shareholding`, `voting_rights`,
`appoint_directors`, `significant_influence`, `other_control`) and
`reporting_exception` distinct. Percentages are an exact decimal string or a
band with explicit inclusive/exclusive bounds; consolidation parents carry no
percentage. Validity is `from`/`to` with `stated`/`open`/`unknown` status;
partial dates stay partial. `unknowns` is recomputed on every validation.

Store rules (C01.2): one authoritative store (`ownership_records` plus
immutable `ownership_record_revisions`), a stable `record_id` per namespace and
source `record_key`, a new revision only when the source-stated content
changes, and every revision keeps its run and observation time. Reads can be
pinned to exact revisions or to a record time. Conflicting assertions from
different sources have different record keys and coexist. The three contracts
are registered as modules in the schema registry
(`ownership_records.register_schemas`), and fixture records validate through
`SchemaRegistry.validate_instance`.

Person statements whose source terms require it (BODS person statements) are
owner-scoped: stored per acquiring principal, redacted for everyone else unless
they hold `knowledge:ownership:review`, and the shared `canonical_entities`
table never learns their names.

## Acquisition (O03–O07)

All acquisition runs through `SourcePackRuntime` with the
`corporate-ownership` source pack (`ownership_bundle.acquire`). GLEIF parts are
projected into `src/kb/lei.py` by the existing LEI projector and then into
ownership assertions by `OwnershipStore.project_gleif` with the LEI record
revision as source; Level 1 records are not copied. Companies House, EDGAR
and BODS pages are projected by `OwnershipProjector`
(`noesis-ownership-part-v1`). PSC natures of control map to assertion kinds
with the original nature text preserved and bands kept as bands. Schedule
13D/13G XML cover pages become `shareholding` assertions with the filing as
source; anything unparsed (HTML filings, Forms 3/4/5) stays a filing
reference. BODS coverage is counted per publisher and source; statements from
sources not listed as implemented and BODS 0.4 records are counted and
skipped. For Handelsregister, Unternehmensregister and BRIS a user-obtained
official document can be recorded as a registration with its document
reference and digest (`record_register_document`); nothing is fetched.

## Identity, graph, timeline, dossier (O08–O10)

`OwnershipIdentityService.propose` offers candidates with basis, evidence and
confidence: `exact-identifier` (0.95), `cross-referenced-identifier` (0.8; the
GLEIF registration pointer, the market instrument master's CIK to LEI mapping,
OpenCorporates links) and `name-jurisdiction` (0.35). Accept and reject are
`match`/`non-match` decisions in `entity_history`; revert appends an `undo`.
Records are never rewritten; graph queries group records only through
accepted, unreverted candidates. Successors and predecessors are corporate
events, never identity candidates.

`OwnershipGraph` answers direct parents, ultimate parents, subsidiaries,
control chain and successor chain as of a date. Each edge carries its source,
kind, validity, as-of status (`valid`, `undetermined`, `not_started`,
`ended`) and control basis. An entity holder sits in the parent slot only when
the source states consolidation or control, or a stated share whose lower
bound is above half. Smaller stated holdings and person holders are returned
separately, never hidden. Different holders for one slot are a conflict,
returned together with `different_source` / `different_date` /
`different_kind` reasons and never ranked. Depth is bounded (at most 10),
cycles are reported, and every result pins record revisions and accepted
identity candidates so that `replay` reproduces it exactly.

`timeline` merges registrations, officer changes, ownership starts and ends,
corporate events, filings, market corporate actions and temporal assertions.
Each entry carries its source, event time and record time. Entries without a
date are listed as undated and never interpolated. `state_as_of` reconstructs
registration, officers and ownership at a date from the revisions known at a
record time. `build_dossier` assembles all of this for one identifier. A name
lookup returns candidates and never picks one. `export_dossier` saves an
authored report in which every sourced statement cites its record revision.

## Composition (O11)

`packs/corporate-ownership/manifest.json` binds `ownership.core` plus
`market.lei` (`market.legal-entities`), `platform.entity-identity`,
`platform.source-runtime` and `platform.authored-reports`. The resolver
produces one authority per store; the shadow report's disagreements for the
bundle are annotated by the reviewed rules in `src/composition/shadow.py`.
Once cut over, `set_ownership_bundle_enabled` is a coordinator selection change
with an activation receipt; disabling keeps every shared provider serving
other bundles.

## Evidence

Offline: authored fixtures in `tests/fixtures/ownership` (see its README),
unit tests in `tests/unit/ownership/`, and the acceptance suite
`tests/unit/domains/test_corporate_ownership_acceptance.py`. Live:
`scripts/ownership_live_check.py` and `docs/development/ownership-evidence/`.
The 2026-09-27 run verified no provider: GLEIF failed with `source_unavailable`
(egress proxy refused the connection), Companies House and SEC stopped at
`credential_missing`, and BODS was not attempted. `LIVE_VERIFICATION` says
`unverified-live` for all four. Demo (offline, fictional companies):
`docs/examples/ownership-dossier-demo.md`.
