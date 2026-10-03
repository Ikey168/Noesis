# AI models and datasets: source-contract audit and bounded coverage (AI01)

Tracking: wave 2 tracker #2736 · recorded 2026-10-03.

No per-track tracker or delivery issue exists yet. They are opened once this
audit names a surviving source, which it does (Hugging Face Hub metadata,
OpenML and the Epoch AI models dataset below), so the next step is to open
them and link them here.

This audit sets out, per source, what the Technology bundle's
`technology.ai-models` provider (subdomain `ai-models-datasets`, ADR-005) may
acquire, how, and on what terms. **It was written without network access: the
provider hosts (`huggingface.co`, `www.openml.org`, `epoch.ai`) were blocked by
this runtime's egress proxy (verified 2026-10-03), so terms, endpoints and
field names were not re-verified live.** They come from the providers'
published documentation as the author knows it. Every item marked _verify_
must be checked against the live terms page and a real response before the
first dated live run. No source is `live` until that run exists.

**There is no machine-readable copy yet.** `PROVIDER_CONTRACTS`,
`BOUNDED_COVERAGE`, `CAPS`, `MINIMISATION`, `EXCLUSIONS` and
`LIVE_VERIFICATION` will be added in `src/ingestion/ai_models_sources.py` by
the track's acquisition issues, and must match this audit.

Non-goals for every source: no download of model weights or dataset files, no
capability, safety, quality, risk or "openness" verdict, no leaderboard or
ranking, no download, like or trending counts, no licence-compliance
interpretation (a declared licence is quoted as stated, never judged), no
merging of a Hub card's self-reported evaluation with OpenML results or Epoch
estimates, and no inference of training data, compute or parameters where the
source states none.

## Access decisions

| Source | Publisher | Delivers | Access | Decision |
| --- | --- | --- | --- | --- |
| Hugging Face Hub metadata | Hugging Face | model and dataset repository metadata per revision: declared licence, pipeline tag, library, tags, base model, datasets named, languages, card front matter including self-reported `model-index` results, file list | Hub HTTP API, JSON; anonymous for public, non-gated repositories | `unverified-live` |
| OpenML | OpenML (Open Machine Learning Foundation) | dataset descriptions and versions with declared licence, computed data qualities, tasks, and run evaluations per task | REST API v1 JSON, no key for reads (_verify_ that v1 stays served after OpenML's server migration) | `unverified-live` |
| Epoch AI models dataset | Epoch AI | curated records of notable models: organisation, publication date, domain, parameters, training compute and dataset size as Epoch estimates them, with a confidence label | CSV download from the data page (_verify_ URL and file name) | `unverified-live` |

Documented, not acquired: Papers with Code (service discontinued in 2025,
_verify_), Hub leaderboards and Spaces (rankings and applications, excluded by
the non-goals), gated and private Hub repositories (need an accepted access
request per repository; out of first coverage), Kaggle datasets (account and
terms acceptance required), and the Epoch benchmarking data (a second dataset;
revisit after first coverage).

**Unavailable-access fallback.** A failed unit (HTTP error, 401/403 on a
repository that became gated, 429 after one retry, redirect to an undeclared
host, schema drift, a response over the byte budget) fails with its code and a
receipt; earlier revisions stay current and nothing is marked removed because
of a failure. Readiness reports the source as `stale`.

## Per-source contract

| Source | Endpoints | Authentication | Licence and redistribution | Rate limits | Revision model: updates, corrections, removals |
| --- | --- | --- | --- | --- | --- |
| Hugging Face Hub | `https://huggingface.co/api/models/{repo_id}`, `/api/models/{repo_id}/revision/{sha}`, `/api/datasets/{repo_id}`, `/api/datasets/{repo_id}/revision/{sha}`, `/api/models/{repo_id}/refs` (_verify_); card at `https://huggingface.co/{repo_id}/resolve/{sha}/README.md` (datasets under `/datasets/`); organisation check `/api/organizations/{name}/overview` (_verify_) | none for public, non-gated repositories; an optional token `NOESIS_HF_TOKEN` (`optional-secret`) only raises limits and never unlocks gated content in first coverage | Hugging Face Terms of Service; repository content is the uploader's, under the licence the repository declares (_verify_). Metadata fields are stored as facts with a locator; the card **body** is user-authored prose whose licence is at best the repository's declared licence and may be unstated, so it is cited by revision URL and digest, not stored | per-IP limits for anonymous use, higher with a token, HTTP 429 (_verify_ numbers); paced at 1 request/second | `sha` is the git commit of the revision and `lastModified` its time: a new `sha` is a new revision, a pinned `sha` is immutable; a renamed repository redirects (_verify_) and the redirect target is recorded as a source-stated rename, never followed to another host; a repository that turns `gated`, `disabled` or answers 404 gets a `withdrawn` or `removed_by_source` revision and keeps its history |
| OpenML | `https://www.openml.org/api/v1/json/data/{id}`, `/data/qualities/{id}`, `/task/{id}`, `/evaluation/list/task/{id}/function/{measure}/limit/{n}` (_verify_ path order) | none for reads; the API key is for uploads only and is never configured | OpenML metadata reusable with attribution, CC BY 4.0 (_verify_); each dataset carries its own declared `licence` field, stored as stated | undocumented (_verify_); paced at 1 request/second | a dataset version is its own id with `name` and `version`; `status` (`active`, `in_preparation`, `deactivated`) changes are new revisions; a changed description or `md5_checksum` is a new revision; evaluations are immutable per run id, a run no longer listed becomes `not-returned` |
| Epoch AI | dataset CSV from `https://epoch.ai/data/` (_verify_ the file path, e.g. a notable-models CSV) | none | CC BY 4.0 with attribution "Epoch AI" (_verify_) | one file per run, 20 MB byte budget | no version numbers: a release is dated by the page's "last updated" stamp (_verify_) or the retrieval time, labelled `retrieval_time`; a changed content digest is a new vintage; a revised estimate is a new revision of that row; a declared row missing from a complete later file is `removed_by_source` |

## Record shape and reuse

Record shapes (`packs/taxonomy.json`): **registry-records** for Hub model and
dataset repositories, OpenML datasets and tasks, and Epoch model entries, each
with revisions; **versioned-documents** for model and dataset cards, cited by
revision `sha` and content digest; **observations** for OpenML run evaluations
and for self-reported card results (`model-index`), each labelled with who
reported it. No new shape is needed.

Reuse points (paths verified in this checkout):

- `src/kb/oss_spdx.py`: declared licences are normalised to SPDX only on an
  exact match against the pinned SPDX License List; Hub values such as
  `other`, `openrail` or model-specific licences stay as declared with
  `license_name` and `license_link` (_verify_ field names), never mapped.
- `src/kb/oss_ecosystem_records.py`: the parse-time `check_no_people` and
  forbidden-field pattern, applied to the person fields listed below.
- `src/kb/entity_history.py`: an Epoch entry and a Hub model repository, or a
  Hub dataset repository and an OpenML dataset, are linked only by a reviewed,
  revertible identity decision; a shared name is never a match.
- Links to literature by stated identifier only: arXiv ids in Hub tags
  (`arxiv:...`) and DOIs in cards or OpenML `citation` resolve through the
  existing paper connector (`src/ingestion/connectors/paper/`) and scholarly
  sources (`src/ingestion/connectors/scholarly/sources.py`); dataset DOIs
  through the DataCite path of `src/ingestion/research_entities_sources.py`.
- Links to OSS ecosystems: the Hub `library_name` is kept as stated text; a
  package link is proposed only where a source names a registry package.
- Transport, budgets and receipts: `src/ingestion/source_pack_runtime.py` and
  `src/ingestion/provider_execution.py`, as in
  `src/ingestion/oss_ecosystem_sources.py`.

## Data minimisation decision

All three sources mix organisation metadata with person data (Hub user
accounts and commit authors, OpenML creators and uploaders, Epoch author
lists). Decision:

- **Stored:** repository id, revision `sha`, `lastModified`, declared licence
  fields as stated, pipeline tag, library, tags, base model, named datasets,
  languages, `gated`/`disabled` flags, file names; card front matter fields
  and digest; OpenML dataset id, name, version, status, declared licence,
  format, qualities, task definitions and evaluation values per run id; Epoch
  model name, organisation, publication date, domain, stated estimates with
  their confidence label, reference link.
- **Redacted:** a repository namespace that is a user account is not stored as
  an organisation; first coverage declares organisation namespaces only,
  checked against the organisation endpoint before acquisition.
- **Excluded:** card body text, commit history and commit authors,
  discussions, Hub `author` when it is a user, download, like and trending
  counts; OpenML `creator`, `contributor`, `uploader` and run uploader ids;
  Epoch `Authors`. Model weights and dataset files are never fetched.
- **Retention:** revisions are kept for provenance with their run; no stored
  field identifies a person, so no erasure workflow applies.
- **Who may query:** `knowledge:technical:ai-models:read` with namespace
  access; writes `...:write`; identity reviews `...:review`.

## Bounded first coverage

| Source | Selection | Caps |
| --- | --- | --- |
| Hugging Face Hub | three public model repositories and two dataset repositories under organisation namespaces, named in the acquisition issue (candidates such as `openai-community/gpt2`, `google-bert/bert-base-uncased`, _verify_) | 5 repositories, 10 declared revisions each, card files of at most 1 MB |
| OpenML | two dataset ids and one task on one of them (candidates such as dataset 61 and its classification task, _verify_) | 2 datasets, 1 task, 100 evaluation rows |
| Epoch AI | the rows of one file that name the declared models | 1 file, 20 rows stored |

Justification: a few named models seen through their Hub revisions and an
Epoch entry, plus a dataset with a task and evaluations, is the smallest
selection that shows declared metadata, independent estimates and run
evaluations side by side without merging them. Every further repository,
dataset or task is a source-pack version bump.

## Live verification

| Source | Status | Checked | Evidence |
| --- | --- | --- | --- |
| Hugging Face Hub | `unverified-live` | - | none yet |
| OpenML | `unverified-live` | - | none yet |
| Epoch AI | `unverified-live` | - | none yet |

Fixtures will be authored, not captured: fictional organisations and
repositories (for example `example-org/fixture-model`), invented commit
shas, OpenML ids and Epoch rows, and dates in 2094-2099, so nothing can be
mistaken for a published record.
