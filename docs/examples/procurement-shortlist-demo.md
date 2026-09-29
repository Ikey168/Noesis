# Public Procurement — explained shortlist demo (offline)

> **Evidence: offline authored fixtures only.** Notices come from `tests/fixtures/source_packs/procurement-*.json` (hand-written to mirror TED eForms search, UK OCDS and SAM.gov responses; buyers, suppliers and identifiers are fictional). They ran through the real source-pack runtime with fixture transports (receipt execution: injected). They are **not** live captures and say nothing about procedures open today. Live checks are recorded separately in `docs/development/procurement-evidence/` (the 2026-09-27 run was blocked for every provider).
>
> The supplier is **synthetic**. Eligibility is a requirements assessment against stated facts and cited notice text, not the contracting authority's decision; the match score orders options and is **not** a probability of winning. Award history is context only. Nothing was submitted and no buyer or portal was contacted.

Regenerate with `python scripts/procurement_demo.py --output docs/examples/procurement-shortlist-demo.md` (as of 2026-09-27 09:00 UTC; source-pack run status: complete).

## Synthetic supplier

Nordlicht Fixture IT GmbH: a small German IT service company (38 staff, turnover EUR 1.5 million), CPV interests 72250000 (system and support services), 72222300 (IT services) and 72400000 (internet services); jurisdictions DE and AT; certified ISO 9001 and ISO/IEC 20000-1; four comparable references; self-declares that five exclusion grounds do not apply; contract range EUR 50,000–1,000,000; needs at least 14 days to prepare; timezone Europe/Berlin.

Unknown profile facts (never defaulted): exclusion.conflict_of_interest, exclusion.distortion_of_competition, exclusion.environmental_social_labour, exclusion.misrepresentation, exclusion.prior_termination, preferences.watch_buyers, supplier.insurance_cover, supplier.naics_codes, supplier.sam_registered, supplier.set_aside_statuses, supplier.turnover_band, supplier.years_trading.

## Shortlist (one row per lot)

| Bucket | Procedure / lot | Provider | State | Deadline as published | Estimated value | Verdict | Score (coverage) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| apply_now | [IT service desk and cloud migration for the district office](https://ted.europa.eu/en/notice/-/detail/00612345-2026) — Service desk operation (LOT-0001) | ted | open | `2026-11-03+01:00 12:00:00+01:00` | 400,000 EUR est., VAT excluded | eligible | 0.92 (100%) |
| consider | [Website hosting and IT support](https://www.contractsfinder.service.gov.uk/Notice/ocds-b5fd17-fixture-cf-001-tender-1) | uk-cf | open | `2026-10-30T17:00:00Z` | 80,000 GBP est., VAT unknown | needs_clarification | 0.75 (73%) |
| consider | [Digital services: helpdesk and hosting](https://www.find-tender.service.gov.uk/Notice/ocds-h6vhtk-0fx001-tender-1) — User helpdesk (1) | uk-fts | open | `2026-11-10T12:00:00Z` | 400,000 GBP est., VAT excluded | needs_clarification | 0.72 (82%) |
| consider | [Canteen and catering services](https://ted.europa.eu/en/notice/-/detail/00620000-2026) — Catering (LOT-0001) | ted | open | `2026-10-30+01:00 11:00:00+01:00` | 600,000 EUR est., VAT excluded | eligible | 0.70 (91%) |
| consider | [IT Help Desk Support Services (fixture)](https://sam.gov/opp/fx0001aa11bb22cc33dd44ee55ff6601/view) | sam-gov | open | `2026-10-28T14:00:00-04:00` | unknown | needs_clarification | 0.60 (45%) |
| watch | [Data analytics platform for port operations (planned)](https://ted.europa.eu/en/notice/-/detail/00613000-2026) | ted | forthcoming | none known | 1,500,000 EUR est., VAT excluded | needs_clarification | 0.53 (73%) |
| watch | [Cloud Hosting Market Research (fixture)](https://sam.gov/opp/fx0002aa11bb22cc33dd44ee55ff6602/view) | sam-gov | forthcoming | none known | unknown | needs_clarification | 0.33 (27%) |
| excluded | [IT service desk and cloud migration for the district office](https://ted.europa.eu/en/notice/-/detail/00612345-2026) — Cloud migration (LOT-0002) | ted | open | `2026-11-03+01:00 12:00:00+01:00` | 900,000 EUR est., VAT excluded | ineligible | 0.93 (91%) |
| excluded | [Digital services: helpdesk and hosting](https://www.find-tender.service.gov.uk/Notice/ocds-h6vhtk-0fx001-tender-1) — Managed hosting (2) | uk-fts | open | `2026-11-10T12:00:00Z` | 600,000 GBP est., VAT excluded | ineligible | 0.68 (82%) |
| excluded | [Office software licences](https://ted.europa.eu/en/notice/-/detail/00598765-2026) — Licences (LOT-0001) | ted | closed | none known | 250,000 EUR est., VAT excluded | eligible | 0.62 (73%) |

## Why each lot is where it is

### IT service desk and cloud migration for the district office — LOT-0001 — apply_now

Notice `00612345-2026` (contract-notice), procedure revision 1, buyer Bezirksamt Fixture-Mitte von Berlin.

- **cpv_fit** (1.00, weight 3): CPV 72253000 lies within your interest 72250000
- **jurisdiction** (1.00, weight 2): performed in DE (within your jurisdictions)
- **value_fit** (1.00, weight 2): estimated 400000 EUR (VAT excluded; estimated, not awarded); within your contract value range
- **submission_effort** (0.60, weight 1): 9 cited requirement(s), 1 document set(s)
- **deadline_feasibility** (1.00, weight 2): 37 days left (your minimum preparation time: 14 days); deadline as published: '2026-11-03+01:00 12:00:00+01:00'
- **incumbency_context** (0.50, weight 1): Helpdesk Fixture Services GmbH holds the most recent award from Bezirksamt Fixture-Mitte von Berlin in this CPV branch (2024-03-15, notice 00587654-2024).; another supplier is the incumbent (context, not a probability)
- **Award context (not evidence the procedure is open):** 2026-03-01 Fixture Digital Ltd (same CPV branch, notice ocds-h6vhtk-0fx900-contractAmendment-1); 2025-02-10 Fixture Digital Ltd (same CPV branch, notice ocds-h6vhtk-0fx900-award-1); 2024-03-15 Helpdesk Fixture Services GmbH (same buyer and CPV, notice 00587654-2024)
- **Next step:** Start a bid workspace; work the cited checklist before the published deadline.

### Website hosting and IT support — consider

Notice `ocds-b5fd17-fixture-cf-001-tender-1` (contract-notice), procedure revision 1, buyer Fixture Parish Council.

- **cpv_fit** (1.00, weight 3): CPV 72400000 lies within your interest 72400000
- **jurisdiction** (0.00, weight 2): performed in GB; outside your stated jurisdictions DE, AT
- **value_fit** (unknown, weight 2): estimated 80000 GBP (VAT unknown; estimated, not awarded); your range is in EUR; no currency conversion is applied
- **submission_effort** (1.00, weight 1): 0 cited requirement(s), 0 document set(s)
- **deadline_feasibility** (1.00, weight 2): 33 days left (your minimum preparation time: 14 days); deadline as published: '2026-10-30T17:00:00Z'
- **incumbency_context** (unknown, weight 1): No award from Fixture Parish Council in this CPV branch is in the acquired history.
- **Next step:** the notice states no machine-readable criteria; read the procurement documents; outside your stated jurisdictions

### Digital services: helpdesk and hosting — 1 — consider

Notice `ocds-h6vhtk-0fx001-tender-1` (contract-notice), procedure revision 1, buyer Fixture Borough Council.

- **cpv_fit** (1.00, weight 3): CPV 72253000 lies within your interest 72250000; CPV 72415000 lies within your interest 72400000
- **jurisdiction** (0.00, weight 2): performed in GB; outside your stated jurisdictions DE, AT
- **value_fit** (unknown, weight 2): estimated 400000 GBP (VAT excluded; estimated, not awarded); your range is in EUR; no currency conversion is applied
- **submission_effort** (1.00, weight 1): 4 cited requirement(s), 1 document set(s)
- **deadline_feasibility** (1.00, weight 2): 44 days left (your minimum preparation time: 14 days); deadline as published: '2026-11-10T12:00:00Z'
- **incumbency_context** (0.50, weight 1): Fixture Digital Ltd holds the most recent award from Fixture Borough Council in this CPV branch (2025-02-10, notice ocds-h6vhtk-0fx900-award-1).; another supplier is the incumbent (context, not a probability)
- **Unknown:** `uk-fts:criterion:1` — supplier.annual_turnover.GBP
- **Award context (not evidence the procedure is open):** 2026-03-01 Fixture Digital Ltd (same buyer and CPV, notice ocds-h6vhtk-0fx900-contractAmendment-1); 2025-02-10 Fixture Digital Ltd (same buyer and CPV, notice ocds-h6vhtk-0fx900-award-1); 2024-03-15 Helpdesk Fixture Services GmbH (same CPV branch, notice 00587654-2024)
- **Next step:** state supplier.annual_turnover.GBP; outside your stated jurisdictions

### Canteen and catering services — LOT-0001 — consider

Notice `00620000-2026` (contract-notice), procedure revision 1, buyer Bezirksamt Fixture-Mitte von Berlin.

- **cpv_fit** (0.00, weight 3): CPV 55520000 is outside your stated interests
- **jurisdiction** (1.00, weight 2): performed in DE (within your jurisdictions)
- **value_fit** (1.00, weight 2): estimated 600000 EUR (VAT excluded; estimated, not awarded); within your contract value range
- **submission_effort** (1.00, weight 1): 1 cited requirement(s), 0 document set(s)
- **deadline_feasibility** (1.00, weight 2): 33 days left (your minimum preparation time: 14 days); deadline as published: '2026-10-30+01:00 11:00:00+01:00'
- **incumbency_context** (unknown, weight 1): No award from Bezirksamt Fixture-Mitte von Berlin in this CPV branch is in the acquired history.
- **Award context (not evidence the procedure is open):** 2024-03-15 Helpdesk Fixture Services GmbH (same buyer, notice 00587654-2024)
- **Next step:** outside your CPV interests

### IT Help Desk Support Services (fixture) — consider

Notice `fx0001aa11bb22cc33dd44ee55ff6601` (contract-notice), procedure revision 1, buyer OFFICE OF ACQUISITION.

- **cpv_fit** (unknown, weight 3): the notice states no CPV code for this lot
- **jurisdiction** (0.00, weight 2): performed in US; outside your stated jurisdictions DE, AT
- **value_fit** (unknown, weight 2): the lot's estimated value is not stated
- **submission_effort** (1.00, weight 1): 1 cited requirement(s), 1 document set(s)
- **deadline_feasibility** (1.00, weight 2): 31 days left (your minimum preparation time: 14 days); deadline as published: '2026-10-28T14:00:00-04:00'
- **incumbency_context** (unknown, weight 1): the notice states no CPV code; incumbency cannot be derived
- **Unknown:** `sam-gov:set-aside` — supplier.set_aside_statuses
- **Award context (not evidence the procedure is open):** 2025-08-01 Fixture Federal Solutions LLC (same buyer, notice fx0003aa11bb22cc33dd44ee55ff6603)
- **Next step:** state supplier.set_aside_statuses; outside your stated jurisdictions

### Data analytics platform for port operations (planned) — watch

Notice `00613000-2026` (prior-information), procedure revision 1, buyer Hafenbehörde Fixture-Hamburg AöR.

- **cpv_fit** (0.40, weight 3): CPV 72300000 only shares the division of your interest 72250000, 72222300, 72400000
- **jurisdiction** (1.00, weight 2): performed in DE (within your jurisdictions)
- **value_fit** (0.00, weight 2): estimated 1500000 EUR (VAT excluded; estimated, not awarded); above your maximum of 1000000 EUR
- **submission_effort** (1.00, weight 1): 0 cited requirement(s), 0 document set(s)
- **deadline_feasibility** (unknown, weight 2): no upcoming submission deadline is known
- **incumbency_context** (unknown, weight 1): No award from Hafenbehörde Fixture-Hamburg AöR in this CPV branch is in the acquired history.
- **Status notes:** prior information notice: planned procurement, not a call for competition
- **Next step:** Monitor for the contract notice.

### Cloud Hosting Market Research (fixture) — watch

Notice `fx0002aa11bb22cc33dd44ee55ff6602` (prior-information), procedure revision 1, buyer OFFICE OF ACQUISITION.

- **cpv_fit** (unknown, weight 3): the notice states no CPV code for this lot
- **jurisdiction** (0.00, weight 2): performed in US; outside your stated jurisdictions DE, AT
- **value_fit** (unknown, weight 2): the lot's estimated value is not stated
- **submission_effort** (1.00, weight 1): 0 cited requirement(s), 0 document set(s)
- **deadline_feasibility** (unknown, weight 2): no upcoming submission deadline is known
- **incumbency_context** (unknown, weight 1): the notice states no CPV code; incumbency cannot be derived
- **Award context (not evidence the procedure is open):** 2025-08-01 Fixture Federal Solutions LLC (same buyer, notice fx0003aa11bb22cc33dd44ee55ff6603)
- **Status notes:** prior information notice: planned procurement, not a call for competition
- **Next step:** Monitor for the contract notice.

### IT service desk and cloud migration for the district office — LOT-0002 — excluded

Notice `00612345-2026` (contract-notice), procedure revision 1, buyer Bezirksamt Fixture-Mitte von Berlin.

- **cpv_fit** (1.00, weight 3): CPV 72222300 lies within your interest 72222300
- **jurisdiction** (1.00, weight 2): performed in DE (within your jurisdictions)
- **value_fit** (1.00, weight 2): estimated 900000 EUR (VAT excluded; estimated, not awarded); within your contract value range
- **submission_effort** (0.30, weight 1): 9 cited requirement(s), 1 document set(s), 1 criterion/criteria needing manual review
- **deadline_feasibility** (1.00, weight 2): 37 days left (your minimum preparation time: 14 days); deadline as published: '2026-11-03+01:00 12:00:00+01:00'
- **incumbency_context** (unknown, weight 1): No award from Bezirksamt Fixture-Mitte von Berlin in this CPV branch is in the acquired history.
- **Disqualifiers (cited):** ted:LOT-0002:criterion:1, ted:LOT-0002:criterion:2
- **Unknown:** `ted:LOT-0002:criterion:3` — no machine rule or approved interpretation for this requirement text
- **Award context (not evidence the procedure is open):** 2025-04-30 Nordlicht Fixture IT GmbH (same CPV branch, notice 00543210-2025); 2024-03-15 Helpdesk Fixture Services GmbH (same buyer, notice 00587654-2024)
- **Next step:** Not applicable: ted:LOT-0002:criterion:1; ted:LOT-0002:criterion:2

### Digital services: helpdesk and hosting — 2 — excluded

Notice `ocds-h6vhtk-0fx001-tender-1` (contract-notice), procedure revision 1, buyer Fixture Borough Council.

- **cpv_fit** (1.00, weight 3): CPV 72253000 lies within your interest 72250000; CPV 72415000 lies within your interest 72400000
- **jurisdiction** (0.00, weight 2): performed in GB; outside your stated jurisdictions DE, AT
- **value_fit** (unknown, weight 2): estimated 600000 GBP (VAT excluded; estimated, not awarded); your range is in EUR; no currency conversion is applied
- **submission_effort** (0.60, weight 1): 5 cited requirement(s), 1 document set(s)
- **deadline_feasibility** (1.00, weight 2): 44 days left (your minimum preparation time: 14 days); deadline as published: '2026-11-10T12:00:00Z'
- **incumbency_context** (0.50, weight 1): Fixture Digital Ltd holds the most recent award from Fixture Borough Council in this CPV branch (2025-02-10, notice ocds-h6vhtk-0fx900-award-1).; another supplier is the incumbent (context, not a probability)
- **Disqualifiers (cited):** uk-fts:criterion:3
- **Unknown:** `uk-fts:criterion:2` — supplier.annual_turnover.GBP
- **Award context (not evidence the procedure is open):** 2026-03-01 Fixture Digital Ltd (same buyer and CPV, notice ocds-h6vhtk-0fx900-contractAmendment-1); 2025-02-10 Fixture Digital Ltd (same buyer and CPV, notice ocds-h6vhtk-0fx900-award-1); 2024-03-15 Helpdesk Fixture Services GmbH (same CPV branch, notice 00587654-2024)
- **Next step:** Not applicable: uk-fts:criterion:3

### Office software licences — LOT-0001 — excluded

Notice `00598765-2026` (contract-notice), procedure revision 1, buyer Bezirksamt Fixture-Mitte von Berlin.

- **cpv_fit** (0.00, weight 3): CPV 48000000 is outside your stated interests
- **jurisdiction** (1.00, weight 2): performed in DE (within your jurisdictions)
- **value_fit** (1.00, weight 2): estimated 250000 EUR (VAT excluded; estimated, not awarded); within your contract value range
- **submission_effort** (1.00, weight 1): 1 cited requirement(s), 0 document set(s)
- **deadline_feasibility** (unknown, weight 2): no upcoming submission deadline is known
- **incumbency_context** (unknown, weight 1): No award from Bezirksamt Fixture-Mitte von Berlin in this CPV branch is in the acquired history.
- **Award context (not evidence the procedure is open):** 2024-03-15 Helpdesk Fixture Services GmbH (same buyer, notice 00587654-2024)
- **Status notes:** the submission deadline has passed
- **Next step:** Not applicable: closed

## Eligibility with cited passages for the top lot

IT service desk and cloud migration for the district office — Service desk operation (LOT-0001); every conclusion cites notice `00612345-2026` revision 1.

| Requirement | Category | Result | Profile facts used | Cited passage |
| --- | --- | --- | --- | --- |
| `ted:exclusion:crime-org` | exclusion | met | exclusion.criminal_conviction | Participation in a criminal organisation (final conviction) excludes the tenderer. |
| `ted:exclusion:corruption` | exclusion | met | exclusion.criminal_conviction | Corruption (final conviction) excludes the tenderer. |
| `ted:exclusion:tax-pay` | exclusion | met | exclusion.tax_arrears | Breach of obligations relating to the payment of taxes excludes the tenderer. |
| `ted:exclusion:socsec-pay` | exclusion | met | exclusion.social_security_arrears | Breach of obligations relating to the payment of social security contributions excludes the tenderer. |
| `ted:exclusion:bankruptcy` | exclusion | met | exclusion.insolvency | Bankruptcy excludes the tenderer. |
| `ted:exclusion:prof-misconduct` | exclusion | met | exclusion.professional_misconduct | Grave professional misconduct excludes the tenderer. |
| `ted:LOT-0001:criterion:1` | economic-financial | met | supplier.annual_turnover.EUR | Minimum annual turnover of EUR 800 000 in each of the last three financial years. |
| `ted:LOT-0001:criterion:2` | technical-professional | met | supplier.references_count | At least three comparable references from the last three years. |
| `ted:LOT-0001:criterion:3` | certification | met | supplier.certifications | ISO/IEC 20000-1 certification of the service management system is required. |

## Incumbency (derived from award history)

Helpdesk Fixture Services GmbH holds the most recent award from Bezirksamt Fixture-Mitte von Berlin in this CPV branch (2024-03-15, notice 00587654-2024).

_derived from acquired award history; context only, never evidence that a procedure is open, and limited to the notices acquired._

## Bid workspace checklist for the top lot

Workspace `procurement-workspace:9b9512b4074e72f1f5fe686505aaaa56` (a research project; Preparation only. Noesis never submits bids and never contacts buyers or procurement portals.)

| Item | Kind | Text | Due / source |
| --- | --- | --- | --- |
| `req:ted:exclusion:crime-org` | requirement | Participation in a criminal organisation (final conviction) excludes the tenderer. | BT-67(a)-Procedure |
| `req:ted:exclusion:corruption` | requirement | Corruption (final conviction) excludes the tenderer. | BT-67(a)-Procedure |
| `req:ted:exclusion:tax-pay` | requirement | Breach of obligations relating to the payment of taxes excludes the tenderer. | BT-67(a)-Procedure |
| `req:ted:exclusion:socsec-pay` | requirement | Breach of obligations relating to the payment of social security contributions excludes the tenderer. | BT-67(a)-Procedure |
| `req:ted:exclusion:bankruptcy` | requirement | Bankruptcy excludes the tenderer. | BT-67(a)-Procedure |
| `req:ted:exclusion:prof-misconduct` | requirement | Grave professional misconduct excludes the tenderer. | BT-67(a)-Procedure |
| `req:ted:LOT-0001:criterion:1` | requirement | Minimum annual turnover of EUR 800 000 in each of the last three financial years. | BT-750-Lot |
| `req:ted:LOT-0001:criterion:2` | requirement | At least three comparable references from the last three years. | BT-750-Lot |
| `req:ted:LOT-0001:criterion:3` | requirement | ISO/IEC 20000-1 certification of the service management system is required. | BT-750-Lot |
| `doc:exclusion` | document/declaration | Self-declaration on exclusion grounds (e.g. ESPD Part III) | BT-67(a)-Procedure |
| `doc:ted:LOT-0001:criterion:1` | document/financial | Evidence of economic and financial standing: Minimum annual turnover of EUR 800 000 in each of the last three  | BT-750-Lot |
| `doc:ted:LOT-0001:criterion:2` | document/reference | Evidence of technical and professional ability (references, staff CVs): At least three comparable references f | BT-750-Lot |
| `doc:ted:LOT-0001:criterion:3` | document/certificate | Copy of the required certificate: ISO/IEC 20000-1 certification of the service management system is required. | BT-750-Lot |
| `doc:notice:60ecb871eee01e5f` | document/procurement-documents | Obtain and review: Procurement documents (https://vergabe.fixture-mitte.berlin.example/docs/2026-017) | https://vergabe.fixture-mitte.berlin.example/docs/2026-017 |
| `deadline:submission:LOT-0001` | milestone | submission deadline as published: 2026-11-03+01:00 12:00:00+01:00 | 2026-11-03T12:00:00+01:00 |
| `plan:go-no-go-decision:LOT-0001` | milestone | Suggested internal milestone: go/no-go decision (21 days before the published submission deadline) | 2026-10-13T12:00:00+01:00 |
| `plan:final-internal-review:LOT-0001` | milestone | Suggested internal milestone: final internal review (3 days before the published submission deadline) | 2026-10-31T12:00:00+01:00 |
| `deadline:clarification:LOT-0001` | milestone | clarification deadline as published: 2026-10-20+02:00 23:59:59+02:00 | 2026-10-20T23:59:59+02:00 |
| `deadline:opening:LOT-0001` | milestone | opening deadline as published: 2026-11-03+01:00 12:30:00+01:00 | 2026-11-03T12:30:00+01:00 |

## Sensitivity

| Criterion change | Positions changed | Top changed |
| --- | --- | --- |
| cpv_fit:dropped | 7 | False |
| cpv_fit:doubled | 2 | False |
| jurisdiction:dropped | 5 | False |
| jurisdiction:doubled | 5 | False |
| value_fit:dropped | 0 | False |
| value_fit:doubled | 5 | False |
| submission_effort:dropped | 0 | False |
| submission_effort:doubled | 0 | False |
| deadline_feasibility:dropped | 2 | False |
| deadline_feasibility:doubled | 0 | False |
| incumbency_context:dropped | 2 | False |
| incumbency_context:doubled | 2 | False |
