# Performing arts and events: source-contract audit and bounded coverage (PA01)

Tracking: wave 3 tracker #2814 · audit issue #2821 · recorded 2026-11-01.
Bundle decision: [ADR-007](../../architecture/decisions/ADR-007-culture-bundle.md)
(CU01, #2815).

No per-track tracker or delivery issue exists yet. This audit leaves two
sources, both `unverified-live`: **Wikidata statements on venues, ensembles,
festivals and stage works**, through the existing `wikidata` connector and kept
as "what Wikidata states", and **Eurostat cultural participation statistics**
through the Eurostat path the Economics and Society providers already use. No
source for performances themselves (programmes, dated performances) survives,
so the track is bounded to venues, organisations, works and participation
statistics.

**Written offline.** The publisher hosts (`www.wikidata.org`, `ec.europa.eu`,
`www.buehnenverein.de`, `www-genesis.destatis.de`) were blocked by this
runtime's egress proxy (verified 2026-11-01), and nothing was fetched. Dataset
codes, dimension names, Wikidata property and class identifiers and terms come
from the publishers' documentation as the author knows it. Every item marked
_verify_ is unchecked and must be checked before the first dated live run. The
only facts confirmed are those in this repository: the `wikidata` connector in
`src/ingestion/media_metadata_sources.py` and the Eurostat sources already
declared in `config/source_packs/economic.json`. No source here is `live`.

**The machine-readable copy does not exist yet.** The track's acquisition
issues add `PROVIDER_CONTRACTS`, `BOUNDED_COVERAGE`, `MINIMISATION` and
`LIVE_VERIFICATION` for `culture.performing-arts` and must match this audit.

Non-goals for every source: no ticketing, event listings or scraping of venue
or festival sites; no attendance, audience or participation estimates of our
own (no rates, sums or averages computed from published figures); no reviews or
ratings; no images or recordings.

## Access decisions

| Source (proposed source-pack id) | Publisher | Delivers | Access | Decision |
| --- | --- | --- | --- | --- |
| `wikidata-performing-arts` | Wikimedia Foundation (Wikidata), reusing the `wikidata` connector | statements on declared venue, ensemble, festival and stage-work items, as Wikidata states them, with rank, qualifiers and references | `wbgetentities`, one declared QID per request (the connector's `request_for`, confirmed in code) | `unverified-live` (statements only, never a register) |
| `eurostat-cultural-participation` | Eurostat, EU-SILC ad-hoc modules on cultural participation (_verify_ module years, believed 2006, 2015 and 2022) | share of people who attended live performances, cinema or cultural sites in the past 12 months, by frequency, sex, age, education and income, national level | Eurostat dissemination API, read as the existing Eurostat sources read it (for example `eurostat-demography-migration` through the `demographics` connector, and the tourism sources through the SDMX path); which connector the track extends is a delivery choice; datasets `ilc_scp01` to `ilc_scp08` and their 2022 successors (_verify_ all codes and dimensions) | `unverified-live` |
| `buehnenverein-theaterstatistik` | Deutscher Bühnenverein | Theaterstatistik: per theatre and orchestra in Germany, performances, attendance, staff, income and public subsidy, per season | priced annual publication (print and PDF, _verify_); no API or open file known to the author (_verify_) | `not-implemented` |

Reasons:

- **Wikidata.** Same access path and same narrow survival as in the
  [FB01 audit](../film-broadcast-evidence/source-audit.md) and the MO01
  amendment: one item per request under CC0 1.0, through the shared culture
  statement reader of ADR-007, every record "Wikidata states, at revision N,
  that X" with references, never a register of venues or ensembles.
  Wikidata holds venues, ensembles, festivals and stage works with some depth;
  it holds few individual performances or productions as items (_verify_), so
  it is not a source for events.
- **Eurostat cultural participation.** Aggregates from a harmonised EU survey,
  published by Eurostat under its reuse policy (Commission Decision
  2011/833/EU, _verify_). The track reuses the existing Eurostat dissemination
  path; no new connector. The module is ad hoc, so the series have few
  reference years and the question wording changes between modules (for
  example the 2022 module's cultural activities and reasons for non-participation
  differ from 2015, _verify_). Each module year is its own definition revision;
  nothing is chained across modules. EU-SILC already reaches `society.income`;
  that provider is not changed.
- **Deutscher Bühnenverein Theaterstatistik.** The issue asked whether national
  statistics should be audited. This is the main German candidate. It is a
  priced publication of a members' association (_verify_ price, format and terms);
  no open licence or machine access is known. Not implemented; a reviewer may
  cite it link-only. Destatis publishes some culture tables in GENESIS
  (_verify_ whether theatre or music-venue tables exist and are sourced from the
  Theaterstatistik); if one exists it could join through the existing
  `destatis-genesis-population-migration` path and its credential
  `NOESIS_DESTATIS_GENESIS_TOKEN`, as an amendment to this audit.
- **Events themselves.** No candidate offers dated performances under open
  terms with machine access: programme data sits on venue sites (scraping
  excluded) and in ticketing platforms (excluded). The track therefore does not
  use the `events-notices` shape. Wikidata P1191 "date of first performance" on
  a work is kept as a dated statement on the work, not as an event.

### Further candidates not audited

Other national statistics (for example Arts Council England's National
Portfolio Organisation data, the French ministry of culture's DEPS
publications) and Europeana's performing-arts collections (already reachable
through `science.cultural` as objects) were not audited. Adding one needs an
amendment to this audit and a source-pack version bump.

## Per-source contract (as far as known)

| Source | Endpoints | Authentication and keys | Licence and redistribution | Rate limits | Revision model: updates, corrections, removals |
| --- | --- | --- | --- | --- | --- |
| Wikidata | the connector's `wbgetentities` request. Classes for P31, all _verify_: Q24354 theatre (building), Q153562 opera house, Q1060829 concert hall, Q868557 music festival, Q1569406 theatre festival, Q2088357 musical ensemble, Q742421 theatre company, Q1344 opera, Q2743 musical theatre work, Q25379 play, Q2643890 ballet. Properties, all _verify_: P131 located in the administrative territorial entity, P17 country, P625 coordinates, P1083 maximum capacity, P571 inception, P576 dissolved, P159 headquarters location, P86 composer, P50 author, P87 librettist, P1191 date of first performance, P4647 location of first performance, P837 day in year for periodic occurrence, P2257 frequency of event. References P248, P854, P813, P143 | none; Wikimedia User-Agent policy with the optional `NOESIS_MEDIA_METADATA_CONTACT` contact | CC0 1.0 (confirmed in the connector's contract); attribution "Wikidata" plus each statement's references | 500 ms interval and `maxlag=5`; HTTP 429 or maxlag stops the run | as in FB01: `lastrevid`, statement ids, rank and qualifiers kept; deprecated statements never current; a statement absent later is `not-returned`; P143-only references marked `reference: wikimedia-import`. P1083 capacity is a stated value with its qualifiers, never an attendance figure |
| Eurostat cultural participation | `https://ec.europa.eu/eurostat/api/dissemination/...` as the existing Eurostat sources (_verify_ SDMX 2.1 or statistics 1.0 path per dataset) | none | Eurostat reuse policy, reuse with acknowledgement (_verify_) | no published quota (_verify_); one request per declared document | `LAST UPDATE` dates each release, each changed release a new vintage; `OBS_FLAG` letters (`u` low reliability, `b` break, `c` confidential, _verify_ the list) kept per value; `c` is a status, never a value; each module year is a separate definition revision and a series break, never chained; a series a later complete release no longer states becomes a `removed_by_source` vintage |
| Theaterstatistik | none acquired | - | not open (_verify_); link-only | - | - |

**Unavailable-access fallback.** A failed document fails that source's run
with its code and a receipt; earlier vintages and revisions stay current and
nothing is marked removed. The provider reports `stale`. Theaterstatistik and
event queries report `not-implemented`, never empty.

## Definitions recorded per series

- **Concept:** the share of the population in Eurostat's stated age range who
  report the stated activity (for example attending live performances) in the
  stated reference period, as Eurostat defines it per module (_verify_
  wording per module year).
- **Dimensions in the key:** activity, frequency, the breakdown (sex, age,
  education, income quintile, degree of urbanisation), unit, geography, time.
- **Survey basis:** EU-SILC module year and reference period; the
  self-reported, sample-based nature is recorded as a definition note.

## Data minimisation decision

- **Eurostat:** aggregates only; no microdata is requested or stored.
  Confidential and low-reliability cells are kept with their flags.
- **Wikidata:** works name composers, librettists and authors; ensembles may
  name members and directors. Per ADR-007, decision 5, a person is kept as QID,
  one label, role and `kind: person`; nothing is read from a human item
  (no birth or death data, citizenship, family, religion P140 or image). Member
  lists of ensembles (P527 has part, _verify_) are not read in first coverage.
  Acquisition starts at declared venue, ensemble, festival or work QIDs and
  never from a person.
- **Retention:** vintages and revisions retained for provenance.

## Record shape and reuse

Shapes: **registry-records** (venues, ensembles, festivals, stage works) and
**statistical-series** (participation). No new shape; `events-notices` is not
used. Reuse: the `wikidata` connector and the ADR-007 culture statement reader;
the Economics series storage (`register_series`, `economic_vintages`) for
Eurostat values, as `src/kb/labour_statistics.py` and the tourism track do;
places through the Geospatial bundle's NUTS and country codes; citation-only
links to `science.cultural` objects (for example a Europeana item depicting a
declared venue) and to `science.media-metadata` work records (for example a
recorded opera).

## Bounded first coverage

| Source | Selection | Caps |
| --- | --- | --- |
| Wikidata | 5 declared venue QIDs in Berlin, 3 declared ensemble QIDs, 2 declared festival QIDs, 10 declared stage-work QIDs | 20 item requests and 250 statements per run; no person item fetched |
| Eurostat cultural participation | Germany (DE) and the EU aggregate, live-performance attendance by frequency, total and by sex, every module year Eurostat publishes for those series | 2 documents, 40 series per response |

Justification: Berlin is the city the Geospatial bundle already resolves, so
venue items link to place features by stated coordinates and administrative
entity. One country and the EU aggregate show the module-year breaks and flags
without a second participation publisher. Every further selection is a
source-pack version bump.

## Live verification

| Source | Status | Checked | Evidence |
| --- | --- | --- | --- |
| Wikidata performing-arts statements | `unverified-live` | - | none for these properties; the connector itself is `unverified-live` |
| Eurostat cultural participation | `unverified-live` | - | none; dataset codes and dimensions _verify_ |
| Deutscher Bühnenverein Theaterstatistik | `not-implemented` | - | priced publication, no open terms or machine access known (_verify_) |

Fixtures are authored, not captured: fictional venues, ensembles and works with
QIDs in the `Q9999xxxxx` range, Eurostat values for fictional reference years
2094-2097, release stamps dated in the past as the tourism audit's deviation
note requires.

## Conclusion

- **Survives:** Wikidata statements (narrowly, as "what Wikidata states") and
  Eurostat cultural participation, both `unverified-live`. A per-track tracker
  may open; its first issue is a bounded live check of the Eurostat dataset
  codes and of at least 15 of the 20 declared Wikidata items.
- **Scope:** venues, organisations, works and participation statistics; no
  performances. The subdomain title says "events"; coverage is reported as
  "venues, organisations and participation, no event records" until a source
  for performances survives an amended audit.
- **Depth:** two publishers (Wikimedia and Eurostat), so no thin row, though
  they answer different questions; ADR-006 decision 5 applies.
