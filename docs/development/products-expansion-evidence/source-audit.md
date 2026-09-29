# Products expansion: source audit and bounded coverage (PX01)

Tracking: [#2061](https://github.com/Ikey168/Noesis/issues/2061) · delivery issue
[#2093](https://github.com/Ikey168/Noesis/issues/2093) · recorded 2026-09-28.

This audit decides which product groups beyond electronic displays and which
electronic-component sources the Products pack covers in v1, and on what terms.
It follows the audit of the original pack (#1706) and was written **without
network access**: endpoints, field names, category identifiers, rate limits and
licence clauses come from the providers' published documentation as the author
knows it. **Every item marked _verify_ must be checked against the live terms
page and a published response before the first dated live run (PX12, #2104).**
No source is `live` until that run exists; all are `unverified-live`.

The machine-readable copy of these decisions is `PROVIDER_CONTRACTS` and
`COMPONENT_SOURCE_DECISIONS` in `src/ingestion/product_sources.py`, returned by
the MCP tool `product_provider_contracts`. Attribute keys, units, modes, label
schemes and provider field mappings per category live in the category registry
(`config/product_categories/*.json`, loaded by `src/kb/product_categories.py`).

Non-goals that apply to every source (unchanged from the display pack): no
prices, stock, availability, retailer feeds, shopping recommendations, affiliate
links, reviews or ratings, inferred compatibility, Full Icecat content, EPREL
supplier-only endpoints or catalogue mirroring. For components additionally: no
cross-reference or drop-in replacement suggestions, and no lifecycle status
that a named source did not publish.

## EPREL product groups

All groups are read through the same public endpoint as displays,
`GET https://eprel.ec.europa.eu/api/products/{productGroup}/{registrationNumber}`
with the `x-api-key` issued by the EPREL public API request process. Revision
semantics are the display adapter's: `versionNumber` orders revisions, a newer
version becomes current, an older one delivered later is history, and only an
explicit `status` of `WITHDRAWN`/`DELETED`/`REMOVED` withdraws a registration.
Supplier-only endpoints are never used.

| Group (EPREL code) | Label regulation (scheme key) | Public fields relevant to comparison | Decision |
| --- | --- | --- | --- |
| Household washing machines (`washingmachines2019`) | Delegated Regulation (EU) 2019/2014, classes A–G (`EU_2019_2014`) | rated capacity (kg), energy class, energy per 100 cycles (kWh), water per cycle (L), maximum spin speed (rpm), spinning noise (dB(A)) and noise class, spin-drying class | **selected** |
| Refrigerating appliances (`refrigeratingappliances2019`) | Delegated Regulation (EU) 2019/2016, classes A–G (`EU_2019_2016`) | total volume (L), annual energy consumption (kWh/annum), energy class, noise (dB(A)) and noise class, climate classes | **selected** |
| Household washer-dryers (`washerdriers2019`) | (EU) 2019/2014 | separate washing and washing-and-drying cycles, two capacities | deferred: two cycle modes per attribute; add after the registry proves mode handling on appliances |
| Household dishwashers (`dishwashers2019`) | (EU) 2019/2017 | rated capacity in place settings, energy per 100 cycles | deferred: no Open Icecat overlap selected for v1 |
| Light sources (`lightsources`) | (EU) 2019/2015 | luminous flux, colour temperature, on-mode power; many registrations per family | deferred: per-light-source records rarely carry a model shared with Icecat |
| Tyres (`tyres`) | (EU) 2020/740 | fuel-efficiency, wet-grip and external-noise classes per tyre size | deferred: a different label scheme (three classes) and size-keyed identity |

Field names for the selected groups (`ratedCapacity`, `energyClass`,
`energyConsumption100`, `waterConsumption`, `maxSpinSpeed`, `noise`,
`noiseClass`, `spinDryingEfficiencyClass`, `totalVolume`,
`energyConsumptionAnnual`, `climateClasses`) are pinned in the registry from the
documentation as the author knows it and are _verify_. The fixtures under
`tests/fixtures/source_packs/products-eprel-*.json` are authored envelopes in
that shape for fictional brands, not captures. Unmapped fields are kept on the
record as `source_fields`, never mapped by guess.

Bounded v1 coverage: one source per selected group, each an explicit selection
of 1–50 registration numbers; weekly refresh as for displays.

## Open Icecat categories

Open Icecat terms already applied to displays carry over unchanged: the Open
catalogue only (sponsoring brands), the `openicecat-live` shop name, brand
content attributed as `brand-authorised-content`, storage and export per the
Open Icecat terms (operator must confirm), Full Icecat products reported per
model as `outside_open_catalogue`.

| Category | Overlaps EPREL group | Decision |
| --- | --- | --- |
| Washing machines | `washingmachines2019` | **selected** |
| Fridges / fridge-freezers | `refrigeratingappliances2019` | **selected** |
| Dishwashers | `dishwashers2019` | deferred with the EPREL group |
| Printers, notebooks, other IT | none | out of scope: no energy-label group to corroborate against |

Category identifiers pinned in `config/source_packs/products.json`
(`provider_category_id`) and the English feature labels in the registry
("Washing capacity", "Energy efficiency class", "Energy consumption per 100
cycles (washing)", "Maximum spin speed", "Noise level (spinning)", "Total net
capacity", "Annual energy consumption", "Climate class") are _verify_: the
category IDs are placeholders to be replaced from the live Open Icecat
category list before the first live run. Icecat energy classes carry no
regulation, so their label scheme stays unknown and they never compare with an
EPREL class.

Bounded v1 coverage: one source per selected category, 1–50 explicit
brand + product code or GTIN selectors.

## Electronic-component sources

| Source | Access | Licence, redistribution and caching | Rate limits | Identifiers | Decision |
| --- | --- | --- | --- | --- | --- |
| Nexar (Octopart) API | GraphQL `https://api.nexar.com/graphql`, OAuth2 client credentials (`identity.nexar.com`) | Commercial API terms; the plans license use of results within the subscriber's application and restrict storing, caching beyond a short period and redistributing part data (_verify_ exact clause and period) | plan quota of matched parts per month (_verify_) | manufacturer + MPN, Octopart part id, distributor SKUs | **link-only**: a link record to the Octopart part search for the manufacturer + MPN; no API call, no cached specs, lifecycle or offers |
| DigiKey Product Information API v4 | `https://api.digikey.com/products/v4/search/...`, OAuth2 (2-legged client credentials) | API terms tie use to DigiKey's services and restrict bulk storage and redistribution of product data (_verify_) | per-minute and per-day request limits by plan (_verify_) | DigiKey part number (distributor SKU), manufacturer + MPN | **link-only**: a link record to the DigiKey keyword search for the MPN; no API call |
| Mouser Search API | `https://api.mouser.com/api/v1/search/partnumber`, API key | API terms oriented to purchasing integrations; redistribution not granted (_verify_) | per-minute and per-day request limits (_verify_) | Mouser part number, manufacturer + MPN | **not implemented**: v1 stores nothing from distributors and two link-only distributors already give readers a place to look |
| Manufacturer parametric data, BMEcat 2005 catalogue files (manufacturer role) | a catalogue file the manufacturer publishes for reuse on its documented download host, pinned per source (HTTPS, same-host only) | per manufacturer; implemented only where the manufacturer's terms permit storing and redistributing the parametric data to the operator's users (operator records and confirms the terms in the source licence block; _verify_ per catalogue) | one bounded download per run | `MANUFACTURER_NAME` + `MANUFACTURER_PID` (else the manufacturer's own `SUPPLIER_PID`), GTIN when published | **implement** |
| Supplier (wholesaler) BMEcat catalogues (supplier role) | a catalogue file the supplier publishes for reuse, pinned per source | same rule; price blocks (`PRODUCT_PRICE_DETAILS`) are never parsed or stored | one bounded download per run | `SUPPLIER_PID` kept only as a provider-scoped SKU alias; identity is `MANUFACTURER_NAME` + `MANUFACTURER_PID` | **implement** |
| Manufacturer web parametric search pages and PDFs found by crawling | HTML | — | — | — | **not implemented**: no crawling; datasheets are followed only when a source record links them |

What the `implement` adapters store: the component identity (normalised
manufacturer + MPN as published), parametric features mapped through the
registry with native and normalised units and tolerances, features the
registry does not map as `source_fields`, linked datasheets (`MIME_INFO`,
purpose `data_sheet`), and `lifecycle_status` **only** where the catalogue
publishes a product status (`PRODUCT_STATUS`), with the catalogue's
generation date as the date basis and the source named. Recognised status texts
(`active`, `NRND` / "not recommended for new designs", "last time buy",
`obsolete`) map to the shared enumeration; any other text stays declared with
value `unknown`. Nothing is inferred from an end-of-life date, an absence or a
successor.

The BMEcat element names used by the adapter (`T_NEW_CATALOG/PRODUCT`,
`SUPPLIER_PID`, `PRODUCT_DETAILS/MANUFACTURER_NAME`, `MANUFACTURER_PID`,
`INTERNATIONAL_PID`, `PRODUCT_FEATURES/REFERENCE_FEATURE_GROUP_ID`,
`FEATURE/FNAME|FVALUE|FUNIT`, `MIME_INFO/MIME`, `PRODUCT_STATUS`, header
`GENERATION_DATE`) follow the BMEcat 2005 specification; whether a given
manufacturer fills `PRODUCT_STATUS` with a lifecycle text, and which
FNAME/ECLASS property identifiers it uses, is _verify_ per catalogue. Fixtures
are authored catalogues for fictional manufacturers.

Credentials: Nexar and DigiKey are link-only and need none. BMEcat downloads
are keyless; a catalogue behind a login would use the existing secret reference
mechanism (`auth.secret_ref`), never an inline credential.

Caching: an `implement` source's records are kept as revisions like any other
provider record; the pinned licence block states the terms under which that is
permitted and readiness blocks live runs until the operator accepts them.
Datasheets are fetched only when the source's document policy says `retain`;
otherwise they stay links.

## Bounded v1 coverage

| Source (source pack `products-displays` 1.2.0) | Category | Bound |
| --- | --- | --- |
| `eprel-washing-machines` | household washing machines | 1–50 registration numbers |
| `eprel-refrigerating-appliances` | refrigerating appliances | 1–50 registration numbers |
| `icecat-washing-machines` | household washing machines | 1–50 brand + product code / GTIN selectors |
| `icecat-refrigerating-appliances` | refrigerating appliances | 1–50 selectors |
| `bmecat-capatronic-mlcc` (manufacturer role) | multilayer ceramic capacitors | one catalogue, 1–50 manufacturer + MPN selectors |
| `bmecat-wholesale-mlcc` (supplier role) | multilayer ceramic capacitors | one catalogue, 1–50 manufacturer + MPN selectors |
| Octopart, DigiKey | any component category | link records computed on lookup; nothing fetched |

The display sources (`icecat-displays`, `eprel-displays`) and the safety
feature's notice sources are unchanged. The category registry also defines
thick-film chip resistors so a resistor catalogue can be added by selection
alone, without code.

## Version choice

The source pack moves from 1.1.0 to 1.2.0 and changes no existing source.
`products.core` keeps its `^1.0.0` range and `products.safety` its `^1.1.0`
range, both satisfied by 1.2.0, so no upgrade chain or pin breaks.
