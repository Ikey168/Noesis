# Technology: AI models and datasets

The Technology bundle's `technology.ai-models` provider (track
[#2742](https://github.com/Ikey168/Noesis/issues/2742), subdomain
`ai-models-datasets`) takes a model or dataset to cited registry records
across three sources, with revisions and declared licences as each source
stated them. It is **covered offline only**: every source is
`unverified-live`, and no live run has happened (AI13).

Source audit: [ai-models-evidence/source-audit.md](../development/ai-models-evidence/source-audit.md)
(AI01). Machine-readable copy: `src/ingestion/ai_models_sources.py`
(`PROVIDER_CONTRACTS`, `BOUNDED_COVERAGE`, `CAPS`, `MINIMISATION`,
`EXCLUSIONS`, `LIVE_VERIFICATION`).

## Enabling it

Three optional features of the existing `technology` bundle, all default
off and independent: `ai-models-hub`, `ai-models-openml` and
`ai-models-epoch`. Each binds `technology.ai-models`,
`platform.entity-identity`, `platform.subscriptions` and
`platform.source-runtime`. OSS ecosystems and Literature are not required;
links to them degrade to `provider_absent` when those providers are not
composed. The sources ship in the separate `technology-ai-models` 1.0.0
source pack (`config/source_packs/technology-ai-models.json`).

## Sources and bounds

| Source | Reads | Caps |
| --- | --- | --- |
| Hugging Face Hub (`huggingface-hub`) | organisation check, repository metadata, refs, each declared revision `sha` and its card file | 5 repositories, 10 declared revisions each, cards up to 1 MB |
| OpenML (`openml`) | dataset description and qualities, one task, its run evaluations per measure | 2 datasets, 1 task, 100 evaluation rows |
| Epoch AI (`epoch-ai`) | one notable-models CSV and the data page's last-updated stamp | 1 file (20 MB), 20 rows stored |

Requests are paced at one per second; HTTP 429 gets one retry. The optional
`NOESIS_HF_TOKEN` only raises limits, is sent as a header and is never
recorded; it never unlocks gated content. OpenML reads use no key and the
upload key is never configured. That OpenML API v1 is still served after
its server migration is _verify_ until AI13.

## The journey

1. **Acquire.** The source-pack runtime runs the `ai-models` adapter; the
   projector appends `noesis-ai-model-record-v2` revisions
   (`src/kb/ai_models_store.py`).
2. **Revisions.** A Hub repository is keyed by repository id and `sha`
   with `lastModified`; a pinned `sha` is immutable and other content under
   it is refused. A repository that turns gated or disabled gets a
   `withdrawn` revision, a 404 a `removed_by_source` revision, a same-host
   redirect a `renamed` revision; history is kept. OpenML dataset versions
   are their own ids; status, description or checksum changes are new
   revisions. Epoch rows are vintaged by the file's content digest, dated by
   the page's stamp or the retrieval time (`retrieval_time`); a revised
   estimate is a new revision and a declared row missing from a complete
   later file is `removed_by_source`.
3. **Identity.** `propose_ai_model_identity_matches` offers Epoch-to-Hub
   model and Hub-dataset-to-OpenML matches on stated identifiers only
   (repository id, OpenML id, then shared arXiv id or DOI). A reviewer other
   than the proposer accepts or rejects; a match can be reverted. Decisions
   are entity identity decisions; nothing is merged. A shared name is never
   a match, and unmatched records stay visible.
4. **Links.** `link_ai_model_records` resolves arXiv ids and DOIs through
   the paper connector and scholarly document ids, dataset DOIs through the
   research-entities DataCite path, and stated package URLs through the OSS
   ecosystems records. `library_name` stays stated text. Missing providers
   and targets are reported.
5. **Answers.** `ai_model_records_as_of` returns each source's revision
   current at a date side by side, each citing the Hub `sha`, the OpenML id
   and version, or the Epoch vintage with its as-of time. Self-reported
   card results, OpenML run evaluations and Epoch estimates stay with who
   reported them and are never merged. `ai_model_revision_history` lists
   revisions and declared-licence changes, quoted as declared with an SPDX
   id only on an exact licence-id match. `export_ai_model_evidence` returns
   a `noesis-evidence-bundle-v1`.
6. **Monitoring.** `create_ai_models_monitor` / `run_ai_models_monitor` /
   `poll_ai_models_monitor` are knowledge subscriptions: notices of new
   revisions, licence changes, gating, removals and renames, each citing the
   revision. A failed refresh never produces a removal notice.

## Tools

Reads (`knowledge:technical:ai-models:read`): `ai_models_source_contracts`
(no scope), `ai_models_readiness`, `list_ai_model_records`,
`ai_model_records_as_of`, `ai_model_revision_history`,
`list_ai_model_identity_matches`, `list_ai_model_links`,
`export_ai_model_evidence`, `poll_ai_models_monitor` (plus
`knowledge:subscriptions:read`). Writes: `propose_ai_model_identity_matches`
and `link_ai_model_records` (`...:write`; reading a held Literature,
research-entities or OSS store also needs its read scope),
`review_ai_model_identity_match` and `revert_ai_model_identity_match`
(`...:review`), `create_ai_models_monitor` and `run_ai_models_monitor`
(plus `knowledge:subscriptions:write`). Every answer is checked against the
minimisation decision before it leaves.

## Minimisation and exclusions

Only organisation-level registry metadata is stored. A user namespace is
refused before anything of the repository is read. Person fields (Hub user
and commit authors, OpenML creators, contributors and uploaders, Epoch
Authors) are refused at parse time; card bodies, commit history,
discussions and download, like and trending counts are never stored.

Not done: downloading model weights or dataset files; capability, safety,
quality, risk or openness verdicts; leaderboards or rankings; licence
compliance interpretation; merging self-reported results with OpenML
evaluations or Epoch estimates; inferring training data, compute or
parameters a source does not state.

## What is not live

Endpoints, field names, the organisation and refs endpoints, the rename
redirect, the OpenML evaluation path and its 412 no-results answer, the
Epoch file URL and columns, and every licence term marked _verify_ in the
audit come from documentation, not a live response. The fixtures are
authored (fictional organisations such as `example-org`, invented shas and
ids, dates 2094-2099). Offline acceptance:
`tests/unit/domains/test_ai_models_acceptance.py`.
