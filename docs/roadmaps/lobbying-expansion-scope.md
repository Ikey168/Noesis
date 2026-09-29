# Lobbying and transparency-register evidence expansion scope

Status: planned, 2026-09-27. An expansion of the existing Political bundle; it
does not create a separate domain or pack. Declarations are what a registrant
filed: the expansion makes no influence or corruption claim, infers no
undeclared lobbying, and never aggregates declared spend ranges into point
estimates.

## Delivery state (audited 2026-09-27)

- Shipped: nothing yet. Planning issues #1925–#2024 are open.
- Composition dependency: a `political.lobbying` provider descriptor and
  optional `lobbying` feature in the Political bundle (`packs/political/`),
  composed with `political.knowledge` (`kb_political`, the legislative dossier
  tools), `ownership.identity`, `market.legal-entities`,
  `platform.entity-identity`, OSINT source identity, `funding.opportunities`
  as cross-reference only, `platform.subscriptions` and
  `platform.source-acquisition`. The feature is default false and coexists
  with the planned optional `political.elections` feature.

Tracking: [#1911](https://github.com/Ikey168/Noesis/issues/1911).

## Outcome

Given a legislative dossier, an organisation or an office holder, assemble what
transparency registers declare: registrants, clients, declared interests and
spend ranges, meetings with officials, and register revisions, each linked by
citation to the dossiers Political already tracks. Register revisions, native
identifiers and unknowns stay visible. Conflicting declarations from different
registers are kept side by side and never reconciled.

## GitHub implementation issues

- [ ] [#1925](https://github.com/Ikey168/Noesis/issues/1925) — T01 Audit transparency-register and meeting-declaration source contracts and select bounded provider coverage.
- [ ] [#1934](https://github.com/Ikey168/Noesis/issues/1934) — T02 Define registrant, client, declared-interest, meeting, spend-declaration and register-revision records with revisions.
- [ ] [#1944](https://github.com/Ikey168/Noesis/issues/1944) — T03 Acquire EU Transparency Register entries with declared clients, spend ranges and revision history.
- [ ] [#1953](https://github.com/Ikey168/Noesis/issues/1953) — T04 Acquire German Lobbyregister entries and disclosed position papers.
- [ ] [#1962](https://github.com/Ikey168/Noesis/issues/1962) — T05 Acquire European Parliament and Commission meeting declarations and the UK consultant-lobbyist register.
- [ ] [#1973](https://github.com/Ikey168/Noesis/issues/1973) — T06 Match registrants and clients to corporate and entity identity through reviewable decisions.
- [ ] [#1982](https://github.com/Ikey168/Noesis/issues/1982) — T07 Link meetings and declared interests to legislative dossiers by explicit register fields or reviewed assertions.
- [ ] [#1993](https://github.com/Ikey168/Noesis/issues/1993) — T08 Answer who declared an interest in a dossier and met whom, with cited register revisions.
- [ ] [#1999](https://github.com/Ikey168/Noesis/issues/1999) — T09 Monitor new registrations, deregistrations, spend revisions and meetings through subscriptions.
- [ ] [#2009](https://github.com/Ikey168/Noesis/issues/2009) — T10 Register the `political.lobbying` provider descriptor and optional feature in the Political bundle.
- [ ] [#2018](https://github.com/Ikey168/Noesis/issues/2018) — T11 Add offline dossier-to-declared-interests acceptance coverage.
- [ ] [#2024](https://github.com/Ikey168/Noesis/issues/2024) — T12 Validate live register coverage and publish a cited dossier-interest demo.

Order: T01 → T02 → {T03, T04, T05} → T06 → T07 → T08 → T09 → T10 → T11 → T12.

## Sources

- [EU Transparency Register](https://transparency-register.europa.eu/): open
  data on registrants, sections, declared clients, fields of interest, EU
  legislative files and declared cost or revenue ranges.
- [German Lobbyregister](https://www.lobbyregister.bundestag.de/): data export
  of registrants, principals, clients, regulatory projects, expenditure ranges
  and disclosed position papers.
- [European Parliament meetings](https://www.europarl.europa.eu/meps/en/about/meetings):
  meeting declarations published by MEPs.
- [Commission meeting declarations](https://ec.europa.eu/transparencyinitiative/meetings/):
  meetings of Commissioners, cabinets and Directors-General with registered
  organisations.
- [UK Register of Consultant Lobbyists](https://registrarofconsultantlobbyists.org.uk/):
  registrants and quarterly client returns.
- [Bundestag party financing](https://www.bundestag.de/parlament/praesidium/parteienfinanzierung):
  published party accounts and donation notices, as filed.
- [Integrity Watch](https://www.integritywatch.eu/): aggregator; terms of reuse
  are verified before it is used, and then only as a fixture-level cross-check
  kept distinct from the primary registers.

T01 verifies the documented access method, terms, authentication, rate limits,
identifiers, revision exposure and cadence per provider and records an
`unverified-live` or `not-implemented` decision for each. These links establish
source candidates, not guaranteed APIs or complete live coverage.

## Expansion of existing capabilities

**Political:** store registrants, clients, declared interests, spend
declarations, meetings and register revisions as their own source-backed
record types in `src/kb/lobbying.py`, alongside the `official-political-records`
source pack (`config/source_packs/political.json`) and the legislative dossier
store (`src/domains/political/legislative_dossiers.py`). A declaration or
meeting links to a dossier only through an explicit register field (legislative
file, CELEX or ELI identifier, Bundestag printed-paper number) or a reviewed
assertion that cites both the register revision and the dossier revision. A
shared keyword or resembling title is a discovery candidate only. Links appear
in the dossier timeline and dependency views without changing dossier stage
semantics.

**Corporate identity (`ownership.identity`, `market.legal-entities`,
`platform.entity-identity`):** propose registrant and client identity
candidates against `canonical_entities`, LEI records and the ownership identity
service (`src/kb/ownership_identity.py`) through the existing candidate, review
and revert operations. One organisation in three registers keeps three
registrant records with three native identifiers; a reviewed match links them
to one entity but never merges the declarations. Unmatched names remain
register strings and still appear in answers, marked unmatched.

**OSINT source identity (`src/kb/source_identity.py`):** where a registrant is
also a publication or media organisation, the match is a candidate alias
decision under that store's review flow, so source dossiers can show declared
interests without asserting editorial influence.

**Funding & Grants (`funding.opportunities`):** EU grants a registrant
declares are stored as register assertions and cross-referenced to funding
opportunity records only as candidates; the expansion never confirms that a
grant was received.

**Platform (`platform.subscriptions`, `platform.source-acquisition`):**
register changes are monitored through the shared subscription provider on the
source pack's own schedule. A notification cites the previous and new register
revision and reports a changed spend range as the old and new range. No new
scheduler is introduced.

**Rights and terms:** register data is reused within each register's published
terms. Disclosed position papers are retained only where the register permits
it and are otherwise link-only. Aggregator content is never presented as a
primary register record.

## V1 acceptance

- Pinned fixtures for the EU Transparency Register, the Lobbyregister, EP and
  Commission meeting declarations and the UK register cover registrants,
  clients, declared interests, spend ranges with currency and period, meetings,
  revisions and deregistrations.
- Repeated ingestion is idempotent; a changed entry creates a new register
  revision that references its predecessor, and a deregistration is a lifecycle
  revision, not a deletion.
- Spend is stored and returned as the declared range; no store field or query
  tool derives a midpoint, sum or total across registrants, registers or years.
- A declaration or meeting links to a dossier only from an explicit register
  field or a reviewed assertion; candidates remain marked as candidates and
  links can be reverted.
- Identity matches are reviewable and reversible; unmatched registrants and
  clients still appear in answers, marked unmatched.
- `list_dossier_declared_interests`, `list_registrant_declarations` and
  `list_official_meetings` return every row with its register revision and
  source, keep conflicting declarations side by side and honour as-of queries.
- Subscriptions fire on new registrations, deregistrations, spend revisions and
  meetings with both revisions cited.
- Offline replay and bounded live checks are recorded separately per provider
  under `docs/development/lobbying-evidence/`. The demo answers a
  dossier-interest question and exposes its register revisions, identity review
  states and link kinds.

## Deferred

Defer influence scoring, network or centrality analysis over registrants and
officials, matching of unregistered organisations to meetings, cross-register
spend totals, national registers beyond Germany and the United Kingdom,
donation-to-vote correlation and any compliance or enforcement view. Register
entry counts describe what was filed, not the extent of lobbying activity.

There is no standalone Lobbying domain manifest, source-pack manifest,
enablement flag, or separate installation lifecycle in this scope. Provider
declarations extend the existing `official-political-records` source pack;
dossier links use the current legislative dossier store and query tools.
