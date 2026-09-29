# Sanctions designations and trade-control lists expansion scope

Status: planned, 2026-09-27. An expansion of the existing Legal bundle; it
does not create a separate domain or pack. It produces no screening verdict,
no sanctions or AML compliance determination and no legal advice, and it never
infers that a similar name is the same party.

## Delivery state (audited 2026-09-27)

- Shipped: nothing yet. Planning issues #1910–#1998 are open.
- Composition dependency: a `legal.sanctions` provider descriptor and optional
  `sanctions` feature in the Legal bundle (`packs/legal/`), composed with
  `legal.works`, `ownership.identity`, `ownership.records`,
  `market.legal-entities`, `platform.entity-identity`, `economics.knowledge`,
  `platform.subscriptions` and `platform.source-acquisition`.

Tracking: [#1907](https://github.com/Ikey168/Noesis/issues/1907).

## Outcome

Given a party (person, entity, vessel) or a controlled-goods category,
assemble what each list stated about it as of a date: designations with their
listing revisions, the programme and legal basis linked to the CELLAR act,
identifier aliases, delistings, and control-list entries by edition. Lists
stay separate; a match across lists is a reviewable assertion, and unknowns
stay unknown.

## GitHub implementation issues

- [ ] [#1910](https://github.com/Ikey168/Noesis/issues/1910) — S01 Audit sanctions, export-control and trade-statistics source contracts and select bounded provider coverage.
- [ ] [#1915](https://github.com/Ikey168/Noesis/issues/1915) — S02 Define designation, listing-revision, programme, legal-basis, identifier-alias and control-list-entry records with revisions.
- [ ] [#1920](https://github.com/Ikey168/Noesis/issues/1920) — S03 Acquire the EU consolidated financial sanctions list and link designations to their CELLAR legal acts.
- [ ] [#1927](https://github.com/Ikey168/Noesis/issues/1927) — S04 Acquire UN, OFAC and UK consolidated lists with per-list revision history and identifier aliases.
- [ ] [#1936](https://github.com/Ikey168/Noesis/issues/1936) — S05 Acquire EU dual-use control-list editions as legal works with as-of version selection.
- [ ] [#1943](https://github.com/Ikey168/Noesis/issues/1943) — S06 Acquire Comext trade flows for controlled-goods categories through the existing SDMX connector with vintages.
- [ ] [#1954](https://github.com/Ikey168/Noesis/issues/1954) — S07 Match designated parties to corporate and entity identity through reviewable, non-destructive decisions.
- [ ] [#1963](https://github.com/Ikey168/Noesis/issues/1963) — S08 Answer what a list stated about a party as of a date with cited listing revisions and legal basis.
- [ ] [#1970](https://github.com/Ikey168/Noesis/issues/1970) — S09 Monitor new designations, delistings, amendments and control-list editions through subscriptions.
- [ ] [#1980](https://github.com/Ikey168/Noesis/issues/1980) — S10 Register the `legal.sanctions` provider descriptor and optional feature in the Legal bundle.
- [ ] [#1989](https://github.com/Ikey168/Noesis/issues/1989) — S11 Add offline party-to-listing-history acceptance coverage.
- [ ] [#1998](https://github.com/Ikey168/Noesis/issues/1998) — S12 Validate live list coverage and publish a cited designation-history demo.

Order: S01 → S02 → {S03, S04, S05, S06} → S07 → S08 → S09 → S10 → S11 → S12.

## Sources

- [EU consolidated financial sanctions list](https://data.europa.eu/data/datasets/consolidated-list-of-persons-groups-and-entities-subject-to-eu-financial-sanctions):
  designations, aliases and the regulation reference behind each entry.
- [EU Sanctions Map](https://www.sanctionsmap.eu/): programme and legal-act
  overview; `not-implemented` unless S01 verifies a documented
  machine-readable interface.
- [UN Security Council Consolidated List](https://main.un.org/securitycouncil/en/content/un-sc-consolidated-list):
  permanent reference numbers, listing dates and narrative summaries.
- [OFAC Sanctions List Service](https://ofac.treasury.gov/sanctions-list-service):
  SDN and consolidated non-SDN entries with UIDs and programme tags.
- [UK Sanctions List](https://www.gov.uk/government/publications/the-uk-sanctions-list):
  unique IDs, regimes and last-updated dates.
- [Regulation (EU) 2021/821](https://eur-lex.europa.eu/eli/reg/2021/821):
  the dual-use control list, acquired as CELLAR works and annex editions.
- [Eurostat Comext](https://ec.europa.eu/eurostat/web/international-trade-in-goods/database):
  trade flows for controlled-goods categories through the SDMX connector.

S01 verifies the documented access method, terms, identifiers, publication
cadence and revision semantics per provider. These links establish source
candidates, not guaranteed APIs or complete live coverage.

## Expansion of existing capabilities

**Legal (`legal.works`):** store designations, listing revisions, programmes,
legal-basis links, identifier aliases, delistings and control-list entries as
their own revision-addressable records in `src/kb/sanctions.py`, under the
`legal-research` source pack (`config/source_packs/legal.json`). A legal basis
resolves to an existing `legal_works`/`legal_expressions` row through the
CELLAR path in `src/ingestion/legal_sources.py`; cited passages come from
`get_legal_passages`, and dual-use editions use `select_legal_version_as_of`
and `compare_legal_versions` unchanged. Current force is never inferred.

**Corporate Ownership and legal entities (`ownership.identity`,
`ownership.records`, `market.legal-entities`):** a designated party is linked
to a corporate record, an LEI (`src/kb/lei.py`) or an ownership record only
through the existing reviewable candidate state machine in
`src/kb/ownership_identity.py`. Accepting a match links records without
rewriting either side, and reverting restores the prior state. Name similarity
proposes a candidate at most; it never produces an accepted match.

**Entity identity (`platform.entity-identity`):** decisions are recorded
through `src/kb/entity_history.py` against `canonical_entities`
(`src/kb/entities.py`). List records keep their per-list identity; no shared
party key is created at ingestion, and cross-list matches are the same kind
of reviewable assertion.

**Economics (`economics.knowledge`):** Comext flows for selected controlled-goods
categories are acquired through `src/ingestion/connectors/dataset/sdmx.py` and
`eurostat.py` into the existing dataset store with vintages. The control-code
to customs-code correspondence is a sourced lookup table with its own
revision; a control code and a customs code are never treated as the same
classification.

**Subscriptions and source acquisition (`platform.subscriptions`,
`platform.source-acquisition`):** new designations, amendments, delistings and
control-list editions are subscription events derived from comparing
snapshots through the source-pack runtime. No separate scheduler or monitor
table is introduced, and events describe the source change only.

**Rights/terms:** each list has its own reuse terms and publication cadence.
Retained evidence is the published file or response with its digest and date;
snapshots are not redistributed beyond what each publisher's terms allow, and
missing or restricted terms default to link-only evidence.

## V1 acceptance

- Pinned offline fixtures cover EU, UN, OFAC and UK snapshots (at least two
  per list), two dual-use annex editions and one Comext vintage, including
  aliases, delistings, unresolved legal bases and conflicting statements.
- Re-ingestion is idempotent; every record keeps its list identity, source
  revision, acquisition time and source-reported dates.
- A party as of a date returns, per list, the listing revision in force, its
  aliases, any delisting and the legal basis with CELLAR passages; dates
  outside the snapshots yield an explicit unknown or not-listed status.
- A control code across two editions returns passage-level differences and
  the edition in force on a date without asserting legal effect.
- Identity matches are proposed, accepted and reverted without changing list
  records; unreviewed similar-name candidates stay candidates.
- No answer carries a screening, risk or compliance field.
- Offline replay and bounded live checks are recorded separately. Live
  evidence lives in `docs/development/sanctions-evidence/`; the demo answers
  one party and one control code with every citation visible.

## Deferred

Defer automated screening or batch matching of customer files, fuzzy
name-matching services, risk scoring, ownership-percentage aggregation across
lists ("50 percent rule" or similar reasoning), national lists beyond the EU,
UN, OFAC and UK sets, non-EU export-control lists, licensing guidance and any
compliance workflow. List entry counts do not imply that a party is or is not
subject to restrictions today.

There is no standalone Sanctions domain manifest, source-pack manifest,
enablement flag, or separate installation lifecycle in this scope.
