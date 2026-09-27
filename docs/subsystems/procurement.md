# Public Procurement

Find public tenders a supplier can realistically bid for, explain eligibility
and competitive fit separately, keep award and contract history as context, and
prepare a cited bid checklist and workspace. Noesis never submits a bid and
never contacts a buyer, contracting authority or procurement portal. Tracker:
[Ikey168/Noesis#1848](https://github.com/Ikey168/Noesis/issues/1848).

The pack reuses the Funding & Grants machinery rather than duplicating it. The
profile store, the three-valued eligibility engine, the ranking structure, the
workspace checklist commands and the monitoring delivery hints are all shared.
It also reuses the source-pack runtime (budgets, cursors, receipts,
watermarks), the `DocumentStore`, `ResearchProjectStore`, `AuthoredReportStore`,
`SubscriptionStore`, `EntityHistoryStore`, `canonical_entities`, `LeiStore` and
the schema registry. It adds no scheduler, permission ledger, project store,
submission engine or database.

| Concern | Module |
| --- | --- |
| Provider contracts, eForms mapping, parsers, native source-pack connectors | `src/ingestion/procurement_providers.py`, `config/source_packs/procurement.json` |
| Record contract (`noesis-procurement-record-v1`) and schema registration | `src/kb/procurement_records.py`, `contracts/schemas/jsonschema/noesis-procurement-*.json` |
| Private supplier and buyer profiles (subclass of `FundingProfileStore`) | `src/kb/procurement_profiles.py` |
| Normalisation, revisions, lots, deadlines, awards, invalidation, projector | `src/kb/procurement_notices.py` |
| Per-lot eligibility (funding rule engine) | `src/kb/procurement_eligibility.py` |
| Ranking and shortlists (`noesis-procurement-shortlist-v1`) | `src/kb/procurement_ranking.py` |
| Identity links, award history and incumbency | `src/kb/procurement_identity.py` |
| Bid workspaces (subclass of `FundingWorkspaceStore`) and drafts | `src/kb/procurement_workspaces.py` |
| Monitoring | `src/kb/procurement_monitoring.py` |
| Bundle declaration, enablement, readiness | `src/kb/procurement_bundle.py`, `packs/procurement/` |
| MCP entry points (knowledge-engine server) | `tools/knowledge_engine_mcp/procurement.py` |

## Provider audit and access decisions (P01)

`PROVIDER_CONTRACTS` records, per provider, the documentation, access method,
authentication, terms, rate limits, pagination, cadence, identifiers, schema
and CPV versions, how notices cross-reference their corrigenda and awards, the
evidence retained, coverage bounds and the fallback when access is unavailable.

| Provider | Decision | Access | Identifiers and cross-references |
| --- | --- | --- | --- |
| TED | implemented (`ted` connector) | TED API v3 `POST /v3/notices/search`, eForms business terms, iteration-token pagination, no credential for reading | publication number + `BT-701` notice id + `BT-757` version; `BT-04` procedure id is stable across PIN, contract notice, change notice (`BT-758`) and result notice. CPV 2008. eForms SDK 1.x only. |
| UK Find a Tender | implemented (`ocds` connector) | OCDS 1.1 release packages, `links.next` pagination on the same host, OGL v3 | OCID across tender, `tenderAmendment`, award, contract and `contractAmendment` releases; CPV via `items[].classification` |
| UK Contracts Finder | implemented (`ocds` connector) | OCDS search, OGL v3 | OCID; mostly below-threshold, rarely lotted |
| SAM.gov | implemented (`sam-gov` connector), credentialed | Get Opportunities v2, `api_key` from the `NOESIS_SAM_API_KEY` secret reference (never stored or logged), per-key daily quotas | `noticeId`; `solicitationNumber` links solicitation and award. No CPV: NAICS, PSC and set-aside codes are kept |
| service.bund.de | **not implemented** | Tender listings are portal pages plus an RSS feed of titles and links. There is no documented interface for notice content, and pages are not scraped. | Above-threshold federal notices come through TED |
| Berlin Vergabeplattform | **not implemented** | No documented public API or bulk export, and the portal is not scraped | Above-threshold Berlin notices come through TED |
| OpenTender | **not implemented** | Bulk historical downloads. They hold award history only and duplicate TED result notices. | Award history comes from TED and OCDS |

`LIVE_VERIFICATION` is `unverified-live` for every implemented provider and
`not-implemented` for the others. `LAST_LIVE_CHECK` records the most recent
bounded live run (P15) with each provider's failure code.

## Records (P02)

`noesis-procurement-record-v1` is one notice as published, with a stage:
`prior-information`, `contract-notice`, `corrigendum`, `cancellation`,
`award` or `modification`. The rules the validator enforces:

- **A corrigendum** names the notice it changes (`changes.changes_notice_id`).
  The store applies it as a revision of the same procedure.
- **Award history** carries awards and contracts. It has no bid deadlines and
  may never assert an active or planned procedure (`not_an_opportunity`).
- **Values** are `{amount, currency, kind, vat}`. The `kind` is `estimated`,
  `awarded`, `contract` or `framework-maximum`, and `vat` is `excluded`,
  `included` or `unknown`. Floats are rejected, amounts need an ISO currency,
  and an estimated value can never carry the awarded kind. eForms values are
  VAT-exclusive by definition. OCDS values count as net only when a gross value
  is also stated. SAM awards have no VAT basis (`unknown`).
- **Deadlines** keep `text` exactly as published, plus `timezone` and an
  `instant` only when the source states an offset. A bare date stays a date.
- **Lots** carry their own CPV classifications, estimated values and status.
  Requirements and deadlines name the lots they apply to.
- **Parties** (buyer and suppliers) keep the source name, language variants and
  stated identifiers (LEI, VAT, national, GB-COH, UEI, …). Links to
  `canonical_entities` and LEI records are identity decisions (P10) and are
  kept outside the record. An LEI that the source states is known from the
  source and is never inferred.
- **Unknowns**: absent values stay `null` and are listed in `unknowns`.
  Profile markers (`profile`, `owner`, `self_declaration`) are rejected as
  `private_leak`.

`register_schemas()` registers the record, profile and shortlist JSON Schemas
in the schema registry as modules owned by `procurement.core`. Every fixture
record validates against them (`test_procurement_records.py`).

## Profiles (P03)

`ProcurementProfileStore` subclasses `FundingProfileStore`. It keeps the same
revisions, idempotent commands, owner-reviewed and proposed facts, effective
dates and evidence, and it has no operator bypass. A *supplier* profile states
CPV interests, jurisdictions, size, turnover (money), certifications,
references, past contracts, set-aside statuses and exclusion-ground
self-declarations (`exclusion.*`, where `true` means the ground applies). A
*buyer* profile states organisation, country, sector, authority type, CPV
interests and thresholds. Facts are restricted per profile kind. Any fact that
is not stated is listed in `unknown_facts` and is never defaulted.

## Acquisition (P04–P06)

Acquisition runs through the source-pack runtime. The `procurement` source
pack declares one bounded source per provider (`ted-notices`, `uk-fts-ocds`,
`uk-contracts-finder-ocds` and `sam-opportunities`), each with licence, budget,
pinned selection and pinned offline fixture. The native adapters:

- refuse any endpoint outside the provider's host allowlist and never follow
  redirects. OCDS `links.next` must stay on the declared host;
- pin one query or parameter window and reject request parameters;
- map notices with fail-closed parsers: a shape that does not match raises
  `schema_drift` and nothing from that page is ingested;
- keep:
  - a JSON-pointer and field locator relative to the notice, so page position
    never changes a record;
  - the language tag of every multilingual text;
  - the original notice, release or opportunity object (`native_notice`,
    stored with the mapped record in the document's source-pack native JSON);
  - the raw response SHA-256 in the page receipt;
  - `execution: network|injected`.

`EFORMS_MAPPING` documents the eForms business term behind every mapped field.
Selection criteria become machine rules only when their text is unambiguous:
a minimum turnover or insurance amount in a stated currency, "at least N
references", or exactly one named certificate without "or equivalent".
Everything else stays unparsed.

## Normalisation (P07)

`ProcurementNoticeStore` keys procedures by provider and cross-stage key (the
eForms `BT-04`, the OCID or the SAM solicitation number). Each contract notice,
corrigendum or cancellation appends a revision that names the notice that
caused it and a classified change list (`deadline_change` per lot and kind,
`requirement_change`, `lot_change`, `value_change`, `status_change`,
`document_change`, `descriptive_change`). An identical notice creates no
revision, and an older notice arriving late never rolls a procedure back.
Awards and modifications become `procurement_awards` rows linked by the
cross-stage key.

Status is derived per lot: `open`, `forthcoming` (prior information),
`closed` (the submission instant has passed), `cancelled`, `awarded`,
`unconfirmed` or `unknown`, each with reasons. A procedure missing from a
complete listing becomes `unconfirmed`. A failed refresh marks the source stale
from its last successful receipt. Neither ever closes a procedure. Derived
views register the revisions they used and are invalidated by later
revisions. Awards invalidate them through an award generation.

## Eligibility (P08)

`assess_lot` runs `src.kb.funding_eligibility.assess` unchanged over a lot's
requirements, which are procedure-level exclusion grounds plus the lot's
selection criteria. Money facts are exposed per currency
(`supplier.annual_turnover.EUR`), so a GBP turnover against a EUR threshold is
`unknown`, not a comparison. Certificate names are normalised before
comparison.

Each finding carries a citation with the procedure key, notice revision,
causing notice id and stage, lot, locator, language and quote, plus the
profile facts it used. Missing facts give `unknown` and unparsed criteria give
`unparsed`, and both lead to `needs_clarification`. Only a failed hard rule
gives `ineligible`.

Assessments pin both revisions. `stale_assessments` and `reassess` find and
recompute assessments whose notice or profile revision has moved on.

## Ranking (P09)

`ShortlistService` scores one item per lot. It uses the funding ranking
structure, the funding buckets and a sensitivity table. The criteria are:

- `cpv_fit`: interest branch against lot CPV;
- `jurisdiction`: place of performance or buyer country against the profile's
  jurisdictions;
- `value_fit`: the estimated lot value against the profile's range, with the
  same currency required;
- `submission_effort`: requirements, documents, two-stage procedures and
  unparsed criteria;
- `deadline_feasibility`: days left against the preparation time, quoting the
  published deadline text;
- `incumbency_context`: derived from award history.

Each criterion states its source and reasons. The score is a weighted mean of
the criteria that are known and is **not** a probability of winning.

`apply_now` requires all of the following: the lot is open and eligible, the
deadline is feasible, and the lot is not outside the profile's CPV interests or
jurisdictions. Past awards for the same buyer or CPV branch are listed as
`award_context` with sources and dates. They never change a procedure's state.

Shortlists pin profile revision, procedure revisions, listing states and award
rows. `replay` recomputes the shortlist from those pins and compares digests.

## Identity, award history and incumbency (P10)

`ProcurementIdentityService.candidates` proposes identities for a party from
three sources: a source-stated LEI (looked up in `LeiStore` when loaded), a
`canonical_entities` alias match, or an identifier shared with an
already-linked party. Nothing is applied automatically.

`decide` records a `match` or `non-match` through
`EntityHistoryStore.decide`. It needs procurement review and entity-history
review/write scopes. A decision never merges or redirects entities. `revert`
undoes a decision with an auditable undo decision. Award history can be
queried by buyer, supplier, CPV branch or linked entity. `incumbency` is a
derived, explained fact that is limited to the acquired notices.

## Bid workspaces and drafts (P11)

`ProcurementWorkspaceStore` subclasses `FundingWorkspaceStore`. The revisioned
checklist commands, `update_items` and the stale semantics are inherited. It
creates a research project that pins the notice, profile and shortlist
revisions. The checklist has requirement items, documents to produce, the
buyer's procurement documents, milestones and gap items:

- Documents to produce cover the exclusion self-declaration, financial
  standing evidence, references, certificates and set-aside representations.
- Milestones carry the published deadline text. Internal milestones are marked
  as suggestions.

Every item cites its source (notice revision, notice id, lot, and locator or
URL). `refresh` adopts a new notice revision, keeps the preparation status and
flags changed or removed items as stale.

Outcomes are `prepared`, `user_reported_submitted` or `buyer_confirmed`, and
`buyer_confirmed` needs an evidence reference from the owner.
`ProcurementBidDraftService` writes an authored report pinned to the notice
revision: supplier facts, a compliance matrix, references, unanswered items
and missing documents.

## Monitoring (P12)

`ProcurementMonitor` is a knowledge subscription. It is evaluated at a
committed `procurement` source-pack watermark, as of that watermark's commit
time, so a replay creates no new events and no separate scheduler exists.
Before evaluating, it recomputes stale assessments. Notifications cover
`new_matching_notice`, `corrigendum`, `deadline_change` (old and new published
text), `cancellation`, `award` (context), `requirements_changed`,
`eligibility_changed` and `stale_source`. Each one cites the procedure key,
notice id and revision. The run lists stale shortlists and workspaces until
they are rebuilt or refreshed. Delivery goes through the subscription's
channel.

## Bundle and MCP (P13)

- **Manifest:** `packs/procurement/manifest.json` binds `procurement.core` (its
  own stores only), `funding.core` for the reused profile, eligibility,
  shortlist and workspace capabilities, `market.lei`, and the shared research
  project, authored report, subscription and source runtime providers. The
  funding capabilities are re-exported with `funding.core` as their provider.
  Neither bundle is the other's dependency root, so disabling either one leaves
  the other working. `CompositionView` attributes such a capability to the
  pack that ships its provider, so funding tools stay attributed to
  `funding-grants`.
- **Source pack:** `procurement` (P04–P06 sources, `LIVE_VERIFICATION` per
  source). Its projector is `noesis-procurement-record-v1` →
  `src.kb.procurement_notices`.
- **Enablement:** `set_procurement_bundle_enabled` (operator) is a coordinator
  selection change with an activation receipt once composed.
- **Tools (29):** `procurement_bundle_status`,
  `set_procurement_bundle_enabled`, `procurement_provider_contracts`,
  `list_procurement_notices`, `inspect_procurement_notice`,
  `procurement_notice_history`, `create/update/inspect/withdraw_procurement_profile`,
  `assess_procurement_eligibility`, `build/inspect/replay_procurement_shortlist`,
  `procurement_award_history`, `procurement_incumbency`,
  `procurement_party_candidates`, `decide/revert_procurement_party_link`,
  `create/inspect/refresh_procurement_workspace`,
  `update_procurement_workspace_items`, `record_procurement_workspace_outcome`,
  `draft_procurement_bid`, `export_procurement_bid_draft`,
  `create/run/poll_procurement_monitor`. Acquisition uses the existing
  `run_source_pack_execution` with pack `procurement`.

## Acceptance evidence

| Issue | Evidence |
| --- | --- |
| [#1877](https://github.com/Ikey168/Noesis/issues/1877) audit | `PROVIDER_CONTRACTS`, `EFORMS_MAPPING`, this page, `test_procurement_providers.py` |
| [#1878](https://github.com/Ikey168/Noesis/issues/1878) records | `test_procurement_records.py` (schema registry registration and fixture validation) |
| [#1879](https://github.com/Ikey168/Noesis/issues/1879) profiles | `test_procurement_profiles.py` |
| [#1880](https://github.com/Ikey168/Noesis/issues/1880)–[#1882](https://github.com/Ikey168/Noesis/issues/1882) acquisition | `test_procurement_providers.py` (runtime run, TED, OCDS, SAM, German portals) |
| [#1883](https://github.com/Ikey168/Noesis/issues/1883) normalisation | `test_procurement_notices.py` |
| [#1884](https://github.com/Ikey168/Noesis/issues/1884) eligibility | `test_procurement_eligibility.py` |
| [#1885](https://github.com/Ikey168/Noesis/issues/1885) ranking | `test_procurement_ranking.py` |
| [#1886](https://github.com/Ikey168/Noesis/issues/1886) identity | `test_procurement_identity.py` |
| [#1887](https://github.com/Ikey168/Noesis/issues/1887) workspaces | `test_procurement_workspaces.py` |
| [#1888](https://github.com/Ikey168/Noesis/issues/1888) monitoring | `test_procurement_monitoring.py` |
| [#1889](https://github.com/Ikey168/Noesis/issues/1889) bundle | `test_procurement_bundle_mcp.py`, `tests/unit/composition/test_procurement_composition.py`, regenerated catalog and shadow report |
| [#1890](https://github.com/Ikey168/Noesis/issues/1890) offline journey | `tests/unit/domains/test_procurement_acceptance.py` (sockets disabled; authored fixtures) |
| [#1891](https://github.com/Ikey168/Noesis/issues/1891) live + demo | `scripts/procurement_live_check.py`, `docs/development/procurement-evidence/` (2026-09-27: blocked), `docs/examples/procurement-shortlist-demo.md` (offline). **Live validation still needs a run from a network that can reach the providers, and a SAM.gov key.** |

Run the suites with `pytest -q tests/unit/procurement tests/unit/domains/test_procurement_acceptance.py tests/unit/composition/test_procurement_composition.py`.

## Limitations

- **Authored fixtures.** Offline evidence uses fixtures that mirror the
  documented response shapes. The exact TED v3 field layout for per-lot
  selection criteria (arrays of arrays aligned with `BT-137-Lot`) is an
  assumption, and parsers fail closed rather than guess.
- **No live validation.** No provider has been validated live yet (see P15).
- **Unparsed criteria.** Many real criteria stay unparsed, such as "or
  equivalent", staff clearances and multi-certificate alternatives. There is no
  reviewed-interpretation workflow for procurement yet.
- **No currency conversion.** Currencies are never converted, and a value range
  in another currency is unknown.
- **No CPV for SAM.gov.** SAM.gov notices carry NAICS and PSC but no CPV, so
  CPV fit and incumbency for them are unknown.
- **Partial incumbency.** Incumbency covers only the acquired award notices.
