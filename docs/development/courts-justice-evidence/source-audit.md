# Courts and justice: source-contract audit, minimisation decision and bounded coverage (CJ01)

Tracking: #2218 · delivery issue #2380 · recorded 2026-09-29.

This audit sets out, per source, what the Legal pack's court and
justice-statistics features (`courts`, `justice-statistics`) may acquire, how,
and on what terms. It was written without network access. Endpoints, fields
and terms come from the providers' published documentation as the author knows
it. **Every item marked _verify_ must be checked against the live
documentation, the live terms and a real response before the first dated live
run (CJ14, #2434). No source is `live` until that run exists.** The
machine-readable copy of these decisions is `PROVIDER_CONTRACTS`,
`MINIMISATION`, `BOUNDED_COVERAGE` and `LIVE_VERIFICATION` in
`src/ingestion/courts_justice_sources.py`; each source entry in
`config/source_packs/legal.json` (`legal-research` 1.4.0, earlier sources
verbatim) states `courts_justice.live_verification: unverified-live`, and the
MCP tool `courts_justice_source_contracts` returns the same decisions.

Non-goals for every source: no outcome prediction or win/loss label, no
recidivism or risk scoring, no neighbourhood safety rating, no ranking of
places or countries, no personal profile of a private individual and no legal
advice.

## Access decisions

| Source (source-pack id) | Provider | Delivers | Decision | Reason |
| --- | --- | --- | --- | --- |
| `courtlistener-dockets` | CourtListener REST API v4 (Free Law Project) | dockets, RECAP docket entries, parties | `unverified-live` | Documented API with a user token; the docket-entries and parties endpoints may need an extra RECAP entitlement (_verify_) - without it the unit fails `authentication_failed` and nothing partial is stored |
| `courtlistener-opinions` | CourtListener REST API v4 | opinion clusters and opinions | `unverified-live` | Documented API with a user token; `sub_opinions`, `opinions_cited`, `citations` field shapes are _verify_ |
| `fbi-cde-summarized` | FBI Crime Data Explorer API (api.usa.gov) | summarized offence counts, rates, populations, participated populations | `unverified-live` | api.data.gov key; response keys (`offenses.actuals`, `populations.participated_population`, `cde_properties.last_refresh_date`) and whether summarized counts are SRS or NIBRS-converted are _verify_; offence definitions are cited from the UCR handbook as declared, never from the API |
| `police-uk-street-crime` | data.police.uk API | street-level crimes and outcomes, aggregated to counts per category and outcome | `unverified-live` | Anonymous documented API, Open Government Licence; fixture-verified |
| `eurostat-crime-iccs` | Eurostat dissemination API (JSON-stat 2.0) + ESMS page | police-recorded offences by ICCS, status flags, footnotes, ESMS comparability section | `unverified-live` | Anonymous documented API reached through the existing `EurostatConnector`; the ESMS HTML section ids are operator-declared and _verify_ |

## Per-source contract

| Source | Endpoints (relative to the declared endpoint) | Key handling | Rate limits and pagination | Identifiers (stored as published) | Revision model |
| --- | --- | --- | --- | --- | --- |
| CourtListener dockets | `/dockets/{id}/`, `/docket-entries/?docket={id}&page_size=100`, `/parties/?docket={id}&page_size=100` | `NOESIS_COURTLISTENER_API_TOKEN` as `Authorization: Token ...`; never in a URL, receipt or record | 5,000 requests/hour per user (_verify_); a list with a `next` link or a larger `count` is `budget_exhausted`, never truncated | docket id, court id, docket number, PACER case id, entry number, RECAP document id | `date_modified`; a changed docket is a new revision, earlier revisions stay queryable |
| CourtListener opinions | `/clusters/{id}/`, `/opinions/?cluster={id}&page_size=100` | as above | as above | cluster id, opinion id, reporter citations (volume, reporter, page) | `date_modified` |
| FBI CDE | `/summarized/{state\|agency}/{code}/{offense}?from=MM-YYYY&to=MM-YYYY` | `NOESIS_FBI_CDE_API_KEY` as `X-Api-Key` | api.data.gov default 1,000/hour (_verify_); one response per unit | state postal code, agency ORI, offence, month | `cde_properties.last_refresh_date` is the vintage; a re-release with changed figures is a new vintage |
| data.police.uk | `/crime-last-updated`, `/crime-categories?date=`, `/crimes-street/{category}?date=&poly=` | none | 15 req/s, burst 30 (_verify_); more than 10,000 crimes answers HTTP 503, which fails the unit | force, neighbourhood, category url, month | `crime-last-updated` is the monthly release (vintage); a revised month is a new vintage |
| Eurostat | `/api/dissemination/statistics/1.0/data/{dataset}?format=JSON&geo=..&iccs=..`, `/cache/metadata/en/crim_esms.htm` | none | fair use (_verify_); a cube above 5,000 cells fails the unit | dataset code, ICCS code, unit, GEO, period | the cube's `updated` stamp is the vintage |

**Licences and attribution.** CourtListener: court records are public records;
the API terms ask for attribution and forbid scraping the web site (_verify_
the current API terms); RECAP documents are **linked, never mirrored** (only
the document number, description, availability flag, page count and link are
kept). FBI CDE: US government work, public domain; the FBI UCR Program is
credited. data.police.uk: Open Government Licence v3.0 with the attribution
statement `OGL_ATTRIBUTION`. Eurostat: reuse authorised under Commission
Decision 2011/833/EU with attribution.

**Unavailable-access fallback.** A failed unit (HTTP error, redirect to another
host, missing secret, schema drift, a list longer than one page) fails the run
for that source with its code; earlier revisions and vintages stay current and
nothing is marked closed, decided or revised because of a failure.

## Minimisation decision

Dockets name private individuals. The decision, enforced at ingestion
(`courts_justice_sources._parties`) and again at projection (a natural-person
party carrying a name or key is refused with `minimisation_violation`):

* **Every party**: its role(s) as published (`party_types[].name`), a party
  type flag (`organisation` | `natural_person`) and a docket-scoped ordinal.
* **Organisations** (a published name carrying a legal-form or public-body
  token - Inc., LLC, Corp., Ltd, Bank, Department, Agency, County, City of,
  State of, United States, ...): the name as published and a docket-scoped
  party key, which may be *proposed* against canonical entities (CJ07).
* **Natural persons** - and every party whose type is unclear - are always
  **pseudonymised**: the record stores `natural person N (Role)` and nothing
  else. Their names, CourtListener party ids, addresses, dates of birth,
  `extra_info`, attorneys and attorney contact details are **never
  persisted**.
* Case captions and docket-entry descriptions are court-published text; they
  are kept verbatim as the record and are never parsed into person records.
* No personal profile is assembled: natural persons are never matched to
  entities, aggregated across dockets, used as a query key or used as a
  subscription target.
* data.police.uk: street-level crimes are aggregated to counts per category and
  outcome category at ingestion; locations, streets and persistent crime IDs
  are not stored, and the publisher's location anonymisation (snap points) is
  recorded as a coverage note - no attempt is made to recover a location.

## Bounded first coverage

* **Dockets:** the declared CourtListener dockets (at most 20 per source) of the
  declared federal courts; fixtures: one fictional D.D.C. docket (`70001`,
  filed 2099) and two opinion clusters (D.D.C. `80001`, D.C. Cir. `80002`).
  Seed provisions: **42 U.S.C. § 1983** and **15 U.S.C. § 45**. Organisational
  parties only as published. A live run names at most 20 dockets per source
  from one court and a one-year filing window.
* **FBI CDE:** the declared states or agency ORIs, UCR Part I offences and a
  window of at most 12 months per unit; fixtures: placeholder state `EX` and
  agency ORI `EX0000100`, burglary, January-March 2098.
* **data.police.uk:** the declared force neighbourhoods (as bounded polygons)
  and months; fixtures: one fictional neighbourhood (`example-force/EX01`),
  January 2099.
* **Eurostat:** the declared crime datasets (`crim_off_cat`), ICCS codes and
  countries; fixtures: DE and FR, ICCS0401 (robbery), 2096-2098.

No selection implies coverage of a court, a statute, a statistics programme or
a country.

## LIVE_VERIFICATION

| Provider | Intended status after CJ14 | Current status |
| --- | --- | --- |
| courtlistener | `verified-live` once a dated run with a token (and RECAP entitlement) is recorded | `unverified-live` |
| fbi-cde | `verified-live` once a dated run with an api.data.gov key is recorded | `unverified-live` |
| police-uk | `verified-live` after a dated anonymous run | `unverified-live` |
| eurostat | `verified-live` after a dated anonymous run and a check of the ESMS section ids | `unverified-live` |

Revisions acquired live while a provider is `unverified-live` are withheld from
subscription notifications (CJ11) until the dated live run exists.

## Mapping onto the Legal work model

| Needed | Existing model | Change (CJ02) |
| --- | --- | --- |
| Docket | `legal_works` | `work_kind` `docket`, provider `courtlistener`, identifiers docket id, court id, docket number, PACER case id; title = case name as published |
| Opinion cluster | `legal_works` | `work_kind` `decision`, identifiers cluster id, reporter citations, opinion ids |
| Docket / opinion revisions | `legal_expressions`, `legal_versions`, `legal_passages`, `legal_facts` | one version per acquired revision; opinion paragraphs as passages with `opinion_id`/`paragraph` locators; decision date as a `decision` fact |
| Entries, parties, dispositions | none | `legal_docket_revisions`, `legal_docket_entries`, `legal_docket_parties`, `legal_opinion_revisions`, `legal_opinions` beside the version they belong to (`src/kb/legal_dockets.py`) |
| Justice statistics | none | `justice_definitions`, `justice_vintages`, `justice_observations`, `justice_coverage_notes`, `justice_comparability_notes` (`src/kb/justice_statistics.py`) |

Existing CELLAR, RII, Berlin, sanctions and federal-statute records serialise
exactly as before.
