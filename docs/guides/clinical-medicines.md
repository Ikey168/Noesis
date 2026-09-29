# Clinical Evidence: medicines regulation

The optional `medicines` feature of the Clinical Evidence bundle (#2214) takes a
medicine or active substance to its regulatory timeline as published:
marketing authorisations and withdrawals, label revisions with the sections that
changed, and FDA Drug Safety Communications, each with source, revision and
as-of time, linked to trials and publications the Clinical pack already holds
only where the regulator's own text cites them.

**Boundary.** Regulatory records are quoted as each regulator published them. The
feature gives no prescribing, dosing or treatment advice and no efficacy or
safety verdict beyond quoting the regulator. Dosing sections (SmPC 4.2 and 4.9,
SPL Dosage and administration, Dosage forms and strengths, Overdosage) are listed
as omitted and never quoted. FAERS figures remain reporting counts.

## Sources

| Source | How | Revisions |
| --- | --- | --- |
| EMA medicine data and EPARs | `medicines` connector, provider `ema-epar`: the medicines export, the product information (SmPC) and withdrawal public statements for pinned product numbers | EPAR revision number and date; earlier revisions are the ones acquired earlier |
| Drugs@FDA | the existing `openfda` connector with the `drugsfda-submissions` endpoint (same client, same optional API key, openFDA disclaimer on every record) | one record per submission; marketing status changes kept as history |
| DailyMed | `medicines` connector, provider `dailymed`: SPL history and current SPL document per pinned set id | one label revision per SPL version, sections keyed by LOINC code |
| FDA Drug Safety Communications | `medicines` connector, provider `fda-dsc`: pinned communication pages | an update is a new revision of the same record |
| RxNorm (RxNav) | `RxNavClient`, used by identity resolution | the RxNorm release is stored on every match |

Access, terms, attribution, rate limits and the bounded medicine set are recorded
in the [source audit](../roadmaps/clinical-medicines-source-audit.md) and returned
by `clinical_provider_contracts` and `medicines_readiness`. No source is live
until the dated live run (#2429).

## Records

`noesis-clinical-medicines-record-v1` (`src/kb/clinical_medicines.py`) extends the
clinical record model and is stored in the clinical record store:
`medicinal-product`, `marketing-authorisation` (one dated event; a status change
is a new record), `label-revision`, `label-section-change` and
`safety-communication`.

## Journey

1. Enable the feature: select `clinical-evidence` with `features: ["medicines"]`
   through the composition coordinator. With the feature off the bundle is
   unchanged.
2. Acquire through the source-pack runtime (`run_source_pack_execution`, pack
   `clinical-evidence`, the `medicines-*` sources).
3. `propose_medicine_identities`: published names are looked up in RxNav. US
   exact names are `equivalent` (accepted by rule); EU products match by active
   substance only (`narrower`) and wait for `review_medicine_identity`; unmatched
   names are reported. `revert_medicine_identity` undoes a decision.
4. `link_medicine_evidence`: trials and publications cited by identifier in the
   regulator's text, FAERS counts through accepted identity on both sides.
5. Ask: `medicine_status_as_of`, `medicine_label_as_of`,
   `compare_medicine_labels`, `medicine_safety_communications`,
   `medicine_regulatory_timeline`. A medicine with no record is reported as
   having none on record.
6. Watch: `create_medicines_monitor`, `run_medicines_monitor`,
   `poll_medicines_monitor` deliver authorisation changes, label revisions with
   section changes and communication updates once per committed watermark.

The offline acceptance test
`tests/unit/domains/test_medicines_acceptance.py` runs this journey on authored
fixtures.
