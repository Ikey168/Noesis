# Humanitarian Response and Conflict Events: source-contract audit and bounded coverage (HR01)

Tracking: #2206 · delivery issue #2232 · recorded 2026-09-29.

This audit sets out, per source, what the Humanitarian bundle may acquire, how,
and on what terms. It was written without network access. Endpoints,
parameters, identifiers and terms come from the providers' published
documentation as the author knows it. **Every item marked _verify_ must be
checked against the live documentation, the live terms and a real response
before the first dated live run (HR14, #2293). No provider is `live` until that
run exists.** The machine-readable copy of these decisions is
`PROVIDER_CONTRACTS`, `LIVE_VERIFICATION` and `ACLED_DECISION` in
`src/ingestion/humanitarian_sources.py`; the source pack is
`config/source_packs/humanitarian.json` (connector `humanitarian`), and the MCP
tool `humanitarian_source_contracts` returns the same decisions.

These non-goals apply to every source:

* **No casualty estimation.** Counts (UCDP `best`/`low`/`high`, ACLED
  `fatalities`, figures inside reports) are stored exactly as the coder or
  publisher published them. Nothing is summed, averaged or reconciled.
* **No cross-coder deduplication.** A UCDP event and an ACLED event about the
  same incident stay two records with their own coding source; there is no
  merged "true" count.
* **No early-warning scores, forecasts, severity rankings or operational
  advice.** Queries return what was published, with citations.
* **No personal data about affected people** (see "Personal-data exposure").

## Access decisions

| Source | Delivers | Decision | Reason |
| --- | --- | --- | --- |
| ReliefWeb API (`api.reliefweb.int`, v2) | situation reports, appeals (reports whose `format` is Appeal / Flash Appeal) and disaster (crisis) entries | `unverified-live` | Documented public JSON API. Every request carries a declared `appname`; ReliefWeb asks integrators to register the appname (_verify_ whether pre-approval is now mandatory). No key. ReliefWeb republishes content from its sources, so each report keeps its own source organisations; bodies are referenced by URL and never mirrored. Attribution: "Source: ReliefWeb (OCHA)" plus the report's own sources |
| HDX CKAN API (`data.humdata.org/api/3/action`) | dataset metadata, resources and their update history; HXL hashtags from a resource's header rows | `unverified-live` | Documented CKAN action API, anonymous for public datasets. Licences vary per dataset (CC BY, CC BY-IGO, ODbL, "other"); the licence is stored per revision. HDX Connect (`is_requestdata_type`), private datasets and datasets whose licence is not an open reuse licence are metadata-only; their resources are never fetched |
| UCDP GED (final releases) through the UCDP API (`ucdpapi.pcr.uu.se/api/gedevents/<version>`) | georeferenced conflict events of a pinned GED version | `unverified-live` | Documented JSON API with `pagesize`/`page`. UCDP announced an access token for the API (_verify_: header name `x-ucdp-access-token`, whether it is mandatory); the source pack declares it as an optional secret. Data licence CC BY 4.0 with the UCDP dataset citation (_verify_) |
| UCDP Candidate Events (monthly releases, same API with the candidate version, e.g. `25.0.x`) | provisional events later replaced by a GED release | `unverified-live` | Same contract as GED. Candidate events are **provisional**: UCDP revises or drops them in the next final GED release. A dropped candidate becomes a `dropped-in-release` revision, never a deletion |
| ACLED API (`acleddata.com`) | coded political violence and protest events | **`declined`** (licence) | See "ACLED licence and access decision" below. No ACLED request is made. The source pack keeps a gated entry so the gap is visible, and every query reports ACLED as `not acquired (licence)` |

## Per-source contract

| Source | Endpoint and authentication | Rate limits and paging | Identifiers (stored as published) | Revision / release model | Retained evidence |
| --- | --- | --- | --- | --- | --- |
| ReliefWeb reports | `GET https://api.reliefweb.int/v2/reports?appname=<declared>`; filters `country.iso3`, `disaster.id`, `format.name`, `date.created` range (_verify_ the v2 query-string syntax) | documented ceiling of 1000 calls/day and 1000 entries per call (_verify_); `limit`/`offset`, at most `max_pages` × `limit` per selection | report `id`, `url`, `source[].shortname/name` (publishing organisations), `country[].iso3`, `primary_country.iso3`, `disaster[].id/glide`, `format[].name`, `language[].code` | a report is updated in place; `date.changed` identifies the update. A changed report (different content hash) is a new revision linked to its predecessor | JSON page per request (SHA-256 in the receipt); the report record holds a locator (`url`), never the body |
| ReliefWeb disasters | `GET https://api.reliefweb.int/v2/disasters?appname=<declared>`; filter `country.iso3` | as above | disaster `id`, `name`, `glide`, `status` (alert/current/past), `type[].name`, `country[].iso3` | `date.changed`; status transitions are revisions | JSON page per request |
| HDX | `GET https://data.humdata.org/api/3/action/package_search?fq=groups:<iso3 lower>&rows=&start=`; resource header rows via the resource `url` (first bytes only; _verify_ that the download host honours `Range`) | anonymous; no published hard limit (_verify_); `rows`/`start`, at most `max_pages` pages; at most `max_hxl_resources` header reads per run | dataset `id` and `name`, `organization.name`, resource `id`, `hash`, `last_modified`, `format` | `metadata_modified` of the package and each resource's `last_modified`/`hash`: any change of metadata, resource list or resource hash is a new dataset revision; prior revisions stay queryable | CKAN JSON per page; header rows (two lines at most) per HXL resource with their SHA-256 |
| UCDP GED / Candidate | `GET https://ucdpapi.pcr.uu.se/api/gedevents/<version>?pagesize=&page=&Country=<GW code>&StartDate=&EndDate=` | documented `pagesize` ceiling 1000 (_verify_); `TotalPages`/`NextPageUrl` | event `id`, `relid`, dataset version, `conflict_new_id`, `dyad_new_id`, `side_a`/`side_b` (labels as coded), `where_prec` (1–7), `date_prec` (1–5), `type_of_violence` (1–3), `best`/`low`/`high`, `latitude`/`longitude`, `adm_1`, `adm_2`, `country_id` (Gleditsch–Ward) | the dataset version is the release; each release of the same event `id` is an event revision listing the fields that changed | JSON page per request |
| ACLED | not used (declined) | — | — | — | — |

**Unavailable-access fallback.** A failed page (HTTP error, a host outside the
declared set, budget exhausted, schema drift) is recorded as stale provider
state with its failure code. The last revisions stay current. Nothing is marked
withdrawn, ended or dropped because of a failure: only a *complete* GED release
(every page read) for a declared country and window can mark a candidate event
as `dropped-in-release`.

**Live evidence.** Source-pack receipts state the execution; fixture replays
are `fixture`. Nothing is reported as live coverage until a dated run is
recorded in this directory (HR14).

## Bounded first coverage

One crisis, one country, a small set of admin-1 areas and a closed window, so
that every live run is small, repeatable and reviewable:

| Dimension | Selection | Justification |
| --- | --- | --- |
| Crisis | the Sudan conflict disaster entry on ReliefWeb (selected by `disaster.id`, pinned in the source pack) and the HDX `sdn` group | a long-running, well-documented crisis that all three sources cover, with GLIDE numbers and p-coded admin boundaries (COD-AB) on HDX |
| Country | Sudan (`SDN`; Gleditsch–Ward 625 for UCDP) | one country keeps admin-boundary vintages and identity review tractable |
| Admin areas | admin-1 Khartoum (`SD01`) and North Darfur (`SD02`) as the pinned area set for conflict-event area queries (_verify_ the p-codes against the COD-AB vintage) | two contrasting areas with admin-level and exact-location events |
| Window | a closed 12-month window declared per source in the source pack | bounded requests; a later window is a new selection, never implicit growth |
| Caps | ReliefWeb ≤ 100 reports and ≤ 20 disasters per run (`limit` 50, ≤ 2 pages); HDX ≤ 25 datasets (`rows` 25, 1 page) and ≤ 10 HXL header reads; UCDP ≤ 1000 events per version (`pagesize` 500, ≤ 2 pages); per-source `max_bytes` 5–10 MB and `timeout_ms` 30 s | explicit request and record ceilings enforced by the adapter and by the source-pack budgets |

Area queries are bounded too: at most a 5° × 5° bounding box (or one accepted
admin polygon) and at most 366 days per query.

The offline fixtures (`tests/fixtures/humanitarian/`, `tests/fixtures/source_packs/humanitarian-*.json`)
are **authored**, with synthetic identifiers, dates in 2098–2099 and fixture
actor labels, so they can never be mistaken for real reports or events.

## ACLED licence and access decision

**Decision: declined (recorded 2026-09-29).** No ACLED acquisition proceeds.

* Access requires a registered myACLED account and an API credential (an OAuth
  token flow since 2025, _verify_).
* ACLED's terms of use permit use with attribution but restrict
  redistribution of the data in raw or near-raw form and require a separate
  licence for commercial use (_verify_ the current wording). The Humanitarian
  bundle stores event records and exports them in evidence bundles, which is
  redistribution of near-raw event data.
* Without an organisational licence that covers storage and bundle export,
  acquiring ACLED would exceed the licence.

Consequences:

* `config/source_packs/humanitarian.json` keeps an `acled-events` entry whose
  `humanitarian.licence_decision.status` is `declined` and references this
  section; its fixture replays nothing and the adapter refuses to fetch
  (`licence_declined`).
* Every place, crisis and conflict-event query lists ACLED under
  `sources_declined` with the reason `not acquired (licence)`.
* The optional composition feature `acled` (default off) documents what an
  accepted decision would enable: the adapter in
  `src/ingestion/humanitarian_sources.py` then runs only with
  `licence_decision.status: accepted`, a decision reference and a
  `NOESIS_ACLED_ACCESS_TOKEN` credential, storing ACLED's own coding
  (`geo_precision`, `time_precision`, actors, `fatalities` as published) under
  coding source `acled`. ACLED and UCDP events are never merged.

Changing the decision needs a new dated section here, a source-pack version
bump and review.

## Personal-data exposure and minimisation

| Source | Exposure | Rule |
| --- | --- | --- |
| ReliefWeb | report bodies can name individuals (staff, victims) | bodies are never mirrored; records keep title, publishers, tags and URL only |
| HDX | datasets can hold survey micro-data or contact columns (`#contact+name`, `#contact+email`, `#contact+phone`) | only metadata and header rows are read, never values; contact and individual-level HXL tags are recorded as `personal-data-tag` and the resource is flagged; HDX Connect and private datasets are metadata-only |
| UCDP | `source_headline`, `source_original` and `where_description` are free text that can name individuals | dropped at parse time; only coded fields and the source-article reference are kept |
| ACLED | `notes` free text | declined; if ever accepted, `notes` is dropped at parse time |

**Minimisation rule.** No record carries data about affected individuals.
`src/kb/humanitarian_records.py` rejects any record that contains a
personal-data field (names of persons, contact details, dates of birth, identity
numbers, household or beneficiary identifiers) anywhere in the record.

## Source-pack entries and live-verification status

| Source id | Provider | `live_verification` |
| --- | --- | --- |
| `reliefweb-reports-sdn` | reliefweb | `unverified-live` |
| `reliefweb-disasters-sdn` | reliefweb | `unverified-live` |
| `hdx-sdn-datasets` | hdx | `unverified-live` |
| `ucdp-ged-sdn` | ucdp-ged | `unverified-live` |
| `ucdp-candidate-sdn` | ucdp-candidate | `unverified-live` |
| `acled-events` | acled | `declined` (licence) |
