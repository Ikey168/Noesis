# Clinical Evidence: medical devices

The `clinical.devices` provider of the Clinical Evidence bundle (#2654) keeps
medical-device regulatory records as regulators published them: openFDA 510(k)
clearances, PMA approvals with their supplements, product-code classifications,
recalls and enforcement reports, MAUDE adverse-event reports and report counts,
AccessGUDID device identifiers and EUDAMED public actor, device and certificate
records. Every record carries its source, record revision and as-of time.

It answers a device's regulatory history as of a date and adverse-event report
counts with the source's caveats. It does **not** detect safety signals, infer
causality from adverse-event reports, give clinical advice or store patient data
beyond what regulators publish.

Source contracts, licences, rate limits, EUDAMED module availability and the
data-minimisation decision:
[source audit](../development/medical-devices-evidence/source-audit.md).

## Enable

Three optional features of `packs/clinical-evidence/manifest.json`, all off by
default and independent of each other:

| Feature | Sources (`clinical-evidence` 0.1.4) |
| --- | --- |
| `medical-devices-fda` | `devices-fda-510k`, `devices-fda-pma`, `devices-fda-classification`, `devices-fda-recalls`, `devices-fda-enforcement`, `devices-fda-maude-reports`, `devices-fda-maude-counts` |
| `medical-devices-gudid` | `devices-gudid-identifiers` |
| `medical-devices-eudamed` | `devices-eudamed-actors`, `devices-eudamed-devices`, `devices-eudamed-certificates` |

Selecting any of them binds `clinical.devices` together with the ownership
identity state machine, entity identity, subscriptions and the source runtime.
Product safety, Medicines, trial, Products and ownership links are optional and
report `provider_unavailable` when those providers or records are absent.

Each source declares a bounded selection (product codes, recall numbers,
product-code windows of at most 366 days, primary DIs or EUDAMED documents).
Acquire through the shared runtime (`run_source_pack_execution` with pack
`clinical-evidence`). The openFDA sources use the optional
`NOESIS_OPENFDA_API_KEY`, sent only as a request parameter. Every source is
`unverified-live`; the EUDAMED document paths are placeholders until MD14.

## Records and revisions

| Record kind | Key | Revision when |
| --- | --- | --- |
| `classification` | product code | the published class, regulation or panel changes |
| `clearance` | K number | a published field changes |
| `approval` | P number | a supplement is newly listed, or a published field changes |
| `supplement` | P number and supplement number | a published field changes |
| `recall` | recall number (one chain per source: recall and enforcement endpoints) | status, class or dates change |
| `adverse-event-report` | MAUDE report number | a published field changes |
| `adverse-event-count` | product code and window | the published counts change |
| `device-identifier` | GUDID primary DI | a new GUDID version is published |
| `actor`, `eudamed-device`, `certificate` | SRN, Basic UDI-DI, certificate number | version or certificate status changes |

Corrections and removals by the source are revisions, never deletions; an older
publication delivered later is kept as an `older-observation` and never becomes
current. `medical_device_record_history` returns the chain and the revision in
force at a date, by the source's own date or by observation.

## Ask

- `medical_device_regulatory_history(namespace, subject, as_of)` — subject is a
  DI, Basic UDI-DI, K or P number, product code, recall number or SRN. US and EU
  are shown separately; each event cites its revision; decisions after the date
  are listed as later events; EUDAMED modules that are not acquired are stated;
  a subject with no records is `none_on_record`.
- `medical_device_adverse_event_counts(namespace, subject, window_from,
  window_to)` — MAUDE report counts per event type and period as openFDA
  published them, beside the reports on record counted per month. Counts are
  reports, never rates or causal events; the caveats, window and source
  revisions come with every answer.
- `export_medical_devices_evidence_bundle` — either answer as an evidence
  bundle whose every assertion cites source, record revision and as-of time.

## Identity and links

`propose_medical_device_identity_matches` offers reviewable candidates:
devices across GUDID and EUDAMED by UDI-DI, devices and Products identities by
GTIN, manufacturers through an accepted device match or a shared K/P number, and
manufacturers and Corporate Ownership entities by name and country (low
confidence). Nothing is merged or accepted automatically, devices are never
matched by name, and unmatched subjects stay visible
(`list_medical_devices_unmatched`). Review with
`review_medical_device_identity_match`, undo with
`revert_medical_device_identity_match`.

`link_medical_device_records` links recalls to Product safety notices by recall
number, records citing a Drugs@FDA application to the Medicines record, devices
to trials naming their K/P number or DI, and manufacturers and devices to
ownership and Products records through accepted matches. Links cite both
revisions and their basis; missing targets are reported.

## Monitor

`create_medical_devices_monitor` watches a device, a manufacturer or a product
code; `run_medical_devices_monitor` reports new clearances, approvals,
supplements, recalls, recall status and class changes, certificate status
changes and GUDID versions, citing the new and previous revisions.
Adverse-event reports are never notified. Monitors are knowledge subscriptions;
there is no new scheduler.

## Minimisation

Contact persons, street addresses, telephone numbers, e-mail addresses and
MAUDE patient blocks are dropped by the parsers and refused by the store. MAUDE
narratives are kept as published and returned only with
`knowledge:clinical:devices:narratives:read`.
