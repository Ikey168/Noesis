# Corporate Ownership and Registries pack scope

Status: implemented offline, 2026-09-27 (see `docs/subsystems/corporate-ownership.md`). Live coverage is not verified. It never infers beneficial ownership, never makes sanctions or AML determinations and never merges entities automatically.

## Delivery state (audited 2026-09-27)

- Delivered: O01–O12. The code is in `src/kb/ownership_*.py` and `src/ingestion/ownership_providers.py`, with the source pack in `config/source_packs/corporate-ownership.json`, unit tests in `tests/unit/ownership/` and the offline acceptance suite in `tests/unit/domains/test_corporate_ownership_acceptance.py`. Existing owners are extended rather than duplicated: `src/kb/lei.py` (`LeiStore.level2`), `src/kb/entities.py` (`register_canonical_entity`), with identity decisions recorded through `src/kb/entity_history.py`.
- Composition: composed. `packs/corporate-ownership/manifest.json` binds `ownership.core` plus `market.lei`, `platform.entity-identity`, `platform.source-runtime` and `platform.authored-reports`. `set_ownership_bundle_enabled` is a coordinator selection change once the bundle is cut over.
- O13 is partial. The live-check harness exists and ran on 2026-09-27; the report is in `docs/development/ownership-evidence/`. No provider was reachable: GLEIF failed with `source_unavailable` because the egress proxy refused the connection, Companies House and SEC stopped at `credential_missing`, and BODS was not attempted. `LIVE_VERIFICATION` stays `unverified-live`. The demo dossier (`docs/examples/ownership-dossier-demo.md`) uses offline authored fixtures about fictional companies, not a live public company.

Tracking: [#1846](https://github.com/Ikey168/Noesis/issues/1846).

## Outcome

Given an explicit company identifier (LEI, register number, or name plus
jurisdiction), assemble a source-cited ownership and control picture:
legal-entity records, parent/child and control relationships, officers,
filings, corporate events and conflicting assertions. Every relationship keeps
its source, jurisdiction, validity interval and reporting exceptions.

## Implementation issues

- [x] [#1850](https://github.com/Ikey168/Noesis/issues/1850) — O01 Audit registry and ownership source contracts and select bounded coverage.
- [x] [#1851](https://github.com/Ikey168/Noesis/issues/1851) — O02 Define legal-entity, ownership-assertion, officer, filing and corporate-event records.
- [x] [#1852](https://github.com/Ikey168/Noesis/issues/1852) — O03 Acquire GLEIF Level 2 relationship and reporting-exception records.
- [x] [#1853](https://github.com/Ikey168/Noesis/issues/1853) — O04 Acquire UK Companies House profiles, officers, PSC and filing history.
- [x] [#1854](https://github.com/Ikey168/Noesis/issues/1854) — O05 Acquire SEC EDGAR submissions and beneficial-ownership filings as filing references.
- [x] [#1855](https://github.com/Ikey168/Noesis/issues/1855) — O06 Acquire Open Ownership BODS statements with per-publisher coverage.
- [x] [#1856](https://github.com/Ikey168/Noesis/issues/1856) — O07 Record national register access decisions for Handelsregister, Unternehmensregister and BRIS.
- [x] [#1857](https://github.com/Ikey168/Noesis/issues/1857) — O08 Reconcile entity identity across LEI, register numbers and OpenCorporates with reviewable matches.
- [x] [#1858](https://github.com/Ikey168/Noesis/issues/1858) — O09 Build ownership-graph queries for parents, subsidiaries, successors and conflicts as of a date.
- [x] [#1859](https://github.com/Ikey168/Noesis/issues/1859) — O10 Build a corporate-event and filing timeline with as-of selection.
- [x] [#1860](https://github.com/Ikey168/Noesis/issues/1860) — O11 Compose the Corporate Ownership bundle over market.lei, entity and source providers.
- [x] [#1861](https://github.com/Ikey168/Noesis/issues/1861) — O12 Add offline identifier-to-ownership-dossier acceptance coverage.
- [ ] [#1862](https://github.com/Ikey168/Noesis/issues/1862) — O13 Validate live registry coverage and publish an explained ownership-dossier demo (harness, dated blocked run and offline demo delivered; live coverage not validated).

Each issue contains acceptance criteria and prerequisite issue references.

## Sources

- [GLEIF API](https://www.gleif.org/en/lei-data/gleif-api): Level 1 records, Level 2 relationships and reporting exceptions.
- [UK Companies House](https://developer.company-information.service.gov.uk/): profiles, officers, PSC, PSC statements, exemptions and filing history.
- [SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces): submissions, company facts and selected Schedule 13D/13G documents.
- [Open Ownership BODS](https://www.openownership.org/en/topics/beneficial-ownership-data-standard/): entity, person and ownership-or-control statements.
- [OpenCorporates](https://api.opencorporates.com/documentation/API-Reference): reused as an aggregator through the existing regional provider.
- Handelsregister, Unternehmensregister and BRIS have documented access decisions and are not implemented.

These links identify candidate sources. They do not guarantee APIs or complete
live coverage. Each provider's supported access is recorded in
`PROVIDER_CONTRACTS` and verified live per provider.

## Composition and reuse

Follow the [pack/workflow architecture](../architecture/pack-workflow-composition.md)
and the [composition migration record](../architecture/composition-migration.md).
Reuse the source-pack runtime, `market.lei`, canonical entities and entity
identity decisions, source identity, the market instrument master and
corporate actions, temporal assertions, the schema registry and authored
reports. Do not introduce another scheduler, permission ledger, project store,
entity store or database.

## Acceptance

- A reproducible journey takes a company identifier to an explained ownership
  dossier: entities, relationships with sources and validity, officers, filings
  and a corporate-event timeline, with conflicts and unknowns visible.
- Identity matches are reviewable and reversible.
- Offline and bounded live evidence are reported separately.

Beneficial-ownership determination, sanctions screening, AML scoring and
scraping of registers whose terms or access forbid it are outside this scope.
