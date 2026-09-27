# Clinical Evidence pack: question to cited evidence map

This guide walks one clinical question through the Clinical Evidence pack and
states what the pack will and will not say. Architecture and module ownership
are in [`docs/subsystems/clinical-evidence.md`](../subsystems/clinical-evidence.md).

## Non-advice boundary

The pack describes trial registrations, posted results, regulatory records and
publications, and explains the strength of the evidence base from recorded
methodology fields. It is **not medical advice**, makes **no dosing or treatment
recommendation** and draws no clinical conclusion. Specifically:

- no score is computed; summary categories are documented rules that list the
  record fields they used;
- no GRADE (or other) grade is asserted when its inputs are missing, and the view
  lists the missing inputs;
- adverse-event figures are FAERS **reporting counts**, never incidence, rates
  or causation, and openFDA's own disclaimer is shown with every openFDA record;
- label dosing sections are not retained;
- registration, results posting and publication are distinct; a paper never
  stands in for posted results;
- the same trial in two registries is linked with its identifier evidence and
  the disagreements are listed, never merged;
- unknown fields stay unknown.

## The journey

1. **Enable** the bundle through the coordinator (operator):
   `set_clinical_bundle_enabled(namespace, true)` returns the activation receipt.
   Science and shared providers are unaffected either way.
2. **Acquire** through the shared runtime: accept each source's license, then
   `run_source_pack_execution` for pack `clinical-evidence` (operation
   `records`). Sources are pinned selections (NCT, EU CT and EudraCT numbers,
   openFDA products, EMA product numbers) or one bounded CT.gov search; budgets,
   cursors and receipts are the runtime's. Set `NOESIS_OPENFDA_API_KEY` for a
   higher openFDA limit; it is never stored. Each CT.gov registry version becomes
   a trial revision with its date; results, member-state decisions, labels,
   approvals, FAERS counts and EMA authorisation status are separate records.
3. **Import PROSPERO** registrations you hold as record exports
   (`import_prospero_registration`, optionally naming your systematic-review
   protocol). PROSPERO is not acquired automatically.
4. **Link publications** harvested by the Science providers
   (`link_clinical_publications`): registry-declared references, PubMed/Europe PMC
   secondary-source identifiers and paper families (preprint, version of record,
   Crossref retractions). Text mentions become candidates for
   `review_clinical_publication_link`. `clinical_coverage_gaps` lists unlinked
   trials and trial reports with no registry identifier.
5. **Record trial design** (`record_clinical_trial_design`) and read
   **outcome switching** (`clinical_outcome_switching`): each finding cites both
   registry versions (or the registry and the publication passage).
6. **Align terms** (`align_clinical_terms`) to publish MeSH and the namespace's
   registry-native terms with their crosswalk; `expand_clinical_question` shows
   each expansion step, blocked incompatible mappings and unmapped terms.
7. **Build the map** (`build_clinical_evidence_map` with a question and request
   key), read the strength view (`clinical_strength_view`) and export it
   (`export_clinical_evidence_bundle`).
8. **Monitor** (`create_clinical_monitor`, `run_clinical_monitor` at committed
   watermarks, `poll_clinical_monitor`). Changes make the map stale until it is
   rebuilt; refreshes run through the source pack's schedule.

A reproducible offline run of the whole journey is
`python scripts/clinical_demo.py --output docs/examples/clinical-evidence-map-demo.md`.

## Per-provider live state

| Provider | Implemented | Live state (2026-09-27) |
| --- | --- | --- |
| ClinicalTrials.gov | yes | unverified live: bounded check blocked by the build network (`source_unavailable`, proxy CONNECT 403) |
| EU CTIS | yes | unverified live: blocked (`source_unavailable`) |
| EU CTR | yes (results availability only) | unverified live: blocked (`source_unavailable`) |
| openFDA | yes | unverified live: blocked (`source_unavailable`) |
| EMA medicines | yes | unverified live: blocked (`source_unavailable`) |
| PROSPERO | no (user-supplied exports) | not implemented: no supported machine access identified |
| WHO ICTRP | no | not implemented: machine access by agreement only |
| Cochrane | no | not implemented: licensed |

`clinical_provider_contracts` and `clinical_bundle_status` return the same
state at run time (`fixture-only` for data acquired from fixtures).

## Offline and live evidence, reported separately

- **Offline**: `tests/unit/clinical/*` and
  `tests/unit/domains/test_clinical_evidence_acceptance.py` run the real
  runtime, adapters and stores over authored fixtures (`tests/fixtures/clinical`,
  a fictional intervention). Receipts say `execution: injected`. The demo document
  is offline evidence.
- **Live**: `scripts/clinical_live_check.py` writes
  `docs/development/clinical-evidence/live-check-<date>.json`. The 2026-09-27 run
  recorded every source as `source_unavailable` because the environment's egress
  proxy refused all provider hosts. No live coverage is claimed; rerun from a
  network that can reach the hosts listed in the report.
