# Products safety notices and recalls: source-contract audit and provider coverage (R01)

Tracking: #1916 · delivery issue #1935 · recorded 2026-09-28.

This audit sets out, per source, what the Products pack's optional `safety`
feature may acquire, how, and on what terms. It was written without network
access. Endpoints, field names, identifiers and terms come from the providers'
published documentation as the author knows it. **Every item marked _verify_
must be checked against the live page and a published response before the
first dated live run (R12, #2033). No provider is `live` until that run
exists.** The machine-readable copy of these decisions is
`SAFETY_PROVIDER_CONTRACTS` in `src/ingestion/product_sources.py`. The MCP tool
`product_safety_source_contracts` returns it and `products_readiness` reports
it per provider.

These non-goals apply to every source:

- A notice is what an authority published. Its hazard, affected
  identification and corrective action are stored verbatim with a JSON-pointer
  locator into the provider payload. Nothing is paraphrased or scored.
- No safety verdict, risk score or consumer advice is produced. A product
  without a notice is reported as having *no notice on record*, never as safe.
  The only "advice" ever shown is the authority's own corrective-action text,
  quoted.
- No similar model is inferred to be affected. Notices reach Products records
  only through reviewable matches on GTIN, brand and model (R06).
- Absence of a notice from a filtered, partial or failed response never marks
  it withdrawn or resolved, and the absence of a follow-up never implies
  closure.
- Portals without documented machine access are never scraped.

## Access decisions

| Source | Delivers | Decision | Reason |
| --- | --- | --- | --- |
| EU Safety Gate (ex RAPEX) | non-food consumer and professional product alerts with measures and follow-ups | `unverified-live` | The public portal (`ec.europa.eu/safety-gate-alerts`) publishes each alert and a weekly report. The adapter reads the portal's JSON alert view by alert number and the weekly report by ISO week (`/public/api/notification/alerts?reference=…`, `/public/api/notification/weekly-reports/{year}/{week}`). These paths and the JSON field names in the fixtures are _verify_: if the portal offers no supported machine interface the source moves to `not-implemented` and is not scraped |
| CPSC Recalls API (saferproducts.gov) | US consumer product recalls | `unverified-live` | Documented REST service `https://www.saferproducts.gov/RestWebServices/Recall?format=json` with query parameters `RecallNumber`, `RecallID`, `Manufacturer`, `RecallDateStart`, `RecallDateEnd`, `LastPublishDateStart`, `LastPublishDateEnd`. No key. Field names (`RecallNumber`, `RecallDate`, `LastPublishDate`, `Products[].Model`, `ProductUPCs[].UPC`, `Hazards[]`, `Remedies[]`, `RemedyOptions[]`, `Manufacturers[]`, `Importers[]`) are from the API documentation; exact casing _verify_ |
| NHTSA recalls API | US motor-vehicle and equipment recall campaigns | `unverified-live` | Documented `https://api.nhtsa.gov/recalls/campaignNumber?campaignNumber=…` (one row per make/model/model year). `recallsByVehicle?make=&model=&modelYear=` answers only the rows of one vehicle, so it is a *filtered view* of a campaign: it is documented for discovery and never used for acquisition, because a partial row set would read as a changed campaign. No key |
| RASFF Window | EU food and feed notifications with follow-ups | `unverified-live` | The public RASFF Window (`webgate.ec.europa.eu/rasff-window`) shows each notification and its follow-ups. The adapter reads its JSON notification view by reference (`/backend/public/notification?reference=YYYY.NNNN`). Whether this is a supported public interface, and its field names, are _verify_; if it is not supported the source becomes `not-implemented` and is not scraped |
| BAuA product recall pages | German recalls and warnings (Produktrückrufe) | `not-implemented` | HTML pages with no documented API, feed or bulk export (_verify_ whether an RSS feed exists). Never scraped. German market-surveillance alerts reach Safety Gate, which is acquired instead |
| GPSR text (Regulation (EU) 2023/988) | the legal act notices cite | `not-implemented` here; acquired through `legal-research` | The act is not a notice source. CELEX `32023R0988` (and the RASFF basis, Regulation (EC) No 178/2002, CELEX `32002R0178`) are added to the Legal pack's CELLAR selection (`cellar-product-safety-acts-eng`, `legal-research` 1.2.0) and resolved by exact identifier (R07). The EUR-Lex HTML page is never fetched |

**Unavailable-access fallback.** A payload is not stored when any of these
happens: an HTTP error, a redirect to another host, schema drift, a response
over the byte ceiling or a page with more notices than the run's result budget
(refused, never truncated). The source run is recorded as failed and monitors
do not advance their watermark past it. An explicitly selected notice number
the provider does not know (HTTP 404) is a `not_found` selection outcome in the
page receipt, not a failure, and not a withdrawal.

## Per-source contract

| Source | Identifiers | Revision semantics | Cadence | Paging and limits | Terms |
| --- | --- | --- | --- | --- | --- |
| Safety Gate | alert number (`SR/00417/26` shape: prefix, running number, two-digit year; _verify_ prefixes), notifying country | `lastUpdateDate` (else `publicationDate`) is the revision date; `followUps[]` carry their own date and country. Earlier versions of an alert are **not** retrievable from the portal (_verify_), so each distinct payload observed is kept as a revision | weekly report plus continuous updates | one alert or one weekly report per request; weekly reports over the result budget are refused | EU Commission reuse notice (Decision 2011/833/EU) with acknowledgement; _verify_ the portal's own legal notice |
| CPSC | `RecallNumber` (five digits, fiscal year + sequence), `RecallID` | `LastPublishDate` is the revision date; the API serves only the current version | continuous | one recall per number request; manufacturer + date window up to 366 days; no documented rate limit (_verify_) | US federal government work, public domain; attribution requested (_verify_) |
| NHTSA | `NHTSACampaignNumber` (`26V104000` shape), `NHTSAActionNumber` | the API has no update date; `ReportReceivedDate` (DD/MM/YYYY) is the only date, so content changes are ordered by observation and marked `order_basis: observation` | continuous | one campaign per request; no documented rate limit (_verify_) | US federal government data, public domain |
| RASFF | notification reference (`2026.0457` shape), classification | `lastUpdate` (else notification date) is the revision date; follow-ups carry their own date and country; prior versions not retrievable (_verify_) | continuous | one notification per request | EU Commission reuse notice; _verify_ |
| BAuA | none machine-readable | n/a | n/a | n/a | n/a |
| GPSR (CELLAR) | CELEX `32023R0988`, ELI `http://data.europa.eu/eli/reg/2023/988/oj` | Legal pack editions | as published | CELLAR SPARQL (legal-research) | EU reuse notice |

What each source publishes, as stored:

| Source | Hazard | Affected identification | Corrective action | Issuing authority | Dates |
| --- | --- | --- | --- | --- | --- |
| Safety Gate | `risk.riskType` (list), `risk.level`, `risk.description`, `risk.compliance` (text that cites standards and acts) | `product.brand`, `product.name`, `product.typeNumberOfModel`, `product.batchNumber`, `product.barcode` (one or more codes in one field), `product.description`, `product.category` | `measures[]` (`measureType`, `takenBy`, `category` compulsory/voluntary) | European Commission (Safety Gate), with the notifying country's authority as `notifyingCountry` | `publicationDate`, `lastUpdateDate`, follow-up dates |
| CPSC | `Hazards[].Name`, `Hazards[].HazardType`, `Injuries[].Name` | `Products[].Name`, `Products[].Model`, `Products[].Description`, `ProductUPCs[].UPC` (kept as GTIN-12 strings with `gtin_state`) | `Remedies[].Name`, `RemedyOptions[].Option` | U.S. Consumer Product Safety Commission | `RecallDate`, `LastPublishDate` |
| NHTSA | `Consequence`, `Summary`, `Component` | `Make`, `Model`, `ModelYear` per row (kept native; never mapped to display attributes) | `Remedy`, `parkIt`, `parkOutSide`, `overTheAirUpdate` | National Highway Traffic Safety Administration | `ReportReceivedDate` |
| RASFF | `hazards[]` (`category`, `name`, `analyticalResult`), `riskDecision` | `product.category`, `product.name`, `batches[]` (lot, best-before verbatim), origin and distribution countries | `actionTaken`, `distributionStatus` | European Commission (RASFF), notifying country as declared | `notificationDate`, `lastUpdate`, follow-up dates |

GTINs are never rewritten: the verbatim string is stored with the
`gtin_state` of `src/ingestion/product_sources.py` (valid, invalid checksum,
invalid format). Matching compares GTINs through one shared key (digits
left-padded to fourteen) on both sides, so a UPC-12 and its GTIN-13 or GTIN-14
form meet. Enumerations (notice type, issuing authority, notifying country)
are kept as the provider's raw value beside a mapped value; an unknown raw
value maps to `unknown`, never to a guess.

## Bounded reference cohort

The offline fixtures are **authored, not captured**, in the documented shapes
above, and name fictional products and companies only (brands Exampla,
Othermark, Brightway and Velomark; fictional notice numbers). They are:

| Source | Selection | Covers |
| --- | --- | --- |
| `safety-gate-alerts` | alerts `SR/00417/26`, `SR/00431/26`, `SR/00502/26`, `SR/00388/26`, `SR/09999/26` (unknown) and weekly report `2026-W11` in category *Electrical appliances and equipment* | brand + model (Exampla EX-32U8, overlapping the `products-displays` selection), a GTIN (Exampla EX-27Q4, `4012345000016`), a GTIN that contradicts the brand (Othermark OM-9 with Exampla's `4012345000030`), a kettle GTIN also named by CPSC, a not-found selector and a weekly report whose other-category alerts are filtered out |
| `cpsc-recalls` | recall `26117`; manufacturer *Exampla Displays Inc.* between 2026-02-01 and 2026-04-30 | the kettle UPC `012345678905` (same GTIN as Safety Gate's `0012345678905`) and a recall naming model `EX-32U8` without a brand or UPC (stays an unmatched string) |
| `nhtsa-recalls` | campaign `26V104000` | a two-row vehicle campaign (Velomark Cityrunner, model years 2025 and 2026) |
| `rasff-notifications` | notification `2026.0457` | a food notification with a lot and best-before date and one follow-up |

The Legal pack's `cellar-product-safety-acts-eng` fixture carries authored
rows for CELEX `32023R0988` and `32002R0178` so that citations resolve offline.
