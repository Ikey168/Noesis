# Medical devices: source-contract audit, minimisation decision and bounded coverage (MD01)

Tracking: #2654 · delivery issue #2658 · recorded 2026-09-30.

This audit sets out, per source, what the Clinical Evidence pack's
`clinical.devices` provider may acquire, how, and on what terms. **The
publishers' documentation and terms pages (open.fda.gov, accessgudid.nlm.nih.gov,
ec.europa.eu/tools/eudamed) could not be fetched from the authoring runtime
(egress blocked), so the terms were not re-verified live.** Endpoints, fields
and terms below come from the issue's references and the publishers'
documentation as the author knows it. Every item marked _verify_ must be
checked against the live pages, the live terms and a real response before the
first dated live run (MD14, #2723). No source is `live` until that run exists.

The machine-readable copy of these decisions is `PROVIDER_CONTRACTS`,
`EUDAMED_MODULES`, `IDENTIFIERS`, `MINIMISATION`, `BOUNDED_COVERAGE`,
`DECLINED`, `MAUDE_CAVEATS` and `LIVE_VERIFICATION` in
`src/ingestion/medical_devices_sources.py`. Each source entry in
`config/source_packs/clinical-evidence.json` (0.1.4; earlier sources verbatim)
states `medical_devices.live_verification: unverified-live` and
`medical_devices.minimisation: medical-devices-minimisation-v1`, and the MCP
tool `medical_device_source_contracts` returns the same decisions.

Non-goals for every source: no safety-signal detection (no disproportionality,
no trend or threshold), no causality from adverse-event reports, no clinical
advice, and no patient data beyond what regulators publish. Decision codes,
recall classes and statuses, event types and certificate statuses are kept
**as the regulator published them**.

## Access decisions

| Source (source-pack id) | Publisher | Delivers | Access | Decision |
| --- | --- | --- | --- | --- |
| `clinical-devices-openfda-510k` | FDA via openFDA `/device/510k.json` | premarket notifications: K number, applicant, device name, product code, decision code and description, decision and receipt dates, clearance type | `search=k_number:"K…"`, one declared K number per unit | `unverified-live` |
| `clinical-devices-openfda-pma` | FDA via openFDA `/device/pma.json` | premarket approvals and every supplement: P number, supplement number, type and reason, decision code and date, trade and generic name, product code, AO statement | `search=pma_number:"P…"`, `limit=100`; one declared P number per unit; more than 100 results is `budget_exhausted` | `unverified-live` |
| `clinical-devices-openfda-classification` | FDA via openFDA `/device/classification.json` | product code, device name, class, regulation number, review panel, implant and life-sustaining flags | `search=product_code:"…"`, one declared product code per unit | `unverified-live` |
| `clinical-devices-openfda-recalls` | FDA via openFDA `/device/recall.json` and `/device/enforcement.json` | recall number (`product_res_number`), event id, status, recalling firm, reason, root cause, action, product code, K and P numbers; class (`classification`) and status from the enforcement report | two requests per declared recall number (the enforcement report may be absent: the class is then unknown, never guessed) | `unverified-live` |
| `clinical-devices-openfda-maude` | FDA via openFDA `/device/event.json` (MAUDE) | reports: report number, MDR report key, event type, event/receipt/report dates, report source, product problems, device fields (brand, generic name, manufacturer, product code, model, UDI-DI), narratives (`mdr_text`) as published; the published `count=event_type.exact` tally | one declared product code and a received-date window of at most one year per unit, `limit=100`; the count query for the same search | `unverified-live` |
| `clinical-devices-accessgudid` | NLM and FDA, AccessGUDID | GUDID device record: primary DI and issuing agency, package and secondary DIs, brand, version/model, catalog number, labeler and DUNS, product codes, premarket submission numbers, GMDN terms, public version number and date, record and distribution status | `/api/v3/devices/lookup.json?di=…`, one declared primary DI per unit (_verify_ the version path and field names) | `unverified-live` |
| `clinical-devices-eudamed-actors` | European Commission, EUDAMED actor module | actor SRN, name, abbreviated name, role, country, city, status, version | the public site's JSON backend `/api/actors?srn=…` (_verify_: no documented public API) | `unverified-live` |
| `clinical-devices-eudamed-devices` | European Commission, EUDAMED UDI/device module | Basic UDI-DI, manufacturer and authorised-representative SRN, device name, model, risk class, legislation (MDR/IVDR/legacy), UDI-DIs with status and trade name, certificate numbers, version | `/api/devices/basicUdiData?basicUdi=…` (_verify_) | `unverified-live` |
| `clinical-devices-eudamed-certificates` | European Commission, EUDAMED notified-bodies and certificates module | certificate number, notified body number and name, type, status, issue/validity/expiry dates, manufacturer SRN, covered Basic UDI-DIs, status-change reason, version | `/api/certificates?certificateNumber=…&notifiedBody=…` (_verify_) | `unverified-live` |

Declined (documented, not acquired): openFDA registration and listing
(owner/operator contact persons and addresses; manufacturers come from the
clearance, approval, GUDID and EUDAMED records instead), MAUDE patient
sections, the GUDID full and delta releases (bulk, outside the bounded
coverage) and the EUDAMED vigilance module (not public).

## Per-source contract

| Source | Authentication and key handling | Licence, redistribution and attribution | Rate limits | Updates, corrections and removals |
| --- | --- | --- | --- | --- |
| openFDA device endpoints | none; an optional api.data.gov key (`NOESIS_OPENFDA_API_KEY`) raises quotas but is not used by this connector and is never written to a record, receipt or log | openFDA Terms of Service: FDA data are public-domain US government works (CC0 where FDA states it); no FDA endorsement may be implied; openFDA's `meta.disclaimer` ("Do not rely on openFDA to make decisions regarding medical care …") is kept on every record and returned with every citation (_verify_ the current terms page) | 240 requests per minute and 1,000 per day per IP without a key (_verify_); at most 25 units per source per run | `meta.last_updated` is the dataset revision and the record's as-of date; a changed payload for a key is a new revision; a new PMA supplement is a new record; a dataset stamp alone is not a revision; a declared unit answered `NOT_FOUND` becomes a `not-published` revision (a removal is a revision, never a deletion) |
| AccessGUDID | none | GUDID data are public US government data published by NLM; cite AccessGUDID; no NLM or FDA endorsement (_verify_ the NLM terms page) | none published; reasonable use (_verify_); at most 25 DIs per run | `publicVersionNumber` / `publicVersionDate` are the revision; a new public version with changed content is a new revision; an older version observed later is kept as an `older-observation` and never becomes current |
| EUDAMED public modules | none | Commission reuse policy (Decision 2011/833/EU) per the EUDAMED legal notice, reuse with acknowledgement; personal data of contact persons and PRRCs are not reused (_verify_ the legal notice) | none published (_verify_); at most 25 units per source per run | EUDAMED version number and last-update date are the revision; a certificate status change (issued, suspended, withdrawn, expired, refused) is a new revision; an empty search result for a declared unit is a `not-published` revision |

### EUDAMED module availability

| Module | Status | Consequence |
| --- | --- | --- |
| Actor registration | available | acquired (`eudamed-actor-json`) |
| UDI/device registration | available | acquired (`eudamed-device-json`) |
| Notified bodies and certificates | available | acquired (`eudamed-certificate-json`) |
| Vigilance and post-market surveillance | not public | explicit gap in every answer; field safety notices are not acquired |
| Clinical investigations and performance studies | not public | explicit gap |
| Market surveillance | not public (competent authorities) | explicit gap |

## Data-minimisation decision (`medical-devices-minimisation-v1`)

The sources publish some personal data. The decision:

* **Excluded (never stored):** MAUDE patient sections (age, sex, weight,
  ethnicity, outcomes, treatments); MAUDE reporter occupation and the
  manufacturer, distributor and reporter contact names, phones, emails and
  street addresses; the 510(k) and PMA contact person and street address;
  GUDID customer contact phone and email; EUDAMED contact persons and persons
  responsible for regulatory compliance (PRRC); street addresses and
  postcodes of firms. The parsers never copy these fields and record only how
  many sections were excluded; the record store refuses any record that still
  carries such a field (`minimisation_violation`, enforced at write time in
  `src/kb/medical_devices_records.py`).
* **Stored:** organisation names, cities, states and countries as published;
  device identifiers and names; decision, recall, certificate and report
  fields as published.
* **Restricted:** MAUDE narratives (`mdr_text`) are stored verbatim as FDA
  released them (FDA redacts them before release, but they can still describe
  a patient). They are returned only to principals holding
  `knowledge:clinical:devices:narratives:read`; everyone else sees that a
  narrative exists and is withheld. Narratives are never exported in an
  evidence bundle.
* **Retention:** revisions are kept for provenance; deleting the namespace
  removes them. No personal field is ever written, so none needs purging.
* **Who may query:** namespace readers with `knowledge:clinical:read`;
  narratives additionally need the narrative scope. MCP answers are checked
  for personal fields and assessment keys before they are returned.

## Adverse-event report caveats

Every MAUDE report and count carries FDA's published limitations
(`MAUDE_CAVEATS`): passive surveillance with incomplete, inaccurate, untimely,
unverified or biased reports and duplicates; a report does not establish that
a device caused an event; counts cannot estimate incidence or prevalence or
compare devices because the number of devices in use is not known; reporting
is influenced by publicity, litigation and reporting requirements, and
reports may be revised. Counts are labelled **reports** and are never
presented as rates, incidence or causal events.

## Stable identifiers

| Source | Identifiers |
| --- | --- |
| openFDA | 510(k) K number; PMA P number + supplement number; three-letter product code; recall number (Z-nnnn-yyyy) and recall event id; MDR report number and report key; device UDI-DI where a report publishes one |
| AccessGUDID | primary DI (GS1, HIBCC or ICCBBA); package and secondary DIs; labeler DUNS; premarket submission numbers; FDA product codes |
| EUDAMED | actor SRN; Basic UDI-DI; UDI-DI; certificate number + notified body number |

Devices are matched across registries by UDI-DI and premarket numbers
(product codes are low evidence; names are never used), and manufacturers to
Corporate Ownership entities by DUNS, LEI or SRN before names (MD07).

## Bounded coverage

* **Entities:** declared devices only - K and P numbers, product codes,
  recall numbers, primary DIs, SRNs, Basic UDI-DIs and certificates listed in
  the source entries. No search crawl. Fixtures: the fictional Exampla Medical
  and Northwind Medtech devices.
* **Places:** US (FDA) and EU (EUDAMED); shown side by side, never merged.
* **Periods:** clearances, approvals and recalls decided or initiated from
  2020-01-01; MAUDE windows of at most one year per unit.
* **Caps:** at most 25 units per source per run and 100 results per request; a
  larger result is `budget_exhausted`, never truncated.
* **Justification:** enough to answer a device's regulatory history and report
  counts for declared devices within unauthenticated quotas, while staying away
  from bulk personal data.

## LIVE_VERIFICATION

| Provider | Status | Outstanding |
| --- | --- | --- |
| `openfda-device` | `unverified-live` | verify the terms page, rate limits and one real response per endpoint (MD14) |
| `accessgudid` | `unverified-live` | verify the lookup path and version fields and the NLM terms (MD14) |
| `eudamed` | `unverified-live` | verify the JSON backend paths and fields and the legal notice; confirm module availability (MD14) |
