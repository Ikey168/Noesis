# Education statistics: source audit and bounded provider coverage (ED01)

Tracking: #2227 · delivery issue #2374 · recorded 2026-09-30.

This audit sets out, per source, what the Science bundle's optional
`education-statistics` feature may acquire, how, and on what terms. It was
written without network access. Endpoints, parameters, column names and terms
come from the providers' published documentation as the author knows it.
**Every item marked _verify_ must be checked against the live documentation, the
live terms and a real response before the first dated live run (ED14, #2444).
No provider is `live` until that run exists.** The machine-readable copy of
these decisions is `PROVIDER_CONTRACTS`, `LIVE_VERIFICATION` and
`BOUNDED_COVERAGE` in `src/ingestion/education_sources.py`; the MCP tool
`education_source_contracts` returns them, and each source entry in
`config/source_packs/scientific.json` (pack `primary-scientific-evidence`
1.2.0) carries its `education_statistics.live_verification` status. Coverage is
recorded against the existing Science pack; no new pack is planned.

These non-goals apply to every source: values are stored as each publisher
released them, with the publisher's definition, unit, currency, scale,
reference period and release vintage. Values of different publishers for the
same concept are shown side by side and never merged, averaged or harmonised;
nothing is converted between currencies or rounded; missing, not applicable,
confidential and suppressed values keep the publisher's own code and never
become zero; no per-student, per-staff or per-capita ratio is derived.
**University rankings, league tables, quality or performance scores and
composite indices are out of scope for every source**: where a source
publishes such fields (derived indicators in ETER, composite tables at the
OECD) they are never selected, acquired or stored, and the record contract
refuses `rank`, `score`, `composite_index` and similar keys.

## Access decisions

| Source | Delivers | Decision (`LIVE_VERIFICATION`) | Reason |
| --- | --- | --- | --- |
| US IPEDS (nces.ed.gov) | institution statistics by UNITID: directory, fall enrolment, completions, staff, finance | `unverified-live` | Public complete data files (zip with one CSV) without authentication. File names (`EF{year}A`, `C{year}_A`, `S{year}_IS`, `F{yy}{yy}_F1A`, `HD{year}`), members (the final release adds `_rv` members), variable codes and imputation flag codes are _verify_ |
| ETER (www.eter-project.com) | European higher-education institution profiles and statistics by ETER ID | `unverified-live` | Published exports; whether bulk export or the API needs registration, the export path, column names, the delimiter, the special codes and the licence text are _verify_ |
| UNESCO UIS Data API (api.uis.unesco.org) | country education indicators by UIS indicator code | `unverified-live` | Documented API without authentication (_verify_). Path `/api/public/data/indicators`, parameters (`indicator`, `geoUnit`, `start`, `end`, `version`, `indicatorMetadata`, `footnotes`), qualifier codes and the footnote field are _verify_ |
| OECD Education at a Glance (sdmx.oecd.org) | EAG indicators by country and ISCED level | `unverified-live` | OECD SDMX REST API without authentication, read through the existing SDMX connector (`SDMXConnector('OECD').csv_url` / `parse_csv`). EAG dataflow ids and versions, dimension order and attribute names are _verify_ |
| Eurostat R&D statistics (ec.europa.eu) | GERD and R&D personnel by sector of performance | `unverified-live` | Eurostat dissemination API (the API `EurostatConnector` reads) through the SDMX connector's ESTAT SDMX-CSV path. Dataset codes (`rd_e_gerdtot`, `rd_p_perssci`), key order and flag letters are _verify_ |

## Per-source contract

| Source | Endpoint or bulk file, format | Identifiers | Licence and attribution | Rate limits and bounds |
| --- | --- | --- | --- | --- |
| IPEDS | `https://nces.ed.gov/ipeds/datacenter/data/{FILE}.zip`, one CSV member per file (_verify_) | `UNITID` (six digits); `OPEID` from the directory file; country US | US federal government work (public domain, 17 U.S.C. 105); attribution to NCES IPEDS requested | not documented; one download per declared file; `max_results` institutions per file, `max_bytes` per file, `max_pages` files per run |
| ETER | declared CSV export on `www.eter-project.com` (_verify_ the path and delimiter) | ETER ID; national identifier; identifiers ETER publishes (a ROR or Wikidata column, _verify_); country as ETER publishes it (EL, UK) | ETER terms of use, reuse with attribution (_verify_ the licence text) | not documented; one export per reference year |
| UNESCO UIS | `GET https://api.uis.unesco.org/api/public/data/indicators?indicator&geoUnit&start&end&version&indicatorMetadata=true&footnotes=true`, JSON (_verify_) | UIS indicator codes (ISCED 2011 level in the code); `geoUnit` ISO 3166-1 alpha-3 | CC BY-SA 3.0 IGO with attribution to UIS (_verify_) | a record limit per call (about 100 000, _verify_); one call per declared document |
| OECD EAG | `GET https://sdmx.oecd.org/public/rest/data/{agency},{dataflow},{version}/{key}?format=csvfile`, SDMX-CSV (_verify_) | `REF_AREA` ISO alpha-3; `EDUCATION_LEV` ISCED 2011 | OECD terms and conditions, reuse with attribution (_verify_) | OECD fair-use limit (about 60 queries an hour, _verify_); one call per declared key |
| Eurostat R&D | `GET https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/{dataset}/{key}?format=SDMX-CSV` with `LAST UPDATE` and `OBS_FLAG` columns (_verify_) | Eurostat GEO codes; `sectperf` (BES, GOV, HES, PNP, TOTAL) | Eurostat reuse policy (Commission Decision 2011/833/EU), attribution | no published per-user limit (_verify_); one call per declared key |

## Release vintages and revision behaviour

| Source | What dates a vintage | Provisional, final and revised releases |
| --- | --- | --- |
| IPEDS | the declared release date of the provisional or final file (`release.stage`) | a provisional release comes first and the final release about a year later; they are distinct vintages and the final one supersedes the provisional one without deleting it; a final zip carries the revised `_rv` member, which the declared document names |
| ETER | the declared data-release date | each annual data release is a vintage; a later release that changes a value adds a revision |
| UNESCO UIS | the indicator metadata `lastDataUpdate`, else the declared date of the data version (`version`) | each UIS data version is a vintage; revised values add revisions; qualifiers (`UIS_EST`, `NAT_EST`, _verify_), magnitudes and footnotes are stored as comparability notes |
| OECD EAG | the declared edition release date (`LAST UPDATE` when the response states it) | each Education at a Glance edition is a vintage; values revised between editions add revisions; `OBS_STATUS` (M missing, L not collected, B break, E estimated, _verify_) and declared note attributes (`COMMENT_OBS`) are comparability notes |
| Eurostat R&D | the dataset's `LAST UPDATE` stamp, else a declared release, else the retrieval time (labelled) | each dataset update is a vintage; revised observations add revisions; flags `p`, `e`, `b`, `d`, `u`, `s`, `r` are comparability notes, `c` and `z` decide the status |

Missing and special values are stored as published: IPEDS imputation flags
(`X`-prefixed columns: R reported, A not applicable, G/J/K/L/N/P imputations,
Z implied zero, _verify_) and declared suppression codes; ETER special codes
(`m` missing, `a` not applicable, `x`/`xc`/`xr` included elsewhere, `c`
confidential, `nc` not calculated, _verify_); OECD `OBS_STATUS`; Eurostat
`OBS_FLAG`.

## Bounded coverage

| Source | Indicators | Institutions or countries | Years |
| --- | --- | --- | --- |
| IPEDS | directory (HD), fall enrolment `EFTOTLT`, completions `CTOTALT`, instructional staff `SISTOTL`, finance total revenues `F1D01` (USD) | a declared UNITID sample (at most `max_results` per file) | the two most recent survey years, provisional and final releases |
| ETER | total ISCED 5-7 students, academic staff FTE, total current expenditure (EUR) | a declared ETER ID sample in DE and FR | the two most recent reference years |
| UNESCO UIS | tertiary gross enrolment ratio, tertiary graduates, tertiary teachers, government expenditure on tertiary education as % of GDP, GERD as % of GDP | DEU, FRA, USA | the two most recent years |
| OECD EAG | expenditure on tertiary educational institutions as % of GDP | DEU, FRA, USA | the two most recent editions |
| Eurostat R&D | GERD in the higher-education sector (`rd_e_gerdtot`), R&D personnel in the higher-education sector (`rd_p_perssci`) | DE, FR | the two most recent years |

The live verification (ED14) uses exactly this coverage. No record set implies
complete coverage of any provider.

## Identity and links

Institution identity uses ROR (`src/ingestion/ror.py`) as the backbone: a ROR
id published by the source, or an identifier published in both the ROR record
and the source record, is an exact match; a name and location match is a
candidate that a reviewer must accept (`src/kb/education_identity.py`, ED08).
Links to Science and Funding records rest on a confirmed ROR match or an
identifier cited in the records, never on names or keywords (ED09).

## Gap against the existing stores

The Economics vintage stores key a series by one geography and have no place
for an institution id, an ISCED level or a release stage, so education
statistics get namespace-scoped `edu_*` tables in the same release, series,
vintage and observation shape (`src/kb/education_statistics.py`), owned by the
new `science.education-statistics` provider of the existing Science pack.
