# Public finance expansion scope

Status: planned, 2026-09-27. An expansion of the existing Economics bundle; it
does not create a separate domain or pack. It never produces a fiscal forecast,
a waste or fraud determination, or a netted figure across accounting bases
without a cited method.

## Delivery state (audited 2026-09-27)

- Shipped: nothing yet. Planning issues #1919–#2014 are open.
- Composition dependency: a `economics.public-finance` provider descriptor and
  optional `public-finance` feature in the Economics bundle (`packs/economics/`),
  composed with `economics.knowledge`, `political.knowledge`, `legal.works`,
  `procurement.award-history`, `funding.opportunities`, `ownership.identity`,
  `platform.entity-identity`, `platform.subscriptions` and
  `platform.source-acquisition`. The feature coexists with the planned
  `economics.demographics` feature; both default to off.

Tracking: [#1909](https://github.com/Ikey168/Noesis/issues/1909).

## Outcome

Given a budget line, ministry, programme or beneficiary, assemble plan and
outturn figures with their vintages and Einzelplan/Kapitel/Titel hierarchy,
supplementary budgets, beneficiary payments, audit findings, and the legal acts
and legislative dossiers behind them, with procurement award history as linked
context. Every figure carries its source revision and accounting basis. Plan,
outturn and payment records stay distinct; conflicting figures are shown side
by side, and unknowns stay unknown.

## GitHub implementation issues

- [ ] [#1919](https://github.com/Ikey168/Noesis/issues/1919) — B01 Audit public-finance source contracts and select bounded provider coverage.
- [ ] [#1926](https://github.com/Ikey168/Noesis/issues/1926) — B02 Define budget-plan, budget-line, outturn-vintage, beneficiary-payment and audit-finding records with revisions.
- [ ] [#1933](https://github.com/Ikey168/Noesis/issues/1933) — B03 Acquire federal budget plans and outturns with chapter and title hierarchy.
- [ ] [#1941](https://github.com/Ikey168/Noesis/issues/1941) — B04 Acquire Berlin budget open data and link lines to districts and Geospatial places.
- [ ] [#1948](https://github.com/Ikey168/Noesis/issues/1948) — B05 Acquire EU Financial Transparency System payments and link beneficiaries to funding and corporate identity.
- [ ] [#1958](https://github.com/Ikey168/Noesis/issues/1958) — B06 Acquire Eurostat government finance statistics through the existing SDMX connector with vintages.
- [ ] [#1968](https://github.com/Ikey168/Noesis/issues/1968) — B07 Link budget lines to legal acts and legislative dossiers by citation and awards to procurement award history.
- [ ] [#1977](https://github.com/Ikey168/Noesis/issues/1977) — B08 Compare plan against outturn and vintage against vintage with cited sources and explicit accounting-basis notes.
- [ ] [#1987](https://github.com/Ikey168/Noesis/issues/1987) — B09 Monitor supplementary budgets, new payment publications and audit reports through subscriptions.
- [ ] [#1996](https://github.com/Ikey168/Noesis/issues/1996) — B10 Register the `economics.public-finance` provider descriptor and optional feature in the Economics bundle.
- [ ] [#2006](https://github.com/Ikey168/Noesis/issues/2006) — B11 Add offline budget-line-to-payments acceptance coverage.
- [ ] [#2014](https://github.com/Ikey168/Noesis/issues/2014) — B12 Validate live coverage and publish a cited budget dossier demo.

Order: B01 → B02 → {B03, B04, B05, B06} → B07 → B08 → B09 → B10 → B11 → B12.

## Sources

- [Bundeshaushalt open data](https://www.bundeshaushalt.de/DE/Open-Data/open-data.html):
  federal plan (Soll) and outturn (Ist) figures with the Einzelplan/Kapitel/Titel
  hierarchy.
- [Berlin Senate Department of Finance, budget](https://www.berlin.de/sen/finanzen/haushalt/):
  Berlin state budget by Einzelplan and district; machine access is to be
  confirmed, otherwise `not-implemented`.
- [EU Financial Transparency System](https://ec.europa.eu/budget/financial-transparency-system/):
  beneficiary-level commitments and payments from the EU budget.
- [EU budget](https://commission.europa.eu/strategy-and-policy/eu-budget_en):
  annual and amending budgets and their budget lines.
- [Eurostat government finance statistics](https://ec.europa.eu/eurostat/web/government-finance-statistics/database):
  ESA 2010 series through the existing SDMX/Eurostat dataset connector.
- [Bundesrechnungshof](https://www.bundesrechnungshof.de/): audit reports and
  findings that cite budget lines.
- [IMF data](https://data.imf.org/): Government Finance Statistics; reuse terms
  to be verified before any acquisition.

B01 verifies the documented access method, terms, authentication, rate limits,
identifiers, accounting basis and cadence per provider and records an
`unverified-live` or `not-implemented` decision for each. These links establish
source candidates, not guaranteed APIs or complete live coverage.

## Expansion of existing capabilities

**Economics:** add `src/kb/public_finance.py` as the record owner for
budget-plan, budget-line, outturn-vintage, beneficiary-payment and
audit-finding records, with source entries in the existing
`config/source_packs/economic.json`. Outturn and statistics vintages reuse
`src/domains/economic/releases.py` and the `kb_economic` vintage comparison;
Eurostat government finance series flow through the existing
`src/ingestion/connectors/dataset/{sdmx,eurostat}.py` connectors as
`dataset-series-v1` records. Amounts are normalised through the pint-based
`src/integrations/units.py`, and the source text is kept alongside.

**Legal and Political:** budget acts (Haushaltsgesetz, Nachtragshaushalt, EU
annual and amending budgets) are resolved through `legal.works`
(`src/kb/legal.py`, CELLAR and national publications) and linked to plan records
only by explicit citation; budget dossiers and votes come from
`political.knowledge` (`kb_political`, DIP) by dossier identifier. A shared
keyword produces a candidate, never a link.

**Procurement, Funding and identity:** beneficiaries are offered as candidates
to `canonical_entities`, `ownership.identity` and `funding.opportunities` grant
records through `platform.entity-identity` decisions
(`src/kb/entity_history.py`) that are reviewable and reversible; unmatched
beneficiaries stay as source strings. Award history from
`procurement.award-history` (`src/kb/procurement_identity.py`) is shown as
context and is never evidence that a payment was made.

**Geospatial:** Berlin district-scoped lines link to `GeospatialStore` places
only through the published district code; no spatial store is introduced.

**Subscriptions:** supplementary budgets, new payment publications and new
audit findings are delivered through the existing subscription outbox
(`src/kb/subscriptions.py`) on source-pack run watermarks; no new scheduler.

**Rights and terms:** each provider's reuse terms are recorded in B01 and on
the source entry. IMF GFS is not acquired until its terms are verified. Figures
are redistributed with attribution only where the terms permit; otherwise the
record links to the source.

## V1 acceptance

- Pinned fixtures for Bundeshaushalt, Berlin, FTS, Eurostat GFS and
  Bundesrechnungshof cover hierarchy, supplementary budgets, multiple outturn
  vintages, payments, findings and conflicting figures.
- Re-ingestion is idempotent; a changed figure creates a new revision and the
  earlier one stays addressable.
- Plan, outturn and payment records stay distinct; differences are computed
  only on the same accounting basis and currency, and figures on different
  bases are shown side by side with an explicit note.
- Links to acts, dossiers, grants and awards rest on explicit citations or
  reviewed identity decisions; candidates stay marked as candidates and every
  decision can be reversed.
- Monitors emit one event per new revision and none on replay after a restart.
- Offline replay and bounded live checks are recorded separately per provider
  (`docs/development/public-finance-evidence/`). The demo takes one federal
  budget line to a cited dossier with source revision and accounting basis
  visible on every figure.

## Deferred

Defer fiscal forecasting, deficit or debt projections, any waste, fraud or
value-for-money determination, cross-basis reconciliation without a published
method, municipal budgets beyond Berlin, sub-beneficiary or final-recipient
tracing, and an interactive budget explorer. Provider record counts do not
imply complete coverage of a budget or of its beneficiaries.

There is no standalone Public finance domain manifest, source-pack manifest,
enablement flag, or separate installation lifecycle in this scope.
