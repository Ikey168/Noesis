# New workflow features

Reviewed 2026-09-05. Companion to the [29 workflow improvement candidates](workflow-improvement-review.md) and [source collection backlog](source-collection-backlog.md). This adds **24 granular feature tasks across eight product capabilities**. All eight capabilities are published as tracking issues with 24 granular sub-issues. Persistent research projects remains under [#1393](https://github.com/Ikey168/Noesis/issues/1393); see the [complete issue index](workflow-issue-backlog.md) for every feature task and its prerequisites. No feature implementation was performed in this review.

The scope assessment used `src/kb`, related services/MCP tools, and the guides for research gaps, acquisition planning, recipes, hypotheses, change briefs, quality, anomalies, snapshots, entity history, multilingual evidence, datasets, quantitative knowledge, citation preservation, and multimodal evidence. Some capabilities have substantial foundations already; the proposed increment is identified below. Absence from these searches is not proof that no earlier design discussion exists.

## Existing capabilities to build on

Do not open generic “add hypotheses,” “add research-gap discovery,” “add change alerts,” “add multilingual search,” or “add research snapshots” issues. These already have documented stores and MCP surfaces. Source acquisition already has objectives, budgets, fallbacks, and execution receipts. Recipes already model resumable tool DAGs. Entity history already supports reviewed merge/split decisions. Quality and anomaly assessments already expose several useful evidence dimensions.

The product opportunity is to connect these foundations into persistent activities that a researcher can start, revisit, review, and share. The implementation gaps in the companion review still apply: a product-level workflow must exercise actual extraction/retrieval, not only accept prepared fixture outputs.

## 1. Persistent research projects

Published GitHub tasks under [#1393](https://github.com/Ikey168/Noesis/issues/1393):

| Task | Implementation issue |
|---|---|
| NF-01 | [#1394 — Persist project questions, scope, and linked investigation state](https://github.com/Ikey168/Noesis/issues/1394) |
| NF-02 | [#1395 — Execute bounded research cycles and reassess evidence gaps](https://github.com/Ikey168/Noesis/issues/1395) |
| NF-03 | [#1396 — Branch projects and compare investigations against a pinned baseline](https://github.com/Ikey168/Noesis/issues/1396) |

All three are native GitHub sub-issues. NF-02 and NF-03 depend on NF-01; the issue bodies include production-readiness requirements, code references, and detailed acceptance criteria.

**User outcome:** “Investigate why these studies disagree, using my scientific and economic knowledge bases, and keep the investigation current.” A project retains the question, scope, evidence, open work, and expenditure across sessions.

**Existing foundation:** `src/kb/source_planner.py`, `research_gaps.py`, `hypotheses.py`, `research_recipes.py`, and `research_snapshots.py`. These have individual objectives/workspaces/plans. The proposed addition is one project lifecycle linking them and reevaluating the next research action from committed results.

- **NF-01 — Persist project questions and linked research state.** Create a project with domains/namespaces, question revisions, explicit success criteria, cumulative budget, owner, and references to existing plans/hypotheses/snapshots. Acceptance: reopening a project reconstructs its completed work and unresolved questions; changing its question preserves the earlier investigation and its citations.
- **NF-02 — Execute a bounded research-and-reassessment loop.** Convert selected existing gap tasks into acquisition/extraction/query recipe steps, then recompute coverage after committed results. Acceptance: stop on the configured iteration/time/cost limit, adequate coverage, or no new independent evidence; preserve reasons, receipts, and cumulative spending across resume. No separate planner or unbounded agent loop is required.
- **NF-03 — Branch and compare project investigations.** Start an alternative investigation from a pinned project state. Acceptance: compare evidence, assumptions, conclusions, coverage, and costs against the common baseline; distinguish new evidence from a changed method, and preserve namespace access constraints.

**Dependencies:** WF-01/02/03, IX-01, and QA-01/03 from the improvement review. NF-01 can start before the production loop is connected.

## 2. Living research reports

<!-- feature-publication:2 -->
Tracking: [#1405](https://github.com/Ikey168/Noesis/issues/1405). Tasks: NF-04 [#1447](https://github.com/Ikey168/Noesis/issues/1447), NF-05 [#1457](https://github.com/Ikey168/Noesis/issues/1457), NF-06 [#1463](https://github.com/Ikey168/Noesis/issues/1463).

**User outcome:** “Show which conclusions in last month's report need revisiting after this correction.” The user gets a focused proposed revision and can inspect exactly why it changed.

**Existing foundation:** `src/kb/artifacts.py`, `claim_timelines.py`, `change_briefs.py`, `citation_preservation.py`, and `research_packages.py`. Change briefs already summarize supplied before/after states. The proposed addition is an authored report whose sections and assertions have durable dependency links and a revision workflow.

- **NF-04 — Store report sections with exact evidence dependencies.** Give sections/assertions stable identities and link them to claims, calculation receipts, source revisions, and a research snapshot. Acceptance: export and reopen a report with the same section order, authored wording, citations, and known limitations; unsupported author commentary remains explicitly distinguishable from sourced statements.
- **NF-05 — Detect which report conclusions are affected by new evidence.** Subscribe report dependencies to committed corrections, retractions, entity decisions, and calculation revisions. Acceptance: identify affected sections with before/after evidence and a reason; unrelated sections remain current, and incomplete coverage yields an explicit uncertain assessment.
- **NF-06 — Draft and review report revisions.** Generate proposed edits only for affected sections, with evidence-backed explanations and conflict handling for concurrent author edits. Acceptance: users accept/reject individual proposals; accepted changes create a versioned report, while earlier reports remain reproducible. Export the updated report and bibliography without automatically publishing it externally.

**Dependencies:** DP-02, EX-01/04, IX-02, QA-03, PK-01; NF-05 follows NF-04 and NF-06 follows NF-05.

## 3. Unified evidence review inbox

<!-- feature-publication:3 -->
Tracking: [#1406](https://github.com/Ikey168/Noesis/issues/1406). Tasks: NF-07 [#1435](https://github.com/Ikey168/Noesis/issues/1435), NF-08 [#1448](https://github.com/Ikey168/Noesis/issues/1448), NF-09 [#1458](https://github.com/Ikey168/Noesis/issues/1458).

**User outcome:** “Give me the ten reviews most likely to improve this investigation.” Resolve ambiguous entities, contested translations, questionable evidence, and extraction errors in one queue.

**Existing foundation:** `src/kb/entity_history.py`, `cross_language.py`, `knowledge_quality.py`, and `research_gaps.py` have scoped review/decision operations. `scripts/human_annotation.py` provides an annotation workflow. Active-learning suggestions already appear in `docs/architecture/mcp-rearchitecture.md`; the increment here is a shared operational queue and feedback routing, not a claim that active learning is an entirely new idea.

- **NF-07 — Aggregate review candidates with an explainable priority.** Reference underlying objects/revisions rather than copying them into a new truth store. Acceptance: prioritize by decision impact, uncertainty, freshness, and source diversity; show priority reasons, support project/domain filters, and avoid one prolific source occupying the whole queue.
- **NF-08 — Route review decisions back to authoritative ledgers.** Add assignment, revision-checked submission, disagreement, and adjudication to inbox tasks. Acceptance: entity and translation decisions call their existing review APIs; stale proposals are rejected or refreshed; conflicting reviewers and unresolved disagreements remain visible.
- **NF-09 — Export reviewed corrections as evaluation/training candidates.** Build versioned datasets from eligible reviewed outcomes. Acceptance: retain source/annotator provenance and agreement state, separate disputed labels, group related documents to prevent split leakage, and require an explicit dataset release before any retraining. Measure reviewer effort and validated error reduction.

**Dependencies:** ER-01 and EX-05. Begin with two review types to prove the adapter contract before adding every subsystem.

## 4. Systematic literature review workflow

<!-- feature-publication:4 -->
Tracking: [#1407](https://github.com/Ikey168/Noesis/issues/1407). Tasks: NF-10 [#1449](https://github.com/Ikey168/Noesis/issues/1449), NF-11 [#1459](https://github.com/Ikey168/Noesis/issues/1459), NF-12 [#1464](https://github.com/Ikey168/Noesis/issues/1464).

**User outcome:** “Screen the papers on this question against my inclusion criteria, record why each was excluded, and produce an evidence table.” This is a new research method built on the existing document and provenance layers.

**Existing foundation:** Scholarly source packs and the collection backlog supply literature; methodology provenance, datasets, and source identity supply reusable records. No dedicated screening protocol and study-selection lifecycle was located in the searched implementation surfaces.

- **NF-10 — Version review protocols and literature search runs.** Store the question, inclusion/exclusion criteria, planned databases, search expressions, dates, and amendments. Acceptance: every candidate traces to a search run and protocol revision; duplicate publications can be associated with the same study without erasing their publication identities.
- **NF-11 — Support staged screening and adjudication.** Track title/abstract and full-text decisions, exclusion reasons, independent reviewers, and conflicts. Acceptance: missing full text remains pending/unavailable rather than automatically excluded; conflicting decisions require resolution. Evaluate ASReview as an optional ordering aid, with human screening decisions retained separately.
- **NF-12 — Export evidence tables and selection-flow counts.** Extract protocol-defined study fields with page/span locators and review state. Acceptance: counts reconcile with the actual screening ledger; exports include unresolved cases, exclusion reasons, protocol amendments, and source versions. Map recorded fields to the applicable PRISMA reporting items without asserting that a generated diagram alone establishes methodological compliance.

**Dependencies:** DP-01/02, EX-05, NF-10 before NF-11/12, plus the scholarly full-text/parser tasks in the source backlog. ASReview is optional and must be benchmarked against the same screening collection.

## 5. Zotero and bibliography interoperability

<!-- feature-publication:5 -->
Tracking: [#1408](https://github.com/Ikey168/Noesis/issues/1408). Tasks: NF-13 [#1450](https://github.com/Ikey168/Noesis/issues/1450), NF-14 [#1460](https://github.com/Ikey168/Noesis/issues/1460), NF-15 [#1451](https://github.com/Ikey168/Noesis/issues/1451).

**User outcome:** “Use my existing Zotero collection as a Noesis research project, and take the resulting citations back into my writing tools.” This supports the whole research lifecycle rather than just adding another scraper.

**Existing foundation:** `src/ingestion/document_store.py`, domain membership, citation preservation, and research-package export. No Zotero-specific connector or bibliography round-trip was located in the searched implementation surfaces.

- **NF-13 — Import Zotero collections with stable identity.** Implement a read adapter for the documented Zotero API, with local-desktop and Web API modes negotiated separately. Acceptance: preserve library/item keys, item versions, collection membership, tags, bibliographic fields, and authorized attachment references; repeated import is idempotent, and missing attachments are explicit.
- **NF-14 — Track incremental library changes and annotation provenance.** Add update/deletion reconciliation and supported note/annotation mapping to Noesis revisions and locators. Acceptance: preserve local review history when a Zotero item changes; external deletion and local retention are distinct states; unsupported annotation anchors are reported. Define read synchronization first and specify any future write-back separately.
- **NF-15 — Export bibliographies alongside evidence-rich reports.** Add structured bibliography export with stable citation keys, initially CSL JSON and a chosen writing-tool format such as BibTeX. Acceptance: report citations resolve to exported items; Unicode names, missing DOIs, corporate authors, preprints, and multiple editions survive a round-trip without conflating bibliography entries with the cited evidence revision.

**Dependencies:** DP-01/02 and PK-01. NF-13 precedes NF-14; NF-15 can be delivered independently of live Zotero access.

## 6. Forecast registration and outcome tracking

<!-- feature-publication:6 -->
Tracking: [#1409](https://github.com/Ikey168/Noesis/issues/1409). Tasks: NF-16 [#1436](https://github.com/Ikey168/Noesis/issues/1436), NF-17 [#1452](https://github.com/Ikey168/Noesis/issues/1452), NF-18 [#1461](https://github.com/Ikey168/Noesis/issues/1461).

**User outcome:** “Record what I expect to happen, the evidence behind it, and later show how well those expectations performed.” Useful for policy, economics, technical developments, and other explicitly time-bounded questions.

**Existing foundation:** `src/kb/hypotheses.py` stores discriminating predictions and comparison scores, while events/quantitative observations provide potential resolution evidence. Its documented comparison scores are not truth probabilities. Classification calibration code in `src/argument_mining/human_eval_metrics.py` is a separate task; no dedicated forecast registration/resolution ledger was located.

- **NF-16 — Register time-bounded forecast questions.** Store outcome definitions, resolution date/rules, evidence snapshot, forecaster, and explicit probability revisions. Acceptance: forecasts can be frozen as known before a cutoff; ambiguous outcome rules must be corrected through visible revisions; hypothesis comparison scores never become forecast probabilities implicitly.
- **NF-17 — Propose and review forecast resolutions.** Match registered rules to sourced events or quantitative observations and retain a reviewable resolution record. Acceptance: unresolved, disputed, cancelled, and retrospectively corrected outcomes are represented; raw event extraction does not automatically settle an ambiguous forecast.
- **NF-18 — Report forecast calibration and performance by domain.** Start with binary forecasts and proper scoring, with reliability bins, sample counts, and baseline comparison. Acceptance: evaluate only forecasts recorded before the relevant cutoff, handle revised/cancelled outcomes consistently, and disclose selection/missing-resolution effects. No performance ranking from tiny samples without displaying their size and uncertainty.

**Dependencies:** NF-16 before NF-17/18; production event/metric provenance and snapshot correctness. This is a distinct optional product direction rather than a prerequisite for knowledge retrieval.

## 7. Reproducible analysis notebooks

<!-- feature-publication:7 -->
Tracking: [#1410](https://github.com/Ikey168/Noesis/issues/1410). Tasks: NF-19 [#1437](https://github.com/Ikey168/Noesis/issues/1437), NF-20 [#1453](https://github.com/Ikey168/Noesis/issues/1453), NF-21 [#1462](https://github.com/Ikey168/Noesis/issues/1462).

**User outcome:** “Turn these sourced tables into a reproducible analysis, with every chart and calculated finding linked to the exact inputs.”

**Existing foundation:** `src/kb/dataset_intelligence.py`, `quantitative.py`, `research_recipes.py`, `methodology_provenance.py`, and `research_packages.py`; the repository also has MLOps demo notebooks. The proposed addition is a first-class research-analysis execution contract tied to evidence and package outputs.

- **NF-19 — Define notebook analysis manifests over pinned inputs.** Reference dataset releases, bounded slices, metric definitions, code, parameters, and environment identity. Acceptance: all declared data inputs resolve to a reproducible snapshot; an unavailable input stops execution rather than substituting the latest data.
- **NF-20 — Execute configured notebooks with bounded resources.** Evaluate `nbclient` as the notebook execution engine, with execution isolation provided separately. Acceptance: cell and run deadlines, memory limits, cancellation, explicit network policy, and captured error state apply; successful outputs and receipts reference exact input/code/environment hashes. Notebook execution must not inherit unrestricted application credentials.
- **NF-21 — Attach analysis outputs to findings and research packages.** Register tables/figures and their producing cells as derived artifacts. Acceptance: a report can cite a calculation and its source inputs; package export contains sufficient permitted inputs or explicit omissions, and a replay compares declared outputs with appropriate numeric tolerances. A successful computation is not itself a verified substantive claim.

**Dependencies:** NF-19 before NF-20/21; QA-01, PK-01, and tested dataset/quantitative lineage. This is a larger feature and should follow the first project/report slice.

## 8. Evidence-linked decision records

<!-- feature-publication:8 -->
Tracking: [#1411](https://github.com/Ikey168/Noesis/issues/1411). Tasks: NF-22 [#1438](https://github.com/Ikey168/Noesis/issues/1438), NF-23 [#1454](https://github.com/Ikey168/Noesis/issues/1454), NF-24 [#1465](https://github.com/Ikey168/Noesis/issues/1465).

**User outcome:** “Record why we chose this option, which assumptions matter, and what future evidence should trigger reconsideration.”

**Existing foundation:** Hypothesis workspaces compare explanations, quality policies expose evidence dimensions, and watches/change briefs identify changes. The new object is an actual decision with alternatives, declared preferences, chosen action, and review conditions; it is not another confidence score for a claim.

- **NF-22 — Store decision alternatives and explicit criteria.** Link options, constraints, assumptions, evidence, author, and decision time to a pinned project state. Acceptance: observations, subjective preferences, and the selected action are distinguishable; revision history preserves what was known when the decision was made.
- **NF-23 — Compare options under declared assumption changes.** Support a bounded sensitivity calculation over explicitly supplied weights and quantitative inputs. Acceptance: show which declared assumptions change the ordering, preserve ties and missing data, and record formula/input provenance; no causal simulation is implied unless an explicit validated model supplies it.
- **NF-24 — Trigger decision reviews when dependencies change.** Register review conditions over assumptions, source revisions, or metric thresholds. Acceptance: material changes create a review task with exact supporting evidence; acknowledgement and a subsequent decision revision are recorded independently, with duplicate alerts suppressed. Reuse the existing watch and brief delivery machinery.

**Dependencies:** NF-22 before NF-23/24; NF-04/05 and NF-07/08 provide reusable dependency and review patterns; SU-01/02 and QA-03 underpin reliable notifications and explanations.

## Libraries and APIs to evaluate

Official sources checked 2026-09-05:

- [Zotero API v3](https://www.zotero.org/support/dev/web_api/v3/basics) exposes library items, collections, versions, and bibliography/export representations. The desktop local API has documented differences from the Web API; capability negotiation is part of NF-13, not an assumption that every endpoint is identical.
- [ASReview LAB](https://asreview.readthedocs.io/en/stable/) provides active-learning support for screening literature. Evaluate its ranking/workflow interchange against NF-11; it does not supply authoritative inclusion labels or prove that stopping screening is justified.
- [PRISMA 2020](https://www.prisma-statement.org/prisma-2020) supplies reporting guidance for systematic reviews. Use applicable items to design NF-12's exported records; this is guidance, not an execution library or automatic quality certification.
- [Jupyter nbclient](https://nbclient.readthedocs.io/en/latest/) executes notebooks programmatically. It can support NF-20, but a resource/security boundary, data pinning, and reproducibility checks remain Noesis responsibilities.

## Suggested delivery order

1. **First product slice:** NF-01 + NF-04 + NF-07. A persistent project, a sourced report, and a visible review queue give the existing APIs a coherent user journey. Add NF-05/08/06 to close the evidence-to-reviewed-report loop.
2. **Research workflow expansion:** NF-13/15 and NF-10/11/12, then incremental Zotero reconciliation. These produce tangible value for literature-heavy domains.
3. **Bounded research automation:** NF-02/03/09 after production extraction/retrieval and quality evaluation are dependable. Measure independent evidence gained per unit cost, reviewer effort, and useful report updates.
4. **Optional domain extensions:** Forecasts (NF-16–18), analysis notebooks (NF-19–21), and decision records (NF-22–24). Select according to actual user demand rather than making all three prerequisites for the core workflow.

Each task above has a separate acceptance boundary. All 24 tasks are published under their respective feature trackers, retaining dependencies on the earlier correctness work. NF-01–03 remain under #1393; the complete issue index links every task. No implementation or product account integration was performed during this feature review.
