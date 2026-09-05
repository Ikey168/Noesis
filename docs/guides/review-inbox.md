# Shared evidence review inbox

`create_review_inbox_task` references a current entity decision, translation, quality assessment, or extracted-object revision. It does not duplicate the evidence into another claim store. Each task declares its domain, optional project, impact, uncertainty, rationale, exact source document revisions, and any known related-publication groups. Registration checks the actual target revision and source availability. Source documents require explicit document-read access; target-domain and namespace access are checked throughout.

`list_review_inbox_tasks` supports domain/project filters and explains its priority: twice declared impact, plus declared uncertainty and recency. A per-source cap applies before the bounded candidate scan so one prolific publisher cannot fill the queue. The scoring inputs are declared priorities, not measured error-reduction predictions.

A coordinator uses `assign_review_inbox_task` to assign two to ten different reviewers. The coordinator cannot also be one of those reviewers. `submit_review_inbox_annotation` checks the target fingerprint and records an immutable label, rationale, declared human/machine origin, and self-reported effort. Reviewers cannot see peer labels before resolution; the coordinator can inspect disagreements. Underlying changes reject stale submissions. Refresh by creating a task against the new target revision.

After all reviews finish, `resolve_review_inbox_task` routes consensus, or an explicit adjudicated label for disagreement, through existing `EntityHistoryStore.decide`, `CrossLanguageStore.review_translation`, or `QualityStore.override`. Domain-review scope remains required. The domain decision and queue receipt commit together; failed routing leaves the task ready for retry. Extraction labels are annotations for explicit dataset releases and never directly replace mined claims. The coordinator records adjudication separately from reviewers, and conflicting original votes remain visible.

Supported labels:

| Target | Label |
|---|---|
| entity | `{"decision":"match"}`, `non-match`, or `uncertain` |
| translation | `{"decision":"accepted"}`, `rejected`, or `disputed` |
| extraction | `{"decision":"correct"}`, `incorrect`, or `uncertain` |
| quality | `{"dimension":"coverage","value":0.5}` with an existing quality dimension |

`build_review_annotation_dataset` creates a draft from explicit task IDs. Unresolved, adjudicated disagreements, machine annotations, and targets changed after review stay outside eligible consensus rows. Their provenance and reasons remain in the excluded section. Eligibility requires multiple separately assigned principals declaring human origin; this declaration does not independently certify who performed the annotation.

Before assigning train/validation/test splits, connected components group document revisions, shared content, shared entity subjects, and declared related publications. Previously released groups retain their split. A newly discovered relationship bridging older train/test groups fails explicitly. Unknown relationships cannot be inferred perfectly; curators must declare related publications they know about.

`release_review_annotation_dataset` is a separate explicit action that rechecks the draft and publishes immutable split guards. Only released datasets can be exported. No tool starts retraining or deploys a model. Exports recheck current task and source access. `evaluate_review_annotation_predictions` compares supplied before/after predictions on every held-out test row and reports paired errors and self-reported reviewer effort. It discloses that the predictions and annotator identities are not independently audited.

Fourteen store/public-MCP/catalog tests cover priority diversity, independent reviews, existing domain routing, rollback, stale revisions, access revocation, grouping, prior split reuse, excluded labels, release gating, and paired evaluation mechanics. A real human reviewer-effort/error-reduction study remains pending EX-05; test annotations are synthetic behavior fixtures.
