# Animal and veterinary health: source-contract audit and bounded coverage (AH01)

Tracking: wave 2 tracker #2736 · recorded 2026-10-03.

This audit sets out, per source, what the Clinical Evidence bundle's proposed
`clinical.animal-health` provider (subdomain `animal-health`) may acquire,
how, and on what terms. No per-track tracker or delivery issue exists yet;
both are opened once this audit names a surviving source, which it does (EFSA
via its Knowledge Junction records; see the access decisions). **It was
written without network access: the publishers' hosts (`wahis.woah.org`,
`www.woah.org`, `www.efsa.europa.eu`, `zenodo.org`, `empres-i.apps.fao.org`)
are blocked by this runtime's egress proxy (CONNECT refused with 403, verified
2026-10-03), so terms, endpoints and field names were not re-verified live.**
They come from the gap table's candidates and the publishers' documentation as
the author knows it. Every item marked _verify_ must be checked against the
live pages, the live terms and a real response before the track's first dated
live run (its "Validate live coverage" issue). No source is `live` until that
run exists.

The machine-readable copy of these decisions does **not** exist yet.
`PROVIDER_CONTRACTS`, `BOUNDED_COVERAGE`, `CAPS`, `EXCLUSIONS` and
`LIVE_VERIFICATION` will be added in `src/ingestion/animal_health_sources.py`
by the track's acquisition issues, merged into the clinical
`PROVIDER_CONTRACTS` of `src/ingestion/clinical_providers.py`, and must match
this audit; a difference is resolved by changing this audit first.

Non-goals for every source: no veterinary, medical or biosecurity advice; no
risk scores, spread or introduction forecasts, nowcasts or "hotspot" maps; no
judgement on trade restrictions, zoning, regionalisation or disease-free
status (an official status is shown only as the publisher states it); no
blending of WOAH, EFSA and FAO figures into one series; no rates, prevalences
or totals computed by Noesis. Values are kept as each publisher released them,
side by side.

## Access decisions

| Source (proposed source-pack id) | Publisher | Delivers | Access | Decision |
| --- | --- | --- | --- | --- |
| `clinical-animal-health-efsa-kj` | EFSA, data records in the EFSA Knowledge Junction community on Zenodo | aggregated surveillance and outbreak data that accompany EFSA scientific outputs (e.g. ASF epidemiological reports, avian influenza overviews, EU One Health zoonoses data), per Member State and published region and period | documented Zenodo REST API, one declared record id per document, files fetched with checksums through the existing `ZenodoClient` (`record`, `acquire` in `src/ingestion/zenodo.py`; origin-locked to `zenodo.org`) | `unverified-live` |
| `clinical-animal-health-efsa-dashboards` | EFSA, data warehouse dashboards and interactive reports | the same domains, interactively | interactive dashboards; no documented public API identified (_verify_) | `not-implemented` |
| `clinical-animal-health-woah-wahis` | WOAH, World Animal Health Information System (WAHIS) | events (immediate notifications, follow-up and final reports) with outbreaks, species and counts; six-monthly reports | public web interface with manual extraction; a JSON backend used by the interface is not documented for reuse (_verify_ whether WOAH now offers a documented API or bulk download) | `not-implemented` |
| `clinical-animal-health-fao-empres-i` | FAO, EMPRES-i+ Global Animal Disease Information System | outbreak events with disease, serotype, species, place, dates, status and reporting source | web application; event downloads require a registered FAO account (_verify_); no documented token API identified (_verify_) | `gated-not-granted` |

**WAHIS** is `not-implemented` on the precedent of SurvStat and the ECDC Atlas
(`docs/roadmaps/clinical-surveillance-source-audit.md`): an undocumented
backend is never scraped. WOAH's terms of use also restrict reproduction for
commercial purposes without permission (_verify_ the current WAHIS terms and
whether they cover redistribution in exported evidence bundles). Reopening it
needs a documented API or bulk file, or a decision record for an
operator-supplied extraction import with verified reuse terms; until then
answers report WAHIS as unavailable, not empty.

**EMPRES-i+** is `gated-not-granted`: no account is held and none was
requested. If a documented token API is confirmed and access is granted, the
credential is read from `NOESIS_FAO_EMPRES_I_TOKEN`, sent only in a request
header and never written to a record, receipt or log; a browser login session
is never automated. FAO terms (CC BY-NC-SA 3.0 IGO for most FAO databases,
_verify_ for EMPRES-i+) and the redistribution of events FAO took from WOAH or
national authorities must be checked before that.

The EU Animal Disease Information System (ADIS, European Commission) is not a
gap-table candidate; it is noted as a possible later source, not audited here.

## Per-source contract

| Source | Endpoints | Authentication | Licence and redistribution | Rate limits | Revision model: updates, closures, removals |
| --- | --- | --- | --- | --- | --- |
| EFSA KJ (Zenodo) | `https://zenodo.org/api/records/{id}` and the record's `files` links (_verify_ the record ids; community `efsa-kj`, _verify_) | none | per record, as the Zenodo record states (CC BY 4.0 for most EFSA data records, _verify_ each); cite EFSA, the output's EFSA Journal DOI and the record DOI | Zenodo API limits (about 60 requests per minute for anonymous clients, _verify_); one record request plus declared files per run | a Zenodo version is a separate record id and DOI under one concept DOI: each declared version is one vintage, a newer version is a new vintage, never an overwrite; file checksums identify content; a record that becomes restricted or deleted fails with `restricted` and stored vintages stay current |
| EFSA dashboards | n/a | n/a | EFSA legal notice (_verify_) | n/a | n/a |
| WOAH WAHIS | `https://wahis.woah.org/` (interface only) | n/a | WOAH terms (_verify_) | n/a | for a later import: event id and report sequence (immediate notification, follow-up n, final report) are the revision chain; a new outbreak or changed count is a new revision; an event marked resolved or closed by a final report is a status revision, never a deletion; a withdrawn report is `withdrawn_by_source` |
| FAO EMPRES-i+ | `https://empres-i.apps.fao.org/` (_verify_) | FAO account; `NOESIS_FAO_EMPRES_I_TOKEN` reserved, not granted | FAO terms (_verify_) | _verify_ | event id is the key; status (`confirmed`, `denied`, ... as published, _verify_) and report-date changes are revisions; the original reporting source is kept per event |

**Unavailable-access fallback.** A failed document (HTTP error, redirect off
`zenodo.org`, checksum mismatch, schema drift, an undeclared column, a file
larger than the cap) fails that source's run with its code and a receipt;
earlier vintages stay current and nothing is marked removed. The provider
reports the source `stale`.

## Definitions and comparability recorded per series

- **Disease and agent** as the publisher names it (e.g. "African swine fever",
  "HPAI H5N1", with subtype or serotype where published); aligned to MeSH and
  WOAH-listed disease names through reviewed crosswalks only, unmapped terms
  are gaps (`src/kb/clinical_terms.py` pattern).
- **Animal population:** domestic vs wild, species or species group and
  epidemiological unit type (holding, backyard, flock, wild animal) as
  published; counts of outbreaks, cases, deaths, animals killed or susceptible
  are distinct measures and never added together.
- **Dates:** start or suspicion, confirmation and reporting date as separate
  fields, as each source states them; a missing date is unknown.
- **Places:** the published admin level only (country, NUTS or ADIS region,
  first-level division), resolved by code through the existing surveillance
  place path (`src/kb/surveillance_places.py`).
- **Comparability:** EFSA aggregates, WAHIS events and EMPRES-i events count
  different things (outbreaks vs cases, official vs mixed sources, different
  reporting lags); they are never reconciled or deduplicated into one count.

## Data minimisation decision

Outbreak data can identify a farm or holding and, through it, a person.
Decision:

- **Stored:** published aggregates and event-level fields at the published
  admin level: event or report id, disease and agent, species group and unit
  type, measures, dates, status, place code and name at admin level 1 or the
  published region, citation.
- **Excluded:** latitude and longitude even when published, locality, village
  or farm names below the published admin region, holding or premises
  identifiers and registration numbers, owner or keeper names and contacts,
  veterinarian or laboratory staff names, and free-text fields that may carry
  them. Parsers never copy these fields and record only how many were dropped;
  the track's record store refuses a record that still carries one at write
  time.
- **Retention:** vintages kept for provenance; nothing personal is held.
- **Who may query:** the existing clinical scopes `knowledge:clinical:read`,
  `:write` and `:review` (`src/kb/clinical_records.py`).

## Bounded first coverage

| Source | Places | Diseases and documents | Periods | Caps |
| --- | --- | --- | --- | --- |
| EFSA KJ | Germany and Poland, rows as published per country or region | African swine fever: the data record of one EFSA annual ASF epidemiological report; HPAI: the data record of one EFSA avian influenza overview (_verify_ that both exist as machine-readable files; if not, the fallback first document is one EU One Health zoonoses data record, _verify_) | ASF 2022-01-01 to 2023-12-31; HPAI one season, 2022-10-01 to 2023-09-30 | 2 records, 5 files per record, 20 MB per file (the `ZenodoClient` default), 10,000 stored rows |

Justification: two diseases with different hosts (pigs and wild boar; poultry
and wild birds) and two countries both affected in the window are the
smallest selection that exercises domestic vs wild measures, regional places
and Zenodo versioning; aggregated EFSA records carry no holding-level detail.
A larger result fails `budget_exhausted`, never truncated. Each further
disease, country or record is a source-pack version bump. Links, by explicit
citation only: `clinical.surveillance` (human zoonotic cases by shared
condition term) and `agrifood.core` (FAOSTAT livestock series by place and
species, through the `src/kb/agrifood_links.py` citation pattern).

## Live verification

| Source | Status | Checked | Evidence |
| --- | --- | --- | --- |
| EFSA KJ (Zenodo) | `unverified-live` | - | none yet; fixtures to be authored |

`clinical-animal-health-woah-wahis` and
`clinical-animal-health-efsa-dashboards` stay `not-implemented` and
`clinical-animal-health-fao-empres-i` `gated-not-granted`; none has a live
check. Fixtures will be authored, not captured: synthetic values and fictional
record ids for reference periods 2094-2097 and release dates in 2098-2099, so
nothing can be mistaken for a published figure.
