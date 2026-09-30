# Astronomy and Space: source-contract audit and bounded coverage (AS01, #2150)

Tracking issue: #2149. This audit records, for every candidate astronomy and
space source, the endpoint, authentication, terms and attribution duties, rate
limits, stable identifiers, how revisions, retractions, demotions, false
positives and superseded solutions are published, and the access decision the
Astronomy pack (`packs/astronomy/`) implements.

**Verification status.** No live request was made while writing this audit.
The build environment has no network access to the Minor Planet Center, JPL,
the NASA Exoplanet Archive, GCAT, CelesTrak or NOAA SWPC. Every claim marked
**(verify)** comes from the providers' public documentation as known at the
time of writing, and must be checked against the live terms and services
before live acquisition is accepted (AS13, #2162, which this change does not
implement). The adapters in `src/ingestion/astronomy_sources.py` parse the
documented shapes; tabular formats (TAP CSV, SATCAT CSV, GCAT TSV) are read
through **declared column mappings**, so a changed header is a declaration
change, not a code change. Every fixture under `tests/fixtures/astronomy/` and
`tests/fixtures/source_packs/astronomy-*.json` is authored and names fictional
objects (provisional designations in the 2026 range that the MPC has not
assigned to the objects described, fictional host stars, fictional launch
vehicles, fictional satellites and fictional alert serial numbers). None of it
is captured data.

`PROVIDER_CONTRACTS` and `LIVE_VERIFICATION` in
`src/ingestion/astronomy_sources.py` restate these decisions in code. Every
implemented source is `unverified-live` until a dated live run is recorded
under this directory.

## Non-goals enforced by every decision below

* No orbit determination, propagation, ephemeris generation, close-approach
  computation or conjunction screening by Noesis. Horizons is therefore not
  used to generate anything, and TLE/GP element sets are not acquired.
* No impact-risk, hazard or collision verdict. Sentry figures are quoted with
  their listing date and method; Noesis adds none.
* No exoplanet validation or disposition. The archive's disposition code is
  quoted verbatim beside its mapping to the record vocabulary.
* No classified, export-controlled or account-restricted catalogue data is
  redistributed (Space-Track: see below).
* No operational space-weather advice. SWPC messages are stored verbatim.

## Summary

| Source | Endpoint | Auth | Format parsed | Decision |
| --- | --- | --- | --- | --- |
| MPC designation identifier API | `https://data.minorplanetcenter.net/api/query-identifier` | none **(verify)** | JSON (`mpc-identifier-json`) | `implement` (unverified-live); the live API expects the identifier in a GET body **(verify)**, which the runtime's GET-only transport does not send, so the first live run may fail with `schema_drift` |
| MPC orbits (MPCORB) | `https://minorplanetcenter.net/iau/MPCORB/` | none | MPCORB fixed-width lines (`mpcorb-dat`) | `implement` (unverified-live), bounded to declared objects; full-file mirroring is out of scope |
| MPC orbit API (`mpc_orbits`) | `https://data.minorplanetcenter.net/api/get-orb` | none **(verify)** | JSON | `link-only`: MPCORB lines carry the solution reference, epoch, arc and observation count the queries need; the JSON orbit API is recorded for AS13 |
| JPL SBDB API | `https://ssd-api.jpl.nasa.gov/sbdb.api` | none | JSON (`jpl-sbdb-json`) | `implement` (unverified-live) |
| JPL Horizons API | `https://ssd.jpl.nasa.gov/api/horizons.api` | none | n/a | `link-only`: Horizons generates ephemerides; the only metadata v1 would take (the solution ID it uses) is already stated by SBDB. A Horizons URL may be cited as a locator, never queried |
| JPL Sentry API | `https://ssd-api.jpl.nasa.gov/sentry.api` | none | JSON (`jpl-sentry-json`) | `implement` (unverified-live), quoted only: listing status, listing/removal date and summary figures as published |
| NASA Exoplanet Archive TAP (`ps`, `pscomppars`, `toi`, `cumulative`) | `https://exoplanetarchive.ipac.caltech.edu/TAP/sync` | none | TAP sync CSV (`exoplanet-tap-csv`) | `implement` (unverified-live), bounded by an explicit host list in each declared ADQL `WHERE` clause |
| NASA Exoplanet Archive removed/retracted planets listing | `https://exoplanetarchive.ipac.caltech.edu/` (page path **(verify)**) | none | CSV (`exoplanet-removed-csv`) | `implement` (unverified-live); the column names are an assumption **(verify)**; retraction is only ever recorded from this listing, never inferred from a row's absence |
| GCAT (Jonathan McDowell) launch log, satellite catalogue and organisations | `https://planet4589.org/space/gcat/tsv/` | none | TSV (`gcat-launch-tsv`, `gcat-satcat-tsv`, `gcat-orgs-tsv`) | `implement` (unverified-live), bounded to declared launch years |
| CelesTrak SATCAT | `https://celestrak.org/pub/satcat.csv` (or the query API) | none | CSV (`celestrak-satcat-csv`) | `implement` (unverified-live), bounded to declared international-designator years |
| NOAA SWPC alerts, watches and warnings | `https://services.swpc.noaa.gov/products/alerts.json` | none | JSON (`swpc-alerts-json`) | `implement` (unverified-live); the feed is a rolling window, so the store keeps every product it saw |
| Space-Track.org (18th SDS catalogue, GP data) | `https://www.space-track.org/` | account | n/a | `not implemented`: the user agreement restricts redistribution of account-obtained data **(verify)**; CelesTrak SATCAT and GCAT cover the catalogue fields v1 needs. Link-only: an object may cite its Space-Track page URL |
| ESA NEOCC (risk list, close approaches) | `https://neo.ssa.esa.int/` | none for the risk list **(verify)** | n/a | `not implemented` in v1: the risk list is a second published risk source; quoting it would need its own listing-date semantics and terms review **(verify)**. Documented for a later version; Sentry is the only quoted risk listing in v1 |

## Terms and attribution

| Source | Terms | Attribution duty recorded in the source pack |
| --- | --- | --- |
| MPC | MPC data are free to use; the MPC asks users to acknowledge the Minor Planet Center **(verify)** | "This research has made use of data and/or services provided by the International Astronomical Union's Minor Planet Center." |
| JPL SBDB, Sentry | NASA/JPL data are generally not copyrighted; the JPL image/data use policy asks for credit to NASA/JPL-Caltech **(verify)** | "Courtesy NASA/JPL-Caltech (JPL Small-Body Database / Sentry)." |
| NASA Exoplanet Archive | The archive asks publications to acknowledge it and to cite the archive DOI of the table used **(verify)**; each parameter set cites its own reference | "This research has made use of the NASA Exoplanet Archive, which is operated by the California Institute of Technology, under contract with NASA under the Exoplanet Exploration Program." plus the table DOI (e.g. `10.26133/NEA12` for `ps`) **(verify)** |
| GCAT | CC-BY 4.0 **(verify)** | "GCAT (General Catalog of Artificial Space Objects), Jonathan C. McDowell, planet4589.org/space/gcat." |
| CelesTrak SATCAT | CelesTrak terms of use; data derived from public 18 SDS information **(verify)** | "SATCAT data courtesy of CelesTrak (T.S. Kelso)." |
| NOAA SWPC | US government work, public domain **(verify)** | "NOAA Space Weather Prediction Center." |

## Rate limits and bounds

| Source | Rate limit | v1 bound (declared in `config/source_packs/astronomy.json`) |
| --- | --- | --- |
| MPC APIs | not published; the MPC asks for moderate use **(verify)** | a named small-body list per declaration (at most 50 identifiers), one request per object |
| MPCORB | static files; daily regeneration **(verify)** | the declared objects' lines only; the adapter emits nothing for other lines and counts them as `out_of_scope` |
| JPL SBDB, Sentry | JPL asks for no more than one concurrent request **(verify)** | one request per declared object |
| Exoplanet Archive TAP | none published; synchronous queries time out on large results **(verify)** | explicit host list (at most 25 hosts) in each declared query; no full-table mirroring |
| GCAT | static TSV files, updated irregularly **(verify)** | declared launch years (e.g. 2026) |
| CelesTrak SATCAT | CelesTrak asks clients not to poll more than every two hours and blocks abusive clients **(verify)** | declared international-designator years |
| SWPC alerts | JSON products regenerated every minute **(verify)** | the rolling window the feed serves; polling cadence runs through subscriptions and source-pack schedules (at most hourly in the pack) |

## Stable identifiers

| Source | Stable identifier used as the record key | Notes |
| --- | --- | --- |
| MPC designations | the designation itself, normalised to the unpacked form (`2026 AB12`, `(700001)`); the packed form (`K26A12B`) is kept beside it | packed/unpacked conversion is deterministic (MPC packed-designation rules) |
| MPC identifications | the **secondary** designation (one identification per designation linked to a primary) | a changed primary is a revision of that identification, so keys never change when older data arrives |
| MPCORB orbit | packed designation + orbit reference (`MPO123456`, `E2026-C99`) + epoch | MPCORB states no solution ID; the reference and epoch distinguish solutions **(verify)** |
| JPL SBDB orbit | designation as stated + `orbit_id` | `orbit_id` increments with each new JPL solution; SPK-IDs change on numbering, so objects link by the source-stated designation, never by SPK-ID |
| JPL Sentry | designation as stated | removal is a revision with the removal date |
| Exoplanet Archive | `ps`: `pl_name` + `pl_refname`; `pscomppars`: `pl_name`; `toi`: `toi`; `cumulative`: `kepoi_name` | host stars: `hostname` plus `tic_id` / `gaia_id` where published |
| GCAT | `#Launch_Tag` (launches), `#JCAT` (objects), `#Code` (organisations) | Satcat number and piece are fields |
| CelesTrak SATCAT | `NORAD_CAT_ID` | `OBJECT_ID` is the COSPAR international designator |
| NOAA SWPC | `product_id` + `Serial Number` | the serial number is stated in the message header |

## How revisions, retractions, demotions, false positives and superseded solutions are published

* **MPC.** A new identification is announced in an MPEC or MPC batch
  **(verify)**; the identifier API then returns the secondary designation under
  the primary. A superseded orbit is simply replaced in MPCORB; the reference
  field changes. The store keeps the earlier line as an earlier vintage. The
  identifier API states no announcing circular; the adapter records one only
  when the response states it (`identifications[].reference`, an assumed field
  **(verify)**), otherwise the announcement is an explicit unknown.
* **JPL SBDB.** A new solution has a new `orbit_id` and `soln_date`; earlier
  solutions are not served **(verify)**. Each `orbit_id` becomes its own
  `orbit_solution` record, so earlier vintages stay on record once seen.
* **JPL Sentry.** An object removed from the risk list is served with a
  removal date instead of figures **(verify)**. The record appends a revision
  with `listing_status: removed` and `removed_at`.
* **NASA Exoplanet Archive.** A disposition change (TOI `PC` to `CP` or `FP`,
  KOI `CANDIDATE` to `FALSE POSITIVE`) appears as a changed row with a new
  `rowupdate` **(verify)**. A planet that fails confirmation is removed from
  `ps` and listed on the archive's removed-planets page with a note and
  reference **(verify)**. A row that disappears from a complete bounded listing
  is recorded as `no_longer_listed` with the date observed; only the removed
  listing produces a `retracted` assertion. `pl_controv_flag = 1` marks a
  controversial planet.
* **GCAT.** Rows are edited in place; the files carry an update date in their
  header comment **(verify)**. A changed outcome code or decay date is a
  revision.
* **CelesTrak SATCAT.** `DECAY_DATE` and `OPS_STATUS_CODE` change in place; a
  change is a revision.
* **NOAA SWPC.** Products are never edited; a cancellation or extension is a
  new product whose header names the serial number it cancels or extends
  (`Cancel Serial Number:`, `Extension to Serial Number:`). A product that
  leaves the rolling feed stays recorded.

## Timestamps and the as-of cutoff

| Record | Source's own date (orders revisions and decides what is current) | Publication clock for as-of queries |
| --- | --- | --- |
| designation, identification | the stated announcement date where given | stated date (end of day, UTC), else first observation |
| orbit_solution | JPL `soln_date`; MPCORB: the file date the declaration states | `computed_at` / file date, else first observation |
| impact_risk_listing | Sentry listing date / `removed` date | the same |
| exoplanet_status_assertion | `rowupdate` (TAP), removal date (removed listing) | the same |
| launch, launch_outcome, orbital_object | GCAT/SATCAT file date the declaration states | file date, else first observation |
| space_weather_product | the header `Issue Time` | issue time |

A later revision is never visible before it was first observed, and an
optional `acquired_by_ms` limits every answer to what had been acquired by
then. A date before the first publication answers `not_yet_published`, not
absence.

## Launch outcome codes (GCAT)

GCAT's `Launch_Code` is kept verbatim. The mapping to `launch_outcome` is
documented here and in `GCAT_OUTCOMES` **(verify against GCAT's launch-code
documentation)**:

| Second character of `Launch_Code` | Meaning in GCAT | `launch_outcome` |
| --- | --- | --- |
| `S` | success | `success` |
| `F` | failure | `failure` |
| `P` | partial failure (reached a wrong orbit) **(verify)** | `partial` |
| `U` or anything else | unknown / not coded | outcome left absent; listed in `unknowns` |

The first character (`O` orbital, `S` suborbital, `D` deep space, ...) is kept
as `launch_class` verbatim.

## Exoplanet disposition codes

| Table | Native value | `exoplanet_status_assertion.disposition` |
| --- | --- | --- |
| `ps` (default parameter set, `default_flag = 1`) | row present | `confirmed`; `controversial` when `pl_controv_flag = 1` |
| `toi` (`tfopwg_disp`) | `CP`, `KP` | `confirmed` |
| `toi` | `PC`, `APC` | `candidate` |
| `toi` | `FP` | `false_positive` |
| `toi` | `FA` | `false_alarm` |
| `cumulative` (`koi_disposition`) | `CONFIRMED` / `CANDIDATE` / `FALSE POSITIVE` | `confirmed` / `candidate` / `false_positive` |
| removed-planets listing | row present | `retracted` |

Any other native value is kept verbatim with no mapped disposition and is
listed in the record's `unknowns`.

## Bounded v1 coverage

* **Small bodies:** a named list of at most 50 identifiers (packed or unpacked
  designations or numbers) declared per source; v1 declares (433) Eros,
  (99942) Apophis, (101955) Bennu and 2024 YR4 **(verify the selection)**.
  Parsers are stateless, so the list names every designation to follow: an
  object numbered after its provisional designation was declared is matched
  again only once its number is added to the declaration.
* **Exoplanets:** an explicit host list (at most 25 hosts) in each TAP query;
  v1 declares TRAPPIST-1 (TIC 278892590), TOI-700 (TIC 150428135) and
  Kepler-22 (Kepler ID 10593626) **(verify the identifiers)**.
* **Launches and orbital objects:** declared launch years (v1: 2026) and GCAT
  organisation codes **(verify the codes)**.
* **Space weather:** the rolling `alerts.json` window; history accumulates in
  the store.

## Credentials

No implemented source needs a credential. Space-Track would need an account
and is not implemented. Should a future source need a key it goes through the
existing `NOESIS_*` secret references of the source-pack runtime and is never
committed.

## Space-object registration, operators and re-entries (SO01, #2417)

The registration, operator-assertion and re-entry sources of the
`astronomy.space-object-registration` provider (UNOOSA Online Index and
registration documents, ESA DISCOS under its account terms, The Aerospace
Corporation re-entry database) are audited in
[`space-object-registration-audit.md`](space-object-registration-audit.md).
