# Medical devices: source-contract audit, data minimisation and bounded coverage (MD01)

Tracking: #2654 · delivery issue #2658 · recorded 2026-09-30.

This audit sets out, per source, what the Clinical Evidence provider
`clinical.devices` (features `medical-devices-fda`, `medical-devices-gudid`,
`medical-devices-eudamed`) may acquire, how, on what terms, and which personal
data is kept out.

**How it was researched.** On 2026-09-30 every official page named below was
requested from this build environment and refused by the network egress proxy
(`EGRESS_BLOCKED` for `open.fda.gov`, `accessgudid.nlm.nih.gov` and
`ec.europa.eu`). The points below therefore come from search-engine excerpts of
the official pages (read 2026-09-30, URLs cited) and from the providers'
documentation as the author knows it. **Every item marked _verify_ is
unverified and must be checked against the live page, the live terms and a real
response before the first dated live run (MD14, #2723). No source is
`verified-live` until that run exists.**

The machine-readable copy of these decisions is `PROVIDER_CONTRACTS`,
`LIVE_VERIFICATION`, `BOUNDED_COVERAGE`, `MINIMISATION`, `EUDAMED_MODULES` and
`MAUDE_CAVEATS` in `src/ingestion/medical_devices_sources.py`. Each source entry
in `config/source_packs/clinical-evidence.json` (`clinical-evidence` 0.1.4)
states `medical_devices.live_verification: unverified-live`, and the MCP tool
`medical_devices_source_contracts` returns the same decisions.

Non-goals for every source: no safety-signal detection, no causality from
adverse-event reports, no clinical advice and no patient data beyond what
regulators publish. Adverse-event report counts are counts of reports with the
source's caveats, never incidence, rates or causal events.

## Pages consulted (2026-09-30)

| URL | Fetched | What was read |
| --- | --- | --- |
| https://open.fda.gov/apis/device/ | no (EGRESS_BLOCKED) | search excerpt: device endpoints 510(k), classification, PMA, recall, enforcement, event, registration and listing, UDI |
| https://open.fda.gov/apis/authentication/ | no (EGRESS_BLOCKED) | search excerpt: 240 requests per minute; 1,000 per day per IP without a key; 120,000 per day per key |
| https://open.fda.gov/terms/ | no (EGRESS_BLOCKED) | search excerpts of openFDA pages: "Do not rely on openFDA to make decisions regarding medical care"; results are to be assumed unvalidated; access may be limited under the Terms of Service. The licence wording itself was not read: _verify_ |
| https://open.fda.gov/apis/device/pma/ and https://open.fda.gov/fields/devicepma_reference.pdf | no | search excerpt: `supplement_number`, `supplement_type`, `decision_code` (e.g. LE30, APRL, APWD), `pma_number` with leading letters |
| https://open.fda.gov/apis/device/510k/searchable-fields/ and https://open.fda.gov/fields/deviceclearance_reference.pdf | no | search excerpt: `k_number`, `decision_code`, `decision_date`, `product_code`, `applicant`, `contact`, address fields |
| https://open.fda.gov/apis/device/classification/ | no | search excerpt: `product_code`, `device_class`, `regulation_number` |
| https://open.fda.gov/apis/device/recall/ and https://open.fda.gov/fields/devicerecall_reference.pdf | no | search excerpt: `https://api.fda.gov/device/recall.json`, `root_cause_description`; `product_res_number`, `recall_status` field names _verify_ |
| https://open.fda.gov/apis/device/event/ and https://www.fda.gov/medical-devices/medical-device-reporting-mdr-how-report-medical-device-problems/mdr-data-files | no | search excerpt: MAUDE holds mandatory and voluntary reports; reports can be incomplete, inaccurate, untimely, unverified or biased; MDR data cannot be used to determine rates of events; report numbers; `event_type` countable with `.exact` |
| https://accessgudid.nlm.nih.gov/resources/developers (device lookup and device history API pages) | no (EGRESS_BLOCKED) | search excerpt: `GET /api/v2/devices/lookup.json` with `di`, `udi` or `record_key`; `GET /api/v2/devices/history.json?di=`; the public device record key is stable when the DI changes; a v3 API exists (new GMDN information) |
| https://ec.europa.eu/tools/eudamed/ | no (EGRESS_BLOCKED) | not read |
| https://health.ec.europa.eu/medical-devices-eudamed/overview_en and the "four first modules mandatory from 28 May 2026" notice | no | search excerpt: Actor registration, UDI/device registration, Notified bodies and certificates and Market surveillance mandatory from 2026-05-28; actor information public except competent-authority contacts |
| https://eur-lex.europa.eu/eli/reg_impl/2021/2078 | no | search excerpt: Implementing Regulation (EU) 2021/2078 on EUDAMED; a public website; machine-to-machine data exchange for national databases |

## Access decisions

| Source (source-pack id) | Provider | Delivers | Decision | Reason |
| --- | --- | --- | --- | --- |
| `devices-fda-510k` | openFDA `/device/510k.json` | 510(k) clearances per product code: K number, decision code, decision date, applicant | `unverified-live` | Documented endpoint; fixture-verified parser; `clearance_type`, `third_party_flag` are _verify_ |
| `devices-fda-pma` | openFDA `/device/pma.json` | PMA originals and supplements per product code | `unverified-live` | `supplement_reason`, `ao_statement` are _verify_ |
| `devices-fda-classification` | openFDA `/device/classification.json` | product code, device class, regulation number, panel | `unverified-live` | As above |
| `devices-fda-recalls` | openFDA `/device/recall.json` | recalls per product code: recall number, status, dates, K/P numbers | `unverified-live` | `product_res_number`, `recall_status`, `event_date_terminated`, `additional_info_contact` are _verify_; the endpoint is not known to state a recall class |
| `devices-fda-enforcement` | openFDA `/device/enforcement.json` | enforcement report of a declared recall number with the recall class (`classification`) and status | `unverified-live` | Recall numbers are the join; `classification` on the device enforcement endpoint is _verify_ |
| `devices-fda-maude-reports` | openFDA `/device/event.json` | MAUDE reports for declared (product code, window) pairs | `unverified-live` | `device.device_report_product_code` search field and `mdr_text` shape are _verify_ |
| `devices-fda-maude-counts` | openFDA `/device/event.json?count=event_type.exact` | report counts per event type for declared windows | `unverified-live` | Count queries return terms and counts only |
| `devices-gudid-identifiers` | AccessGUDID device lookup and device history | device record of a declared primary DI with package DIs, version, product codes, premarket submissions | `unverified-live` | v2 paths from the search excerpt; the v3 migration, the history response shape and the terms of use are _verify_ |
| `devices-eudamed-actors` | EUDAMED public site, actor module | actor by SRN | `unverified-live` | No documented public API or bulk download was confirmed; operator-declared documents with placeholder paths (`/tools/eudamed/placeholder/...`) and an authored mapping of the public fields; paths, shape and reuse terms are _verify_ |
| `devices-eudamed-devices` | EUDAMED public site, UDI/device module | device by Basic UDI-DI with its UDI-DIs | `unverified-live` | As above |
| `devices-eudamed-certificates` | EUDAMED public site, notified bodies and certificates module | certificate by number with status and dates | `unverified-live` | As above |
| (not acquired) | openFDA `/device/registrationlisting.json` | establishment registrations and listings | `documented-not-acquired` | Records name official correspondents and contact persons; MD03 does not need them |
| (not acquired) | openFDA `/device/udi.json` | GUDID copy without version history | `documented-not-acquired` | AccessGUDID is acquired instead |

## Per-source contract

### openFDA device endpoints

- **Endpoints:** `https://api.fda.gov/device/{510k,pma,classification,recall,enforcement,event}.json`, queried with `search` and `limit`/`skip`, or `count=event_type.exact` for report counts.
- **Authentication and key handling:** an optional key; the adapter uses the optional secret `NOESIS_OPENFDA_API_KEY` (the key the Medicines feature's openfda provider already uses) and sends it as the `api_key` request parameter only. It never enters a durable URL, a record, a receipt or a document; a response that echoes it is discarded.
- **Rate limits:** 240 requests per minute; 1,000 requests per day per IP without a key and 120,000 per day per key (search excerpt; _verify_). HTTP 429 is reported as `rate_limited` with `Retry-After`.
- **Licence and redistribution:** openFDA terms of service; FDA data are US government works. Every response carries `meta.disclaimer`, which is stored on every record ("Do not rely on openFDA to make decisions regarding medical care"; results are unvalidated). The exact licence wording is _verify_.
- **Updates, corrections and removals:** openFDA publishes no per-record revision stamp. A changed row is a new revision of the same record key (a recall status change, a supplement newly listed on an approval). `meta.last_updated` is kept in the receipt. A row that disappears from a later response is not deleted; the last revision stays on record.
- **MAUDE caveats** (attached to every report and count): MAUDE holds reports from mandatory and voluntary reporters; reports can be incomplete, inaccurate, untimely, unverified or biased and do not establish that a device caused an event; events are under-reported and report counts cannot establish rates or compare devices; a report count is a number of reports, not of patients or events.

### AccessGUDID

- **Endpoints:** `https://accessgudid.nlm.nih.gov/api/v2/devices/lookup.json?di=` and `/api/v2/devices/history.json?di=` (search excerpt; a v3 API exists: _verify_ which is current).
- **Authentication:** none. **Rate limits:** not stated in the excerpts (_verify_); one lookup and one history request per declared DI.
- **Licence:** FDA GUDID public device identification data published by NLM; the AccessGUDID terms of use were not read (_verify_).
- **Revisions:** `publicVersionNumber` and `publicVersionDate` per device record version; a new version is a new revision (order by version number; an older version delivered later is an `older-observation`). The public device record key is stable if the primary DI changes.

### EUDAMED public modules

- **Access:** the EUDAMED public site. No documented public API or bulk download could be confirmed, and the machine-to-machine service under Implementing Regulation (EU) 2021/2078 is for registered actors. The adapter therefore reads operator-declared JSON documents on `ec.europa.eu` under `/tools/eudamed/`; the pack's paths are placeholders until MD14 replaces them with verified public paths.
- **Authentication:** none. **Rate limits:** not documented (_verify_).
- **Licence:** the Commission's reuse policy (Decision 2011/833/EU) with acknowledgement; whether it covers EUDAMED public data is _verify_.
- **Module availability** (stated in every regulatory-history answer):

  | Module | Availability (as audited) | Acquired |
  | --- | --- | --- |
  | Actor registration | public; mandatory from 2026-05-28 | yes (`devices-eudamed-actors`) |
  | UDI/device registration | public; mandatory from 2026-05-28 | yes (`devices-eudamed-devices`) |
  | Notified bodies and certificates | public; mandatory from 2026-05-28 | yes (`devices-eudamed-certificates`) |
  | Market surveillance | mandatory for authorities from 2026-05-28; public content not verified | no: explicit gap |
  | Vigilance and post-market surveillance | not in mandatory use | no: explicit gap |
  | Clinical investigations and performance studies | not in mandatory use | no: explicit gap |

- **Revisions:** version number and last-update date per public record; a certificate status change (valid, suspended, withdrawn, expired) is a new revision ordered by certificate revision and status date.

## Data-minimisation decision (binding for every medical-devices module)

- **Stored:** regulatory identifiers (K and P numbers, supplement numbers, product codes, recall and report numbers, DIs, SRNs, certificate numbers); decision, recall, report and certificate dates as published; company names as published with city, state and country; device brand, model, catalogue number and description; MAUDE event type, report source, product problems and device fields; MAUDE narrative text (`mdr_text`) as published.
- **Never stored (dropped in the parser, listed under `minimisation.withheld`, refused by the store with `minimisation_violation`):** contact persons (510(k) `contact`, recall `additional_info_contact`, MAUDE `manufacturer_contact_*` and `reporter_*` fields, GUDID customer contacts, EUDAMED contact details and PRRC names); street addresses, postal codes, telephone numbers and e-mail addresses; every MAUDE `patient` block (age, sex, weight, ethnicity, race, patient problems and outcomes).
- **Narratives:** kept verbatim on adverse-event reports only and returned only to principals holding `knowledge:clinical:devices:narratives:read`; otherwise withheld and counted. Narratives never enter the source-pack documents.
- **Who may query:** `knowledge:clinical:read` with namespace access for records; the narrative scope for MAUDE text; answers that follow accepted identity matches also read `knowledge:ownership:read`.
- **Matching:** companies only; no person is a subject, matched or linked.
- **Retention:** retained with the record revision; no personal identifier is stored, so nothing personal remains to purge; no automatic expiry in the first coverage.

## Bounded first coverage

- **FDA:** the product codes and recall numbers named in each source's selection (at most 50 units per source, at most 5 pages of 100 rows per unit, never truncated); MAUDE reports and counts only for declared (product code, window) pairs of at most 366 days.
- **GUDID:** the primary DIs named in the selection (at most 50).
- **EUDAMED:** the SRNs, Basic UDI-DIs and certificate numbers named in the selection (at most 50 each) from the three public modules above.
- **Justification:** a device journey needs its product code, premarket numbers, recalls and a short report window; whole-endpoint or bulk downloads are not a bounded selection and are not acquired. Nothing implies complete coverage of a device class, a manufacturer or a market.

## LIVE_VERIFICATION

Every source is `unverified-live`: fixture-verified parsers in the documented
shapes (authored fixtures in `tests/fixtures/medical_devices/`, fictional
companies and devices), no dated live run from this runtime. The dated run and
cited demo belong to MD14 (#2723).
