# Food composition and labelling: source contracts, licences and bounded coverage (FC01, #2245)

Parent: #2216 (Products pack expansion). Machine-readable form of every decision below:
`PROVIDER_CONTRACTS`, `TABLE_DECISIONS`, `LIVE_VERIFICATION` and `BOUNDED_COVERAGE` in
`src/ingestion/food_composition_sources.py`. Source entries: `config/source_packs/products.json`
(`products-displays` 1.3.0, connector `food-composition`, operation `food`: `off-food-products`, `fdc-foods`,
`ciqual-composition`).

Out of scope for every source: health or diet advice, nutrition scores, health ratings or rankings computed by
Noesis, and recomputation of any provider score.

## Provenance classes (decision)

Open Food Facts is **crowd-sourced**; FoodData Central and national composition tables are **reference**. The
class is mandatory on every record (`provenance_class`), each provider keeps its own rows (`food_items` keyed by
provider and provider key), and no query or export merges values of the two classes into one value: answers show
them side by side and name differences as differences, never reconciled.

## Open Food Facts (acquire, crowd-sourced)

| Item | Contract |
| --- | --- |
| Access | Product API v2: `GET https://world.openfoodfacts.org/api/v2/product/{barcode}.json?fields=...`; a pinned revision adds `rev=N` (*verify* that v2 honours `rev`). Daily JSONL/CSV/Parquet dumps exist; a bounded dump slice is the documented alternative but not used, because the coverage is a short GTIN list. |
| Authentication | None for reads; a descriptive `User-Agent` (app, version, contact) is required. |
| Rate limits | 100 product reads/min/IP; 10 searches/min (search is never used). One product per page. |
| Licence | Database: **ODbL 1.0**. Individual contents: **DbCL 1.0**. Product images: **CC BY-SA** (never mirrored). |
| Attribution | "Contains information from Open Food Facts, made available under the ODbL v1.0" - carried as `attribution` on every record, answer and export (`ODBL_ATTRIBUTION` in `src/kb/food_composition.py`). |
| Share-alike | A publicly used database derived from OFF data (alone or combined with other data) must be offered under the ODbL. Noesis keeps OFF-derived records separable: own provider rows, own provenance class, exports list them per provider with the attribution and `share_alike: true`, so they can be published under the ODbL or removed without touching reference data. Produced works (answers) carry the attribution. |
| Revisions | `rev` (integer, increments on each edit) orders revisions; `last_modified_t` is the as-of date. Each OFF revision observed is a label revision; earlier revisions are only as available as `rev` allows. |
| Quality flags | `data_quality_*_tags`, `completeness`, `states_tags` kept verbatim in `source_fields`. |
| Excluded | Nutri-Score, NOVA, Eco-Score / environmental score, nutrient levels, `nutrition-score-*` and `*-estimate-from-ingredients` nutriments, images: never parsed or stored; listed per page in the receipt (`excluded_fields_dropped`). |
| Live status | `unverified-live` (intended `live-verified` after #2302). |

## USDA FoodData Central (acquire, reference)

| Item | Contract |
| --- | --- |
| Access | `GET https://api.nal.usda.gov/fdc/v1/food/{fdcId}?format=full`, one food per page. |
| Authentication | data.gov API key in the `X-Api-Key` header, held as the `NOESIS_FDC_API_KEY` secret reference (never in manifests, receipts or records). `DEMO_KEY` is never used. |
| Rate limits | 1,000 requests/hour/key (api.data.gov default); HTTP 429 blocks the key for an hour and maps to `rate_limited`. |
| Data types | Branded (label data with `gtinUpc`; `foodNutrients` per 100 g calculated from the label, `labelNutrients` per serving), Foundation (analytical, derivation codes, data points), SR Legacy (final 2018 release), Survey (FNDDS). The data type is kept on every record. |
| Licence | Public domain, published under CC0 1.0; citation requested ("U.S. Department of Agriculture, Agricultural Research Service. FoodData Central."). |
| Revisions | `publicationDate` orders revisions of one FDC ID; a newer publication of the same FDC ID is a new revision. An updated Branded food may get a new FDC ID: the two are related through GTIN identity proposals, never merged. |
| Nutrients | FDC nutrient number, name, unit, amount and derivation code as published; label nutrients (`fdc-label-nutrient`, no unit published: `unit.state = absent`) stay distinct from food nutrients (`fdc-nutrient-number`). |
| Live status | `unverified-live`. |

## Composition tables (FC05 decisions)

| Table | Decision | Reason |
| --- | --- | --- |
| Ciqual (Anses, FR) | **acquire** | Licence Ouverte / Etalab 2.0 (verify the edition notice); XML archive (`alim_*`, `const_*`, `compo_*`); stable food and constituent codes, INFOODS codes, explicit editions. Bounded to selected native food codes of one pinned edition. |
| EFSA food composition data | reference-only | A compilation of national tables with borrowed and harmonised values published as workbooks; storing it would put harmonised values beside the national sources, which FC05 forbids. Cited, not stored. |
| BLS (Max Rubner-Institut, DE) | reference-only | Historical editions are licensed per user without redistribution; the reuse terms of the current edition must be verified before any acquisition. |
| McCance and Widdowson's CoFID (UK) | reference-only | Open Government Licence permits reuse, but the dataset is one Excel workbook without stable per-food access; deferred to an operator import. |
| Frida (DTU, DK) | excluded | Outside the bounded coverage. |

Ciqual values are kept verbatim: `teneur` text (decimal comma, `traces`, `< x`, `-`), `min`, `max`,
`code_confiance` and `source_code`; the value-type marker (`trace`, `less-than`, `missing`, `as-published`) is read
from the table's own text, never estimated. Units come from the constituent name (`(g/100 g)`); a constituent
without a stated unit keeps `unit.state = absent`. No value is harmonised across tables.

## Bounded coverage

| Kind | Selection |
| --- | --- |
| GTINs | `4000000000105` (frozen chicken kebab; two OFF label revisions; named by RASFF 2026.0457 by brand and designation), `4000000000150` (yoghurt; no notice on record), `0071000000208` (cereal bar; FDC Branded as UPC-A `071000000208`, and a second FDC Branded food publishing the same GTIN under another brand - an identity conflict), `4000000000204` (unknown to OFF). |
| Generic foods | FDC Foundation 9990201 (apple), SR Legacy 9990301 (oats); Ciqual 2020 codes 13039 (apple) and 9310 (oat flakes). |
| Markets | EU (OFF world database, products sold in DE/FR/NL), US (FDC Branded). |
| Food groups | frozen meat products, yoghurt, cereal bars, fruit, cereals. |

All fixtures are synthetic (fictional brands and barcodes with valid check digits; illustrative values). Offline
fixture results and live results are reported separately; every provider stays `unverified-live` until the dated
live run of #2302.

## LIVE_VERIFICATION

| Provider | Status | Intended |
| --- | --- | --- |
| open-food-facts | unverified-live | live-verified |
| fooddata-central | unverified-live | live-verified |
| composition-table:ciqual | unverified-live | live-verified |
| composition-table:efsa / bls / cofid | not-implemented | not-implemented (reference-only) |
| composition-table:frida | not-implemented | not-implemented (excluded) |
