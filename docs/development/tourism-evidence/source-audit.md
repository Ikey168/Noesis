# Tourism and hospitality: source-contract audit and bounded coverage (TO01)

Tracking: wave 2 tracker #2736 · recorded 2026-10-03.

No per-track tracker or delivery issue exists yet. They are opened once this
audit names a surviving source; the acquisition, store, link and live-validation
issues then cite this document.

This audit sets out, per source, what an `economics.tourism` provider in the
existing Economics bundle (`packs/economics/`) may acquire, how, and on what
terms. **It was written without network access: the publisher hosts
(`ec.europa.eu`, `www.untourism.int`, `www.unwto.org`, `www.e-unwto.org`) are
blocked by this runtime's egress proxy (verified 2026-10-03), so terms,
endpoints, dataset codes, dimension names and rate limits were not re-verified
live.** They come from the publishers' documentation as the author knows it.
Every item marked _verify_ must be checked against the live pages, the live terms
and a real response before the first dated live run (the track's "Validate live
coverage" issue, not yet opened). No source is `live` until that run exists.

The machine-readable copy of these decisions does not exist yet.
`PROVIDER_CONTRACTS`, `BOUNDED_COVERAGE`, `CAPS`, `EXCLUSIONS` and
`LIVE_VERIFICATION` will be added in `src/ingestion/tourism_sources.py` by the
track's acquisition issues and must match this audit; the source-pack entries
those issues declare carry the same `live_verification` status.

Non-goals for every source: no nowcasting, no filled months or regions, no
seasonal adjustment of our own, no blending of Eurostat and UN Tourism figures,
no occupancy rates, averages, per-capita or per-bed figures of our own, no
derived indicators, no forecasts.

## Access decisions

| Source (proposed source-pack id) | Publisher | Delivers | Access | Decision |
| --- | --- | --- | --- | --- |
| `eurostat-tourism-occupancy` | Eurostat, tourism statistics (Regulation (EU) No 692/2011, _verify_ amendments) | nights spent and arrivals at tourist accommodation establishments, by residence of guest and accommodation type; monthly national, annual NUTS 2 | Eurostat SDMX 2.1 dissemination API, SDMX-CSV through the existing SDMX connector (ESTAT path); datasets `tour_occ_nim`, `tour_occ_arm`, `tour_occ_nin2` (_verify_ codes and dimension order) | `unverified-live` |
| `eurostat-tourism-capacity` | Eurostat | establishments, bedrooms and bed places, annual, national and NUTS 2 | same path; `tour_cap_nuts2` (_verify_) | `unverified-live` |
| UN Tourism statistics (Tourism Statistics Database, Compendium and Yearbook of Tourism Statistics, data dashboards) | UN Tourism (formerly UNWTO) | inbound and outbound arrivals, expenditure, accommodation and industry indicators per country | no documented, stable, open machine API known to the author; the Compendium and Yearbook are distributed through the UN Tourism e-library, with bulk reuse terms the author cannot state (_verify_); dashboards are interactive pages | `not-implemented`: no stable machine access and unclear redistribution terms; dashboard pages and e-library files are never scraped |

The UN Tourism indicators that reach the UN SDG Global Database (for example SDG
8.9.1, tourism direct GDP) could be read through the UNSD SDG API instead
(_verify_). That route is a different source with its own terms and is not
audited here; adding it needs an amendment to this audit and a source-pack
version bump.

No Eurostat term was found that forbids the intended use (store aggregates with
citations, show them side by side, export cited evidence bundles). Should the
live check find otherwise, the source moves to `blocked` in `LIVE_VERIFICATION`.

## Per-source contract

| Source | Endpoints | Authentication and keys | Licence and redistribution | Rate limits | Revision model: updates, corrections, removals |
| --- | --- | --- | --- | --- | --- |
| Eurostat occupancy and capacity | `https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/{dataset}/{key}?format=SDMX-CSV&startPeriod=...` | none | Eurostat reuse policy (Commission Decision 2011/833/EU): reuse with acknowledgement (_verify_) | no published quota (_verify_); one request per declared document | `LAST UPDATE` dates each release; monthly data are first published provisional and revised in later months, each changed release a new vintage; `OBS_FLAG` letters (`p` provisional, `e` estimated, `b` break, `c` confidential, `u` low reliability, _verify_ the list) are kept per value; `c` is a status, never a value; a NUTS code-list change (NUTS 2021 to 2024, _verify_) makes a different place key, linked only through Eurostat's published correspondence; a series a later complete release no longer states becomes a `removed_by_source` vintage |
| UN Tourism | none acquired | - | not established (_verify_) | - | - |

**Unavailable-access fallback.** A failed document (HTTP error, redirect to
another host, schema drift, a response larger than the budget) fails that
source's run with its code and a receipt; earlier vintages stay current and
nothing is marked removed or revised because of a failure. Readiness reports the
source as `stale`. UN Tourism queries report `not-implemented`, never empty.

## Definitions recorded per series

- **Concept:** nights spent (each night a guest actually stays) and arrivals
  (guests checking in) as Eurostat defines them; capacity as establishments,
  bedrooms or bed places on the reference date or year Eurostat states.
- **Dimensions in the key:** residence of guest (`c_resid`: domestic, foreign,
  total), accommodation type (`nace_r2`: `I551` hotels, `I552` holiday and
  short-stay, `I553` camping, `I551-I553` total, _verify_), unit, frequency and
  geography with its NUTS version.
- **Coverage threshold:** the establishment size threshold each country applies
  (the regulation's minimum and national deviations, _verify_) is recorded as a
  definition note; a `d` flag or published deviation is a source-stated
  comparability note.
- Monthly national and annual NUTS 2 series are different series even where they
  cover the same nights; annual totals are never computed from months.

## Data minimisation decision

The sources publish aggregate statistics; none returns data about a person, a
guest or an individual establishment. Decision:

- **Stored:** published aggregates per place, period and series key, with flags,
  notes, definitions and citations.
- **Redacted:** nothing; no personal field is ever acquired.
- **Excluded:** traveller survey microdata behind the demand-side `tour_dem_*`
  tables; establishment-level returns; Eurostat's experimental short-stay
  platform statistics (`tour_ce_oa*`, collaborative-economy data from booking
  platforms, _verify_), which are out of scope until separately audited;
  confidential cells, stored as their status only. A derived, filled, blended or
  forecast value is refused at write time.
- **Retention:** release vintages are kept for provenance; no erasure workflow
  applies.

## Bounded first coverage

| Source | Places | Series | Periods | Caps |
| --- | --- | --- | --- | --- |
| Eurostat occupancy, monthly | Germany (DE) | `tour_occ_nim` and `tour_occ_arm`: total, domestic and foreign residence, `I551-I553` | at most 36 months from a declared start period | 2 documents, 30 series per response |
| Eurostat occupancy, NUTS 2 | Berlin (`DE30`) | `tour_occ_nin2`: total residence, `I551-I553` | from a declared start year | 1 document, 10 series |
| Eurostat capacity | Berlin (`DE30`) | `tour_cap_nuts2`: establishments and bed places | from a declared start year | 1 document, 10 series |

Justification: one country and one of its NUTS 2 regions show both frequencies,
the residence split, provisional-to-revised monthly vintages and a regional
confidentiality flag, without needing a second publisher. Berlin is the region
the Geospatial bundle already resolves, so the NUTS code links to its boundary
feature by shared published code (as `src/kb/public_finance_places.py` links
districts), and the Labour link reaches `economics.labour` series for
accommodation and food services (NACE Rev.2 section `I`) for the same place by
shared code and citation, never as a ratio. Numeric values reuse the Economics
series storage (`register_series` in `src/domains/economic/model.py`:
`economic_vintages`, `dataset_observations`) as `src/kb/labour_statistics.py`
does; no new series store or record shape (`statistical-series`) is introduced.
Every further place or dataset is a source-pack version bump.

## Live verification

| Source | Status | Checked | Evidence |
| --- | --- | --- | --- |
| Eurostat occupancy | `unverified-live` | - | none yet; offline fixtures to be authored |
| Eurostat capacity | `unverified-live` | - | none yet; offline fixtures to be authored |
| UN Tourism statistics | `not-implemented` | - | no stable machine access, terms not established (_verify_) |

The fixtures will be authored, not captured: synthetic values for reference
years 2094-2097 and release dates in 2098-2099, so nothing can be mistaken for a
published figure.
