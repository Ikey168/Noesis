# Clinical Evidence pack scope

Status: implemented offline, 2026-09-27 (see `docs/subsystems/clinical-evidence.md`). No source is live-verified yet. It gives no medical advice, dosing or treatment recommendation.

## Delivery state (audited 2026-09-27)

- Delivered in code: H01–H14 below. Modules are `src/kb/clinical_*.py`, `src/ingestion/clinical_providers.py` and `src/ingestion/connectors/paper/trial_registry.py`, with extensions to the Science owners `src/kb/methodology_provenance.py`, `src/kb/systematic_reviews.py` and `src/kb/ontology.py`. Tests are in `tests/unit/clinical/`, `tests/unit/composition/test_clinical_composition.py` and `tests/unit/domains/test_clinical_evidence_acceptance.py`.
- Composition: composed. `packs/clinical-evidence/manifest.json` binds `clinical.core` plus `science.literature`, `science.methodology`, `science.systematic-reviews`, `science.paper-families`, `platform.source-runtime` and `platform.subscriptions`. `set_clinical_bundle_enabled` is a coordinator selection change.
- Sources: the `clinical-evidence` source pack (`config/source_packs/clinical-evidence.json`) runs ClinicalTrials.gov, CTIS, EU CTR, openFDA and EMA as native connectors. PROSPERO, WHO ICTRP and Cochrane are recorded as not implemented, each with a reason.
- Live evidence: the bounded live check of 2026-09-27 was blocked by the build environment's egress proxy for every host (`source_unavailable`). `LIVE_VERIFICATION` stays `unverified-live`. H14's live acceptance row stays open until a run from a permitted network succeeds.

Tracking: [#1847](https://github.com/Ikey168/Noesis/issues/1847).

## Outcome

Given a clinical question (intervention, condition, population), assemble registered trials with their version history, posted results, regulatory records and the publications and preprints linked to them. Explain evidence strength from methodology records, separately from any conclusion.

## Implementation issues

- [x] [#1863](https://github.com/Ikey168/Noesis/issues/1863): H01 audit clinical source contracts and select bounded provider coverage.
- [x] [#1864](https://github.com/Ikey168/Noesis/issues/1864): H02 define registered-trial, arm, outcome, result, regulatory-record and registry-link records.
- [x] [#1865](https://github.com/Ikey168/Noesis/issues/1865): H03 acquire ClinicalTrials.gov studies with version history and results sections (live state: unverified).
- [x] [#1866](https://github.com/Ikey168/Noesis/issues/1866): H04 acquire EU CTIS and EU CTR trial records and link them across registries (live state: unverified).
- [x] [#1867](https://github.com/Ikey168/Noesis/issues/1867): H05 acquire openFDA drug labels, approvals and adverse-event summaries with disclaimers preserved (live state: unverified).
- [x] [#1868](https://github.com/Ikey168/Noesis/issues/1868): H06 acquire EMA medicine records and PROSPERO systematic-review registrations. PROSPERO is user-supplied exports only (not implemented for automated acquisition); EMA live state is unverified.
- [x] [#1869](https://github.com/Ikey168/Noesis/issues/1869): H07 link trials to publications and preprints through the Science providers.
- [x] [#1870](https://github.com/Ikey168/Noesis/issues/1870): H08 extend methodology provenance with trial-design fields and outcome-switching detection.
- [x] [#1871](https://github.com/Ikey168/Noesis/issues/1871): H09 explain strength of evidence from methodology records, separate from claim conclusions.
- [x] [#1872](https://github.com/Ikey168/Noesis/issues/1872): H10 align conditions and interventions to MeSH through ontology crosswalks.
- [x] [#1873](https://github.com/Ikey168/Noesis/issues/1873): H11 monitor trial status, results posting, label changes and retractions.
- [x] [#1874](https://github.com/Ikey168/Noesis/issues/1874): H12 compose the Clinical Evidence bundle over the Science providers.
- [x] [#1875](https://github.com/Ikey168/Noesis/issues/1875): H13 add offline question-to-evidence-map acceptance coverage.
- [ ] [#1876](https://github.com/Ikey168/Noesis/issues/1876): H14 validate live clinical coverage and publish a cited evidence-map demo. The live-check script, the recorded blocked run and the offline demo are delivered. Live validation is not: every provider host was refused by the build network.

Each issue contains its acceptance criteria and prerequisite references.

## Sources

- [ClinicalTrials.gov API v2](https://clinicaltrials.gov/data-api/api): studies, results sections. Version history comes from the site's history endpoint, which is not part of the documented v2 contract.
- [EU CTIS](https://euclinicaltrials.eu/): trials, member-state decisions and result summaries, through the public portal's JSON.
- [EU Clinical Trials Register](https://www.clinicaltrialsregister.eu/): legacy EudraCT protocols per member state. Only the availability of results is recorded.
- [openFDA](https://open.fda.gov/apis/): labels, Drugs@FDA approvals and FAERS reporting counts, stored with openFDA's disclaimer.
- [EMA medicine data](https://www.ema.europa.eu/en/medicines/download-medicine-data): authorisation status and EPAR links from the published export.
- [PROSPERO](https://www.crd.york.ac.uk/prospero/): user-supplied record exports only.
- [Europe PMC](https://europepmc.org/RestfulWebService), PubMed, medRxiv and bioRxiv: the existing Science providers.

WHO ICTRP (machine access only by agreement) and Cochrane (licensed) are recorded as not implemented, each with its reason.

## Composition and reuse

The pack follows the [pack/workflow architecture](../architecture/pack-workflow-composition.md). It extends the Science owners rather than duplicating them:

- trial design and outcome switching live in methodology provenance;
- PROSPERO registrations are linked to the systematic-review store;
- publication families, retractions and preprints come from paper families and Crossref notices;
- MeSH crosswalks live in the ontology module;
- registry identifiers are read from scholarly records by the paper connectors.

It adds no scheduler, permission ledger, project store, paper store or database.

## Acceptance

- A reproducible journey takes a clinical question to a cited evidence map. The map shows:
  - trials with their status and versions;
  - results;
  - regulatory records;
  - linked publications;
  - methodology summaries;
  - an explained strength-of-evidence view with unknowns visible.

  This is met offline (`tests/unit/domains/test_clinical_evidence_acceptance.py`, `docs/examples/clinical-evidence-map-demo.md`).
- Offline and bounded live evidence are reported separately. The live report is `docs/development/clinical-evidence/live-check-2026-09-27.json`, which records a blocked run.
