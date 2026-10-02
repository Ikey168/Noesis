# Clinical Evidence: medical devices

The Clinical Evidence bundle's `clinical.devices` provider (#2654) answers:
*given a device, a manufacturer or an FDA product code, which regulatory
records did the regulators publish - device identifiers, clearances and
approvals with supplements, recalls and adverse-event reports - each with its
source, record revision and as-of time?*

It records what FDA and the EUDAMED public modules published. It never
detects safety signals, infers causality from adverse-event reports, gives
clinical advice or stores patient data beyond what regulators publish.

## Features

openFDA, AccessGUDID and EUDAMED coverage are three independent optional
features of the `clinical-evidence` bundle, all default off:
`medical-devices-fda`, `medical-devices-gudid` and `medical-devices-eudamed`.
Selecting any one binds the `clinical.devices` provider (capabilities
`clinical.devices-records`, `-identity` and `-monitoring`). Product safety,
Medicines and Corporate Ownership links degrade gracefully when those packs
are absent: the link is reported as `provider-missing`, never dropped.

## Sources

Nine sources in the `clinical-evidence` source pack (0.1.4), connector
`medical-devices`, all `unverified-live` until the dated live run (MD14,
#2723). Access decisions, licences, rate limits, revision models, EUDAMED
module availability and the minimisation decision are in the
[source audit](../development/medical-devices-evidence/source-audit.md).

| Source | Publisher | Units |
| --- | --- | --- |
| `clinical-devices-openfda-510k` | openFDA `/device/510k.json` | declared K numbers |
| `clinical-devices-openfda-pma` | openFDA `/device/pma.json` | declared P numbers (original and supplements) |
| `clinical-devices-openfda-classification` | openFDA `/device/classification.json` | declared product codes |
| `clinical-devices-openfda-recalls` | openFDA `/device/recall.json` + `/device/enforcement.json` | declared recall numbers |
| `clinical-devices-openfda-maude` | openFDA `/device/event.json` | product code + received-date window (at most one year) |
| `clinical-devices-accessgudid` | AccessGUDID device lookup | declared primary DIs |
| `clinical-devices-eudamed-actors` | EUDAMED actor module | declared SRNs |
| `clinical-devices-eudamed-devices` | EUDAMED UDI/device module | declared Basic UDI-DIs |
| `clinical-devices-eudamed-certificates` | EUDAMED notified bodies and certificates | declared certificate + notified body |

Acquisition runs through `noesis-knowledge-engine.run_source_pack_execution`
with the `clinical-evidence` pack and these source ids. EUDAMED vigilance,
clinical-investigation and market-surveillance modules are not public; every
answer lists them as gaps.

## Records

`noesis-medical-device-record-v2` (`src/kb/medical_devices_records.py`):
classification (product code), clearance (K number), approval and
approval-supplement (P number + supplement number), recall (recall number,
class and status as published), adverse-event report (report number, event
type and dates, FDA's caveats attached), published report count (product code
and window), device identifier (primary DI with package DIs and public
version), EUDAMED actor (SRN), device (Basic UDI-DI) and certificate (notified
body + number, status revisions). Every revision is immutable; a changed
publication is a new revision, an older version observed later never becomes
current, and a record the publisher stops answering becomes a `not-published`
revision - never a deletion.

**Minimisation** (`medical-devices-minimisation-v1`): patient sections,
reporter and contact persons, PRRCs, phones, emails and street addresses are
never stored; the store refuses any record carrying one. MAUDE narratives are
stored as FDA released them and returned only with
`knowledge:clinical:devices:narratives:read`.

## Journey

1. `propose_medical_device_identities` - devices across FDA, GUDID and EUDAMED
   by UDI-DI and premarket numbers (a shared product code is low evidence;
   names are never used for devices), manufacturers against Corporate
   Ownership entities by DUNS, LEI or SRN first (names as low evidence) and
   GUDID DIs against Products GTINs. `review_medical_device_identity` accepts
   or rejects with a reason (an entity identity decision with reviewer and
   time); `revert_medical_device_identity` reverts;
   `list_medical_device_identity_candidates` shows unmatched subjects.
2. `link_medical_device_records` - recalls to Product safety notices by recall
   number, combination products to Medicines records by drug application
   number, devices to trial registrations naming their identifiers, and
   manufacturers to ownership entities by accepted match; every link names its
   basis and both record revisions.
3. `medical_device_regulatory_history` - a device (K/P number, DI, Basic
   UDI-DI) or product code to its clearances, approvals, supplements, recalls
   and EU certificates as of a date, each citing the revision the source had
   published by then; US and EU side by side; later events, removals and gaps
   listed; a subject with no record says so.
4. `medical_device_adverse_event_counts` - MAUDE report counts per event type
   and period as published, beside the reports on record, with FDA's caveats,
   the query window and source revisions. Counts are **reports**, never rates,
   incidence or causal events.
5. `export_medical_device_evidence_bundle` - every item cited with source,
   record revision and as-of time; narratives are never exported.
6. `create_medical_device_monitor` / `run_medical_device_monitor` /
   `poll_medical_device_monitor` - subscribe to a device, manufacturer or
   product code; notices cite the new and previous revision and state what
   changed (new clearance, approval, supplement or recall; recall status or
   class change; certificate status change; removal). Platform subscriptions
   at committed watermarks; no new scheduler.

## Evidence

Offline only: `tests/unit/domains/test_medical_devices_*.py` (including the
acceptance journey `test_medical_devices_acceptance.py`) and
`tests/unit/composition/test_clinical_devices_composition.py` over fictional
fixtures. No live coverage is claimed; the live run and cited demo are MD14
(#2723).
