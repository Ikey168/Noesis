# Funding & Grants

Find funding an applicant can realistically apply for, explain eligibility and
fit, and prepare a source-backed application plan — without ever submitting an
application or contacting a funder. Tracker:
[Ikey168/Noesis#1761](https://github.com/Ikey168/Noesis/issues/1761).

The bundle reuses existing owners throughout: `DurableHTTP` and the
`DocumentStore`/`SnapshotStore` for acquisition, namespace/owner scopes for
access, `ResearchProjectStore` for workspaces, `AuthoredReportStore` for
drafts, `QuantitativeStore` calculation receipts for budget arithmetic, and
`SubscriptionStore` for monitoring. It adds no scheduler, permission ledger,
project store or submission engine.

| Concern | Module |
| --- | --- |
| Provider contracts, parsers, bounded client, evidence store | `src/ingestion/funding_providers.py` |
| Record contract (`noesis-funding-record-v1`) | `src/kb/funding_records.py`, `contracts/schemas/jsonschema/noesis-funding-record-v1.json` |
| Private profiles | `src/kb/funding_profiles.py` |
| Normalization, revisions, deadlines, invalidation | `src/kb/funding_opportunities.py` |
| Eligibility and reviewed interpretations | `src/kb/funding_eligibility.py` |
| Ranking and shortlists (`noesis-funding-shortlist-v1`) | `src/kb/funding_ranking.py` |
| Workspaces, checklists and drafts | `src/kb/funding_workspaces.py` |
| Monitoring | `src/kb/funding_monitoring.py` |
| Bundle declaration, enablement, readiness | `src/kb/funding_bundle.py` |
| MCP entry points (knowledge-engine server) | `tools/knowledge_engine_mcp/funding.py` |

## Provider audit and access decisions (F01)

None of the four sources offers a documented, versioned open-call API that the
bundle can rely on. Each provider therefore has an explicit, bounded access
contract (`PROVIDER_CONTRACTS`); nothing implies comprehensive coverage.

| Provider | Access used | Auth | Pagination | Cadence | What counts as programme / call / award | Retained evidence |
| --- | --- | --- | --- | --- | --- | --- |
| NLnet | Public HTML: `/propose/` and fund pages | none | none | ≤ daily | fund page / fund + announced deadline round / project pages (not acquired) | HTML snapshot, paragraph quote locators |
| EU Funding & Tenders | Portal search JSON (`api.tech.ec.europa.eu/search-api`, public `SEDIA` key passed as a credential slot) and `topicDetails/<id>.json` | public portal key | `pageNumber`/`pageSize`, bounded by `max_pages` | ≤ daily | framework programme / topic (round = call identifier) / CORDIS (not acquired) | JSON snapshot, JSON pointers |
| Förderdatenbank | Public HTML programme pages and result lists | none | result pages only when selected | ≤ weekly | directory entry / not asserted / not acquired | HTML snapshot, definition-list locators |
| EXIST | Public HTML programme pages | none | none | ≤ weekly | programme variant / dated rounds when stated / not acquired | HTML snapshot, quote locators, guideline links |

Decisions and fallbacks:

- The EU portal endpoints are what the public portal itself uses; they are not a
  versioned public API contract. Procurement tenders (`type=0`) are counted and
  split out, never ingested as grant opportunities.
- A Förderdatenbank listing is a *directory entry*. It links to the
  administering body (for example EXIST) and never asserts an open window.
- On any unavailable source, a failure receipt is stored, the provider is marked
  stale, and the last known revision stays visible with a staleness reason. A
  failed or partial refresh never closes a call.
- **Live verification is outstanding.** From the environment this was built in,
  the organization's egress policy denied all five hosts; see
  `docs/development/funding-evidence/live-check-2026-09-25.json`. Every provider's
  `LIVE_VERIFICATION` status stays `unverified` until
  `scripts/funding_live_check.py` succeeds from a network that permits them.

## Records (F02)

`noesis-funding-record-v1` separates `programme`, `call`, `directory_entry` and
`award`. Awards and directory entries cannot carry open status or deadlines.
Instruments distinguish `grant`, `stipend`, `prize` (cash-equivalent) from `loan`,
`guarantee`, `equity`, `credit`, `in-kind` and `procurement`. Money is a decimal
string with an explicit currency and *basis*: `award_range` is `per-project`,
`per-applicant` or `per-person-month`; `programme_budget` is `call-total`,
`topic-total` or `programme-total` and is never used as an award size. Funding
rate, co-financing, eligible costs, reimbursement timing and repayability are
optional and `null` means unknown. `unknowns` is recomputed on validation.
Requirements need exact locators. Keys that look like private applicant state are
rejected. The existing `config/extraction_schemas/eu-funding.json` models award
metadata; `award_from_extraction` maps it to `award` records only.

## Profiles (F03)

Facts are declared per section (`applicant`, `project`, `preferences`) with
validators; free-form facts use `custom.*`. Each fact has effective dates,
evidence references and a review state. Owner-stated facts are
`owner-reviewed`; imported or extracted facts are `proposed` and stay unknown to
eligibility until the owner accepts them. Every change is a revision behind a
command key. Only the owner can read a profile — `operator` scope included —
and only with current funding and namespace scopes; a withdrawn profile makes
its history and every assessment, shortlist, workspace and draft pinned to it
unreadable.

## Normalization and deadlines (F08)

Opportunities are keyed by provider, record kind, native ID and round, so rounds
never merge. Provider assertions are kept per listing (search listing versus
detail page); the detail page wins by authority/detail rank, and field-level
disagreements are retained as `conflicts`. Identical re-acquisition updates
only `last_seen`. Changes append a revision with classified amendments
(`deadline_shift`, `status_change`, `rule_change`, `terms_change`,
`documents_change`) and invalidate every registered derived view (assessments,
shortlists, workspaces) that pinned an older revision.

Status is derived conservatively. A call is `closed` only when the provider
says so or the final submission instant has passed. A call missing from a
*complete* listing becomes `unconfirmed`. Deadlines keep original text; an
instant is only produced when the source states a timezone or offset. A date
without a timezone is not converted into an instant.

## Eligibility (F09)

Hard requirements are evaluated with three-valued logic over `all`/`any`/`not`
and `rule`+`except` nodes. Each finding cites the opportunity revision, source
URL and locator, and lists the facts used and missing. Verdicts are
`eligible`, `ineligible` (a hard requirement fails) or `needs_clarification`
(unknown facts, unparsed text, unclear hardness, conflicting requirement
assertions, or a record that is not an application call). Procedural
requirements (documents, submission route, duration) go to the checklist,
not the verdict. Free-text requirements can gain a machine rule only through a
versioned interpretation, approved by a reviewer other than its author, and
bound to the exact requirement text. Every result states that it is a
requirements assessment, not a funder decision.

## Ranking (F10)

Criteria: topic fit, usable funding, deadline feasibility, application effort,
cash flow/co-financing and obligations, each with reasons, a weight and an
explicit *unknown* state. The match score is a weighted mean over known
criteria, reported with its coverage, and is not a probability. Loans, equity,
guarantees and credits score zero usable cash. Buckets are rule-based:
`apply_now` requires an eligible, open/rolling call with a feasible deadline;
ineligible, closed and non-opportunity records can never be `apply_now`.
Sensitivity reports how the order changes when each criterion is dropped or
doubled.

## Workspaces and drafts (F11, F12)

A workspace is a research project with pinned `funding_opportunity`,
`funding_profile` and `funding_shortlist` links (new link kinds requiring a
revision), plus a checklist of requirements, documents, deadlines and gaps
(partner, affiliation, clarification). `refresh` adopts a new call revision
while keeping item status and marking changed items stale. Outcomes separate
`prepared`, `user_reported_submitted` and `provider_confirmed`, which requires
an owner-supplied evidence reference.

Drafts are authored reports. Sourced statements depend on owner-reviewed
profile facts, call requirements or a budget calculation receipt; every gap is
an explicit `UNANSWERED` commentary item. Budget lines are checked against
eligible costs, currency (no conversion) and matching funds, and the totals are
recorded as a replayable quantitative calculation and verified again. Owner
edits are new report revisions; regenerating creates a separate report instead
of overwriting edits. Exports re-check current access to the workspace and profile.

## Monitoring (F13)

A monitor is a knowledge subscription whose result set is the profile's view of
each opportunity. Runs happen at committed watermarks, so event IDs are
replay-safe and replays add nothing. Notifications cover new matching calls,
rule changes, profile-driven eligibility changes, deadline shifts, closures and
stale sources, with a `deliver_not_before` computed in the user's timezone.
Failed refreshes degrade coverage and mark items uncertain. Affected workspaces
are listed for reassessment. Delivery uses the subscription's configured channel
and existing outbox.

## Bundle and MCP (F14)

The bundle is composed under the
[pack/workflow composition contracts](../architecture/pack-workflow-composition.md)
(C09.4, [#1843](https://github.com/Ikey168/Noesis/issues/1843)):

| Piece | Where |
| --- | --- |
| Manifest | `packs/funding-grants/pack.json` (v1 runtime fields) and `composition.json` (requirements, entry points) |
| Funding-owned provider | `noesis.funding` (`config/composition/providers/funding.json`): the funding tools, split into read, change and acquisition capabilities, owning the `funding_*` tables |
| Shared providers it binds | `noesis.research-projects`, `noesis.reports`, `noesis.quantitative`, `noesis.subscriptions`, `noesis.documents` |
| Declaration kept for callers | `BUNDLE` in `src/kb/funding_bundle.py` |

The manifest resolves against the shipped candidates to a plan that binds only
those shared providers plus `noesis.funding`. No shared provider owns a
`funding_*` table, so applicant facts stay owner-scoped.

`funding_bundle_status` reports each provider and entry point as `ready` (a
network observation is current), `fixture-only` or `unavailable`, from the
provider evidence as before. It adds a `composition` section read from the C04
readiness assessment (the coordinator's, once the bundle is cut over): a
coordinator-owned blocker on a bound shared provider (disabled, shut down,
failed, account limit exhausted) makes the entry points that use it
`unavailable`. The entry point → capability map is `metadata.entry_points` in
`composition.json`.

`set_funding_bundle_enabled` (operator) has one authority at a time:

- after cutover (`scripts/migrate_composition.py` or `Coordinator.cutover`), it
  is a composition selection change: enabling selects and activates the
  bundle, disabling removes the root through the coordinator. The selection is
  deployment-wide; the `namespace` argument is kept for compatibility and the
  result says `"scope": "deployment"`;
- before cutover, or with `NOESIS_COMPOSITION_LIFECYCLE=legacy`, the
  per-namespace `funding_bundle_state` table is the legacy authority.

The two are never written for the same change. Disabling gates only the
funding tools; shared providers, research projects and other workflows keep
working. A dispatched profile → shortlist → workspace workflow template is
not implemented.

## Acceptance evidence

| Issue | Evidence |
| --- | --- |
| [#1762](https://github.com/Ikey168/Noesis/issues/1762) audit | `PROVIDER_CONTRACTS`, this page, `test_funding_providers.py::test_every_provider_has_an_explicit_access_contract_without_invented_apis` |
| [#1763](https://github.com/Ikey168/Noesis/issues/1763) records | `test_funding_records.py` |
| [#1764](https://github.com/Ikey168/Noesis/issues/1764) profiles | `test_funding_profiles.py` |
| [#1765](https://github.com/Ikey168/Noesis/issues/1765)–[#1768](https://github.com/Ikey168/Noesis/issues/1768) acquisition | `test_funding_providers.py` |
| [#1769](https://github.com/Ikey168/Noesis/issues/1769) normalization | `test_funding_opportunities.py` |
| [#1770](https://github.com/Ikey168/Noesis/issues/1770) eligibility | `test_funding_eligibility.py` |
| [#1771](https://github.com/Ikey168/Noesis/issues/1771) ranking | `test_funding_ranking.py` |
| [#1772](https://github.com/Ikey168/Noesis/issues/1772), [#1773](https://github.com/Ikey168/Noesis/issues/1773) preparation | `test_funding_workspaces.py` |
| [#1774](https://github.com/Ikey168/Noesis/issues/1774) monitoring | `test_funding_monitoring.py` |
| [#1775](https://github.com/Ikey168/Noesis/issues/1775) bundle | `test_funding_bundle_mcp.py`, `test_funding_composition.py` (manifest resolves to shared providers plus `noesis.funding`; one enablement authority; disabling leaves shared providers working), regenerated MCP catalog |
| [#1776](https://github.com/Ikey168/Noesis/issues/1776) offline journey | `test_funding_acceptance.py` (network disabled; authored fixtures) |
| [#1777](https://github.com/Ikey168/Noesis/issues/1777) live + demo | `scripts/funding_live_check.py`, `docs/development/funding-evidence/`, `docs/examples/funding-shortlist-demo.md` (offline). **Live validation still needs a run from a network that can reach the providers.** |

Run the suite with `pytest -q tests/unit/funding`.

## Limitations

- Offline evidence uses authored fixtures that mirror provider page shapes; real
  pages may differ, and parsers fail closed (`schema_drift`) rather than guess.
- Requirement extraction is pattern-based and conservative. Most free-text
  conditions (consortium composition, European dimension, applicant lists)
  stay unparsed until a reviewed interpretation exists.
- Stipend ranges are used as an upper bound; per-status rates are not modelled.
- No currency conversion is performed anywhere.
