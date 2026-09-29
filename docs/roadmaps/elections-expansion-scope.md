# Elections expansion scope

Status: planned, 2026-09-27. An expansion of the existing Political bundle; it
does not create a separate domain or pack. It never predicts seats or
outcomes, never aggregates polls into a single "true" number, and never
derives a causal claim from news framing; forecasts are user-registered and
scored only after certified resolution.

## Delivery state (audited 2026-09-27)

- Shipped: nothing yet. Planning issues #1918–#2016 are open.
- Composition dependency: a `political.elections` provider descriptor and
  optional `elections` feature in the Political bundle (`packs/political/`),
  composed with `political.knowledge`, the existing polls connector,
  `geospatial.feature-query`, `geospatial.place-resolution`, the binary
  forecast ledger, `news.articles`, `platform.entity-identity`,
  `platform.subscriptions` and `platform.source-acquisition`.

Tracking: [#1908](https://github.com/Ikey168/Noesis/issues/1908).

## Outcome

Given a contest (an election, a constituency or a referendum), assemble
certified and preliminary results by constituency with result vintages,
candidates and lists, comparable poll series with fieldwork dates and sample
sizes, constituency geometry references, and user-registered forecasts
resolved against certified results. Preliminary and certified results stay
distinct, a poll is never a result, and every figure cites the source revision
it came from.

## GitHub implementation issues

- [ ] [#1918](https://github.com/Ikey168/Noesis/issues/1918) — L01 Audit election-result and poll source contracts and select bounded provider coverage.
- [ ] [#1923](https://github.com/Ikey168/Noesis/issues/1923) — L02 Define election, contest, constituency, candidate-or-list, result-vintage and poll-series records with revisions.
- [ ] [#1930](https://github.com/Ikey168/Noesis/issues/1930) — L03 Acquire federal and Berlin official results with constituency geometry references.
- [ ] [#1939](https://github.com/Ikey168/Noesis/issues/1939) — L04 Acquire UK Electoral Commission and MIT Election Lab results as comparable contests with jurisdiction-specific rules preserved.
- [ ] [#1947](https://github.com/Ikey168/Noesis/issues/1947) — L05 Acquire poll series through the existing polls connector with fieldwork dates, sample sizes and publisher terms.
- [ ] [#1957](https://github.com/Ikey168/Noesis/issues/1957) — L06 Reconcile parties, candidates and constituencies across elections through reviewable entity identity.
- [ ] [#1965](https://github.com/Ikey168/Noesis/issues/1965) — L07 Project constituencies onto Geospatial boundaries and answer place-based result queries as of a date.
- [ ] [#1974](https://github.com/Ikey168/Noesis/issues/1974) — L08 Register election forecasts through the existing binary forecast ledger and resolve them against certified results.
- [ ] [#1983](https://github.com/Ikey168/Noesis/issues/1983) — L09 Relate news frames and sentiment to contests as evidence without inferring causation.
- [ ] [#1991](https://github.com/Ikey168/Noesis/issues/1991) — L10 Monitor preliminary-to-certified changes, recounts and new polls through subscriptions.
- [ ] [#2000](https://github.com/Ikey168/Noesis/issues/2000) — L11 Register the `political.elections` provider descriptor and optional feature in the Political bundle.
- [ ] [#2008](https://github.com/Ikey168/Noesis/issues/2008) — L12 Add offline contest-to-results-and-polls acceptance coverage.
- [ ] [#2016](https://github.com/Ikey168/Noesis/issues/2016) — L13 Validate live result and poll coverage and publish a cited contest demo.

Order: L01 → L02 → {L03, L04, L05} → L06 → L07 → {L08, L09} → L10 → L11 →
L12 → L13.

## Sources

- [Bundeswahlleiterin open data](https://www.bundeswahlleiterin.de/en/service/opendata.html):
  federal results, preliminary and certified, with constituency identifiers
  and geometry files.
- [Berlin Landeswahlleitung](https://www.wahlen-berlin.de/) and
  [daten.berlin.de](https://daten.berlin.de/): Berlin state and district
  results and their open-data releases.
- [UK Electoral Commission electoral data](https://www.electoralcommission.org.uk/research-reports-and-data/electoral-data):
  UK results by constituency under first-past-the-post rules.
- [MIT Election Lab](https://electionlab.mit.edu/data): US results by state
  and county; data-use terms govern retention.
- [ParlGov](https://www.parlgov.org/): party and cabinet reference data used
  for identity assertions, not as a result source.
- [wahlrecht.de poll listings](https://www.wahlrecht.de/umfragen/): German
  poll series; `not-implemented` unless redistribution terms are confirmed,
  with publisher terms recorded per series.

L01 verifies the documented access method, terms, identifiers, vintages and
cadence per provider and records an `unverified-live` or `not-implemented`
decision for each. These links establish source candidates, not guaranteed
APIs or complete live coverage.

## Expansion of existing capabilities

**Political:** election, contest, constituency, candidate-or-list,
result-vintage and poll-series records become their own source-backed record
types in `src/kb/elections.py`, extending the pack ontology's `election`
object type and `contests` relation rather than a parallel vocabulary. Results
arrive through the existing `official-political-records` source pack
(`config/source_packs/political.json`) and
`src/ingestion/connectors/political_official.py`. A preliminary and a
certified publication are separate vintages of one contest; a later vintage
never overwrites an earlier one, and figures are stored as published, never
recomputed.

**Polls connector:** poll readings enter through `harvest_polls`,
`parse_poll_csv` and `poll_to_series` in
`src/ingestion/connectors/dataset/` and land in the existing
`ObservationStore`. Each reading keeps publisher, method, fieldwork window,
sample size and population. A poll is stored and typed as a poll series and is
never a result vintage; no series is blended into another.

**Entity identity:** parties, candidates and constituencies are linked across
elections and sources only through `EntityHistoryStore` decisions and the
pack's scoped aliases; unmatched names stay as source strings. Successor
parties and boundary changes are dated assertions with their source, kept side
by side with conflicting assertions. Every decision is reversible.

**Geospatial:** constituency geometry references are projected through the
existing `GeospatialFeatureProjector` into `GeospatialFeatureStore` as their
own collections with provider, native id, vintage and CRS; existing boundary
collections such as `alkis_bezirke:bezirksgrenzen` are queried, never copied.
Place-to-constituency links go through `geospatial.place-resolution` and stay
reviewable; a boundary change never alters a stored result.

**Forecast ledger:** election forecasts are created, revised, proposed,
resolved and scored only through the existing `ForecastStore` tools on
`noesis-knowledge-engine`. The expansion generates no forecast or probability.
Resolution proposals cite the certified vintage record; a preliminary vintage
never resolves a forecast, and scoring happens only after certified
resolution.

**News:** `news.articles` frames and sentiment are attached to a contest,
party or candidate only through a resolved entity mention or a reviewed user
assertion, and are shown next to results and polls for the same period. The
response contract has no correlation or causation field.

**Subscriptions and source acquisition:** new vintages, recounts, corrections
and poll readings are delivered through `SubscriptionStore`, and acquisition
runs through the source-pack runtime under its budgets. No watcher, scheduler,
permission ledger, project store, entity store or spatial store is added.

**Rights and terms:** result publishers, poll publishers and dataset
maintainers carry different reuse terms. Item-level terms are recorded per
source and per poll series; a series whose terms forbid redistribution is kept
as link-only metadata. Publicly viewable does not mean redistributable.

## V1 acceptance

- Pinned offline fixtures cover a federal or Berlin contest with a preliminary
  and a certified vintage, a UK or US contest with its jurisdiction rules, a
  poll series with fieldwork dates and sample sizes, a constituency boundary
  change and a party succession.
- Re-ingestion is idempotent; every result figure, poll reading, identity
  decision, geometry projection and news link cites its source revision.
- A query for a contest as of a date returns the vintage current at that date;
  the preliminary-to-certified difference list cites both sides.
- A place and a date resolve to the constituency whose boundary vintage was
  valid then, and its result vintages, without altering stored results.
- A registered forecast resolves only against a certified vintage; a contest
  without one reports an explicit unresolved status.
- Offline replay (`tests/unit/domains/test_elections_acceptance.py`) and
  bounded live checks (`docs/development/election-evidence/`) are recorded
  separately per provider. The demo answers one Berlin and one UK or US
  contest and states what remains unknown.

## Deferred

Defer seat projections and any outcome model, poll averaging or house-effect
adjustment, additional jurisdictions beyond the audited providers, precinct
or polling-station geometry, exit polls, campaign-finance records, and any
inference from news framing to results. Provider record counts do not imply
that results or polls can be redistributed.

There is no standalone Elections domain manifest, source-pack manifest,
enablement flag, or separate installation lifecycle in this scope.
