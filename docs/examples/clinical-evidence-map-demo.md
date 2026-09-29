# Clinical Evidence: cited evidence-map demo

> **Evidence: offline authored fixtures only.** Provider responses come from `tests/fixtures/clinical` (hand-written in each provider's response shape). The intervention *noetiglutide*, its sponsor, every trial number, PMID, DOI, label and product number are **fictional placeholders**. Nothing here describes a real trial or medicine. Live checks are recorded separately in `docs/development/clinical-evidence/`; the latest one was blocked by the build environment's network policy, so no source is live-verified.
>
> **This evidence map describes trial registrations, posted results, regulatory records and publications, and explains the strength of the evidence base from recorded methodology fields only. It is not medical advice, makes no dosing or treatment recommendation and draws no clinical conclusion. Unknown fields stay unknown; consult the primary sources and qualified professionals.**

Regenerate with `python scripts/clinical_demo.py --output docs/examples/clinical-evidence-map-demo.md`.

## Question

- Population: adults with type 2 diabetes
- Condition: type 2 diabetes (expanded to MeSH D003924)
- Intervention: noetiglutide (no MeSH mapping; matched as registered)
- Comparator: placebo; outcome of interest: HbA1c

Term expansion, explained:

- `D003924`: equivalent -> D003924 via crosswalk crosswalk:clinical-terms-mesh.98569e7e9080@1.0.0:58efe46bf04df35a (confidence 1.0)
- `D003922`: incompatible mapping term:type-2-diabetes -> D003922 blocks expansion
- `term:t2dm`: registry term maps equivalent to D003924 via crosswalk crosswalk:clinical-terms-mesh.98569e7e9080@1.0.0:58efe46bf04df35a (curation, reviewed by fixture-reviewer)

## Trials

| Registry | Identifier | Status | Revisions | Phase | Allocation / masking | Planned / actual N | Results | Publications | Retracted | Outcome-switching findings |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ctgov | [NCT09000001](https://clinicaltrials.gov/study/NCT09000001) | completed | 5 | phase-3 | randomized / quadruple | 600 / 612 | posted | 1 | False | primary-retimed, publication-retimed |
| ctgov | [NCT09000002](https://clinicaltrials.gov/study/NCT09000002) | terminated | 3 | phase-2 | randomized / none | 120 / 64 | not-posted | 2 | True | none recorded |
| ctgov | [NCT09000003](https://clinicaltrials.gov/study/NCT09000003) | recruiting | 1 | phase-3 | randomized / double | 900 / unknown | not-posted | 1 | False | none recorded |
| ctis | [2023-509001-12-00](https://euclinicaltrials.eu/search-for-clinical-trials/?lang=en&EUCT=2023-509001-12-00) | ongoing | 1 | phase-3 | randomized / double | 880 / unknown | not-posted | 1 | False | none recorded |
| euctr | [2015-900001-10](https://www.clinicaltrialsregister.eu/ctr-search/search?query=2015-900001-10) | completed | 1 | phase-3 | randomized / double | 600 / unknown | available-not-acquired | 1 | False | publication-retimed |

Registered twice, never merged (cross-registry links with their identifier evidence):

- NCT09000001 <-> euctr 2015-900001-10: disagreements on design.masking, primary_outcomes
- NCT09000003 <-> ctis 2023-509001-12-00: disagreements on status, design.enrollment_planned, primary_outcomes
- 2023-509001-12-00 <-> ctgov NCT09000003: disagreements on status, design.enrollment_planned, primary_outcomes
- 2015-900001-10 <-> ctgov NCT09000001: disagreements on design.masking, primary_outcomes

## Outcome-switching findings (findings, not verdicts)

- NCT09000001 `primary-retimed` Change From Baseline in HbA1c: registry:registry-history:1 (Baseline, week 26) -> registry:registry-history:2 (Baseline, week 52)
- NCT09000001 `publication-retimed` Change From Baseline in HbA1c: registry:registry-history:3 (Baseline, week 52) -> publication paper:261db12345a243303fd43563: "The primary outcome was change in HbA1c from baseline to week 26."
- 2015-900001-10 `publication-retimed` Change in HbA1c from baseline: registry:unversioned (Week 52) -> publication paper:261db12345a243303fd43563: "The primary outcome was change in HbA1c from baseline to week 26."

## Regulatory records (providers' own disclaimers kept)

- EMA authorisation-status: EMA: Noetiglu (fixture) (version 4) [https://www.ema.europa.eu/en/medicines/human/EPAR/noetiglu-fixture]
- FDA approval: FDA application NDA299001 (version 2025-04-15) [https://api.fda.gov/drug/drugsfda.json?search=application_number:NDA299001]
  - openFDA disclaimer: "Do not rely on openFDA to make decisions regarding medical care. While we make every effort to ensure that data is accurate, you should assume all results are unvalidated. We may limit or otherwise restrict your access to the API in line with our Terms of Service."
- FDA label-revision: Label: NOETIGLU (FIXTURE) (version 7) [https://api.fda.gov/drug/label.json?search=set_id:f1x7u2e0-0000-4000-8000-000000000001]
  - openFDA disclaimer: "Do not rely on openFDA to make decisions regarding medical care. While we make every effort to ensure that data is accurate, you should assume all results are unvalidated. We may limit or otherwise restrict your access to the API in line with our Terms of Service."
  - Not retained: dosage_and_administration (outside the non-advice boundary)
- FDA adverse-event-summary: FAERS report counts: noetiglutide (version 2026-09-19) [https://api.fda.gov/drug/event.json?search=patient.drug.openfda.generic_name%3A%22noetiglutide%22&count=patient.reaction.reactionmeddrapt.exact]
  - FAERS report counts (reports mentioning the term, of 1210 reports): NAUSEA 412, VOMITING 198, DIARRHOEA 187. Number of adverse-event reports in FAERS that mention the term for the queried product. Reporting counts only: not incidence, not a rate, and not evidence that the product caused the event.
  - openFDA disclaimer: "Do not rely on openFDA to make decisions regarding medical care. While we make every effort to ensure that data is accurate, you should assume all results are unvalidated. We may limit or otherwise restrict your access to the API in line with our Terms of Service."

## Registered reviews

- CRD42099000001: Noetiglutide for type 2 diabetes: a systematic review of randomised trials (fixture) (Ongoing; a registered review protocol, not review findings; supplied as a user export)

## Strength of the evidence base (rule-based, traceable)

- Summary rule **S2**: exactly one D1 trial with posted results (R1) whose linked publications are known not to be retracted.
- Counts: {'trials': 5, 'randomised': 5, 'results_posted': 1, 'results_not_posted': 3, 'with_publications': 5, 'retracted': 1, 'retraction_status_unknown': 0, 'with_outcome_switching_findings': 2}.
- Score: none computed. GRADE asserted: False (inputs missing: risk of bias, inconsistency, indirectness, imprecision, publication bias).
- Per-trial design rules:
  - NCT09000001: D1 randomised-blinded-controlled (inputs {'allocation': 'randomized', 'masking': 'quadruple', 'comparator_recorded': True, 'arm_types': ['EXPERIMENTAL', 'PLACEBO_COMPARATOR']}); results rule R1 (posted)
  - NCT09000002: D2 randomised-open-or-single-blind (inputs {'allocation': 'randomized', 'masking': 'none', 'comparator_recorded': False, 'arm_types': ['EXPERIMENTAL']}); results rule R3 (not-posted)
  - NCT09000003: D1 randomised-blinded-controlled (inputs {'allocation': 'randomized', 'masking': 'double', 'comparator_recorded': True, 'arm_types': ['ACTIVE_COMPARATOR', 'EXPERIMENTAL']}); results rule R3 (not-posted)
  - 2023-509001-12-00: D1 randomised-blinded-controlled (inputs {'allocation': 'randomized', 'masking': 'double', 'comparator_recorded': True, 'arm_types': []}); results rule R3 (not-posted)
  - 2015-900001-10: D1 randomised-blinded-controlled (inputs {'allocation': 'randomized', 'masking': 'double', 'comparator_recorded': True, 'arm_types': []}); results rule R2 (available-not-acquired)

## Unknowns and coverage gaps

- Unknown fields across trials: design.enrollment_actual, design.enrollment_planned, methodology.preregistration, methodology.sample_size_actual, registration.prospective
- Unregistered publication: A randomised trial of noetiglutide in adolescents with type 2 diabetes (PMID 99000003): reported as a trial but declares no registry identifier
- Unmapped question term 'noetiglutide': no crosswalk mapping to MeSH; matched through registry-native labels only

## Evidence bundle

- `sha256:ff4f109bf862a736cfc169367de16b5aa3517d1273950e5edf6a69a8d763e54e` (19 objects, completeness partial: 2 declared omissions).
- Evidence kind: offline only = True; live providers: none.
