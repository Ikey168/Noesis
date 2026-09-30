# Agriculture and Food Systems: source-contract audit and bounded provider coverage (AF01)

Tracking: #2213 · delivery issue #2331 · recorded 2026-09-29.

This audit sets out, per source, what the Agriculture and Food Systems pack may
acquire, how, and on what terms. It was written without network access.
Endpoints, field names, codes and terms come from the providers' public
documentation as the author knows it. **Every item marked _verify_ must be
checked against the live page and a published response before the first dated
live run (AF13, #2370). No provider is `live` until that run exists.**

The machine-readable copy of these decisions is `PROVIDER_CONTRACTS`,
`LIVE_VERIFICATION`, `SECRET_REFS` and `EXCLUSIONS` in
`src/ingestion/agrifood_sources.py`, and `FLAG_VOCABULARIES` in
`src/kb/agrifood_records.py`; the source-pack declaration is
`config/source_packs/agrifood.json` (`agrifood` 1.0.0). The MCP tool
`agrifood_source_contracts` returns them and `agrifood_bundle_status` reports
readiness per provider.

These non-goals apply to every source:

- No yield, production or price forecasting and no projection by the pack.
  Publisher forecasts (NASS forecast reference periods, Eurostat `f` flags) and
  USDA PSD projections are labelled as the publisher's (`estimate_type`
  `publisher-forecast` / `publisher-projection` / `publisher-estimate`).
- No food-security indicator or score is computed; a food-balance element is
  quoted as published.
- Sources are never blended: every series belongs to one publisher, dataset,
  commodity code, place code, element, unit and period type. Figures are never
  summed or averaged across sources or across differently defined commodities.
- Marketing years and calendar years are explicit and never converted.
- Withheld, confidential, missing and not-applicable values stay text with
  their flag; nothing is imputed.
- A revised figure is a new vintage beside the prior one; nothing is
  overwritten.

## Access decisions

| Source | Delivers | Decision | Reason |
| --- | --- | --- | --- |
| FAOSTAT API | QCL production, area harvested, yield; PP producer prices; FBS food balance elements | `unverified-live` | `https://faostatservices.fao.org/api/v1/en/data/{domain}` with `area`, `item`, `element`, `year`, `show_flags`, `show_notes` and `output_type=objects`, and `/en/groupsanddomains` for the domain `date_update` (paths, parameters and field names _verify_). No key for the documented public endpoints; FAO has announced token-based API access, so a token requirement must be checked before a live run (it would be held as a `NOESIS_` secret reference). The bulk CSV downloads (`bulks-faostat.fao.org`) are the documented fallback; the full bulk archive is never mirrored |
| USDA NASS Quick Stats | production, yield, area harvested/planted, price received at national, state (FIPS) and county (FIPS) level | `unverified-live` | Documented API `https://quickstats.nass.usda.gov/api/api_GET/` with explicit `short_desc`, `agg_level_desc`, `state_fips_code`, `county_code`, `year__GE`/`year__LE`, `format=JSON`. Requires an API key sent as the `key` query parameter; held only as the secret reference `NOESIS_NASS_API_KEY` (auth `required-secret`). Stored citation URLs and receipts are built without the key. An empty answer (HTTP 400 "no data") is a `no_data` selection outcome |
| USDA FAS PSD Online | supply and distribution attributes per marketing year | `unverified-live` | PSD OpenData API `https://apps.fas.usda.gov/OpenData/api/psd/commodity/{commodity}/country/{country}/year/{market_year}` with `/api/psd/commodityAttributes` and `/api/psd/unitsOfMeasure` (paths and the `API_KEY` header _verify_). Key held as `NOESIS_FAS_API_KEY` |
| Eurostat agriculture | crop production (`apro_cpsh1`: area, harvested production, yield) and selling prices of crop products (`apri_ap_crpouta`) | `unverified-live` | Through the existing SDMX connector (`SDMXConnector("ESTAT").csv_url` / `parse_csv`, SDMX-CSV with the `LAST UPDATE` column and `OBS_FLAG`); no key. Dimension codes (`C1110`, `AR`, `PR_HU_EU`, `YI_HU_EU`, `01110000`) and the price unit are _verify_ |
| EU Agri-food data portal | weekly cereal market prices | `unverified-live` | `https://ec.europa.eu/agrifood/api/{sector}/prices` with `memberStateCodes`, `productCodes`, `beginDate`, `endDate` (paths, parameters and product codes `BLTPAN`, `MAI` _verify_). No key |

**Unavailable-access fallback.** A payload is not stored when any of these
happens: an HTTP error other than the provider's documented "no data" answer, a
redirect to another host, schema drift, a response over the byte ceiling, a
selection with more statements than the run's result budget, or a different
figure republished under an unchanged release (`vintage_conflict`). The source
run is recorded as failed and monitors never evaluate it. A selection the
provider has no data for is a `no_data` outcome in the page receipt; queries
report the commodity and place as having **none on record**.

## Per-source contract

| Source | Keys | Release and revision behaviour | Cadence | Paging and limits | Licence and attribution |
| --- | --- | --- | --- | --- | --- |
| FAOSTAT | domain, area code (FAO), item code (with CPC as published), element code, year | A domain is updated as a whole; its `date_update` is the release. Each update is a new vintage of every series it carries; earlier vintages stay queryable. Recent years are often estimated or imputed and later replaced by official figures - both stay | a few times a year per domain | one selection (area, item, elements, years) per page; no published rate limit | CC BY 4.0 for statistical data (_verify_ the current FAO terms of use). Attribution: "Source: FAO. FAOSTAT" with the access date |
| NASS Quick Stats | `short_desc`, geography (national / state FIPS / county FIPS), year and `reference_period_desc`, `load_time` | Estimates are revised in place (annual summaries, census benchmarking); `load_time` is the vintage, so a revised figure is a new vintage beside the prior one. Forecast reference periods (e.g. `YEAR - AUG FORECAST`) are NASS's own forecasts and are labelled so | NASS release calendar | at most 50,000 rows per request; one `short_desc` and geography per page | U.S. Government work, public domain. Attribution: "Source: USDA NASS Quick Stats" |
| FAS PSD | commodity code, country code, marketing year, attribute id, release month | Monthly releases aligned with WASDE; `calendarYear` + `month` of a row is its release. Every monthly release is stored as a new vintage | monthly | one commodity, country and market-year set per page (one request per market year plus the attribute and unit lists) | U.S. Government work, public domain. Attribution: "Source: USDA FAS PSD Online" |
| Eurostat | dataset, dimension key, period | A dataset is republished as a whole; its `LAST UPDATE` is the vintage | on dataset update | one dataset key per page | Eurostat copyright notice: reuse with acknowledgement (_verify_). Attribution: "Source: Eurostat" |
| Agri-food portal | product, member state, market (with stage), week | No release stamp is published; a changed answer is a new vintage dated by its first retrieval | weekly | one product and member state window per page | Commission reuse policy (Decision 2011/833/EU), reuse with acknowledgement (_verify_) |

## Flag vocabularies and mapping

Flags are stored **verbatim** (code, label, note) with the vocabulary name; the
pack adds classes beside them as an index (`FLAG_CLASSES`), so the mapping is
lossless. An unknown code keeps its text with the class `unknown`.

| Vocabulary | Codes (as published) | Classes |
| --- | --- | --- |
| `faostat-flags` | A Official figure, B Time series break, E Estimated value, I Imputed value, M Missing value (data cannot exist, not applicable), O Missing value, P Provisional value, T Unofficial figure, X Figure from international organizations; the `Note` column is kept | official, series-break, estimated, imputed, missing + not-applicable, missing, provisional, unofficial, international-organization |
| `eurostat-obs-flags` | b, c, d, e, f, n, p, r, s, u, z; combined letters (`ep`) are kept as published and each letter classified; `:` is a missing value | series-break, confidential (value withheld), definition-differs, estimated, forecast (publisher forecast), not-significant, provisional, revised, estimated (Eurostat estimate), low-reliability, not-applicable |
| `nass-value-codes` | (D) withheld to avoid disclosing individual operations, (S) insufficient reports, (NA) not available, (X) not applicable, (Z) less than half the rounding unit, (H)/(L) coefficient-of-variation markers; survey vs census is the series `program` | withheld, withheld, missing, not-applicable, below-rounding, low-reliability / official |
| `psd-release-convention` | PSD carries no per-figure flag. By the WASDE convention the newest marketing year of a release is a USDA projection and the one before it a USDA estimate (_verify_ against the WASDE "Proj." / "Est." labels) | projection, estimated |
| `agri-food-portal` | no per-figure flags | not-flagged |

## Bounded coverage (acquired by `config/source_packs/agrifood.json`)

| Source | Commodities | Places | Domains / datasets / elements | Periods |
| --- | --- | --- | --- | --- |
| FAOSTAT | maize (corn) item 56, wheat item 15, soya beans item 236, maize and products (FBS) item 2514 | United States (area 231), France (68), Germany (79, soya beans: no data on record) | QCL 5510 production, 5312 area harvested, 5412 yield; PP 5532 producer price; FBS 5511 production, 5142 food | 2021-2023 |
| NASS | CORN (grain production, yield, price received), SOYBEANS (acres harvested), WHEAT (no data) | United States, Iowa (FIPS 19), Story County, Iowa (FIPS 19169) | Quick Stats survey and census | 2022-2023; marketing-year prices |
| PSD | corn 0440000, wheat 0410000 | United States (US), European Union (E4) | production (28), imports (57), exports (88), ending stocks (176) | market years 2024-2025 |
| Eurostat | common wheat and spelt `C1110`, common wheat `01110000` | France, Germany | `apro_cpsh1` (AR, PR_HU_EU, YI_HU_EU), `apri_ap_crpouta` | 2022-2023 |
| Agri-food portal | milling wheat `BLTPAN`, maize `MAI` | France, Germany | cereal prices | January 2024 weeks |

Places resolve through `geospatial.place-resolution`
(`src/kb/geospatial.py`) by the codes each source uses (FAO area, ISO 3166-1,
US FIPS, PSD country, Eurostat geo, portal member state); commodities are
aligned only through reviewable crosswalks (AF07). Trade flows, climate and
weather records and RASFF notices are linked by citation only (AF08).
