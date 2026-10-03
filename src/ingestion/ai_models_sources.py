"""AI model and dataset registry sources for the Technology ``technology.ai-models`` provider (#2742, AI03-AI05).

The machine-readable copy of ``docs/development/ai-models-evidence/source-audit.md`` (AI01). One native source-pack
connector, ``ai-models``, driven by :mod:`src.ingestion.source_pack_runtime` with three sources of the
``technology-ai-models`` pack (``config/source_packs/technology-ai-models.json``). Each source declares one provider and
a bounded selection; the adapter fetches one declared unit per page and returns provider-neutral statements
(``noesis-ai-model-record-v2``) that :class:`src.kb.ai_models_store.AiModelsProjector` appends as revisions.

* **Hugging Face Hub metadata** (``huggingface-hub``, format ``hf-hub-api-json``) - one public, non-gated repository
  under an organisation namespace per page: the namespace is checked against the organisation endpoint first (a user
  namespace is refused), then the repository metadata, its refs and each declared revision ``sha`` with the card file
  (``README.md``) at that pinned ``sha``. The card's front matter is kept (allow-listed keys) with its digest; the card
  body is cited by its revision URL and digest, never kept. Self-reported ``model-index`` results become observations
  labelled as self-reported. A repository that states ``gated`` or ``disabled`` gets a ``withdrawn`` revision and is
  not read further (an optional ``NOESIS_HF_TOKEN`` only raises limits and never unlocks gated content); a 404 is a
  ``removed_by_source`` revision; a same-host redirect to another repository id is recorded as a source-stated rename
  and never followed to another host.
* **OpenML** (``openml``, format ``openml-json-v1``) - REST API v1 JSON, no key (the upload key is never configured):
  dataset descriptions and qualities per declared dataset id, one task definition and its run evaluations per declared
  measure. ``creator``, ``contributor``, ``uploader`` and the run uploader ids are dropped; the description and the
  citation text are not kept (their digest and the identifiers they state are).
* **Epoch AI** (``epoch-ai``, format ``epoch-models-csv``) - one notable-models CSV per run (20 MB byte budget), the
  rows naming the declared models (20 at most); ``Authors`` is dropped. The release is dated by the data page's
  "last updated" stamp, else the retrieval time (``retrieval_time``).

Every provider is ``unverified-live`` until a dated live run (AI13); endpoints, field names and limits marked *verify*
come from the providers' documentation, not from a live response. Nothing here downloads weights or dataset files,
ranks, scores or judges a model or dataset, counts downloads, likes or trending, interprets a licence, merges
self-reported card results with OpenML evaluations or Epoch estimates, or infers training data, compute or parameters
a source does not state.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError

CONNECTOR = "ai-models"
ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RECORD_CONTRACT = "noesis-ai-model-record-v2"
UNIT_CONTRACT = "noesis-ai-model-unit-v1"
AUDIT = "docs/development/ai-models-evidence/source-audit.md"
SECRET_REF = "NOESIS_HF_TOKEN"
# A placeholder the offline fixtures send so the token path is exercised; it must never appear in any record.
FIXTURE_SECRET = "fixture-hf-token-not-a-real-token"
PROVIDERS = ("huggingface-hub", "openml", "epoch-ai")
FORMATS = {
    "hf-hub-api-json": {"provider": "huggingface-hub"},
    "openml-json-v1": {"provider": "openml"},
    "epoch-models-csv": {"provider": "epoch-ai"},
}
PROVIDER_HOSTS = {"huggingface-hub": "huggingface.co", "openml": "www.openml.org", "epoch-ai": "epoch.ai"}
PACING_S = {"huggingface-hub": 1.0, "openml": 1.0, "epoch-ai": 1.0}
MAX_RETRY_WAIT_S = 60.0
USER_AGENT = "Noesis/ai-models (+https://github.com/Ikey168/Noesis)"
HUB_KINDS = ("model", "dataset")
NEVER_SENTENCE = (
    "Model and dataset registry records as each source stated them, per revision and side by side: no weights or "
    "dataset files downloaded, no capability, safety, quality, risk or openness verdict, no leaderboard or ranking, no "
    "download, like or trending counts, no licence-compliance interpretation, no merging of self-reported card results "
    "with OpenML evaluations or Epoch estimates, and no inferred training data, compute or parameters."
)
EXCLUSIONS = (
    "downloading model weights or dataset files",
    "capability, safety, quality, risk or openness verdicts",
    "leaderboards or rankings",
    "download, like or trending counts",
    "licence-compliance interpretation",
    "merging self-reported card results with OpenML evaluations or Epoch estimates",
    "inferring training data, compute or parameters a source does not state",
    "card body text, commit history, commit authors and discussions",
    "person fields (Hub user authors, OpenML creators, contributors and uploaders, Epoch authors)",
    "gated or private Hub repositories",
)
DOCUMENTED_NOT_ACQUIRED = {
    "papers-with-code": "service discontinued in 2025 (verify); documented, not acquired",
    "hub-leaderboards-and-spaces": "rankings and applications; excluded by the non-goals",
    "gated-and-private-hub-repositories": "need an accepted access request per repository; out of first coverage",
    "kaggle-datasets": "account and terms acceptance required",
    "epoch-benchmarking-data": "a second Epoch dataset; revisit after first coverage",
}
# Hard ceilings the adapter enforces on top of the source-pack budgets (AI01 bounded first coverage).
CAPS = {
    "huggingface-hub": {"repositories": 5, "revisions_per_repository": 10, "card_bytes": 1_000_000},
    "openml": {"datasets": 2, "tasks": 1, "evaluation_rows": 100},
    "epoch-ai": {"files": 1, "bytes": 20_000_000, "rows": 20},
}
MAX_FILE_NAMES = 1000
MAX_QUALITIES = 200
BOUNDED_COVERAGE = {
    "huggingface-hub": {
        "selection": "three public model repositories and two dataset repositories under organisation namespaces, "
                     "named in the acquisition issue (candidates such as openai-community/gpt2, "
                     "google-bert/bert-base-uncased, verify)",
        "caps": "5 repositories, 10 declared revisions each, card files of at most 1 MB",
    },
    "openml": {
        "selection": "two dataset ids and one task on one of them (candidates such as dataset 61 and its "
                     "classification task, verify)",
        "caps": "2 datasets, 1 task, 100 evaluation rows",
    },
    "epoch-ai": {
        "selection": "the rows of one file that name the declared models",
        "caps": "1 file, 20 rows stored",
    },
    "justification": "a few named models seen through their Hub revisions and an Epoch entry, plus a dataset with a "
                     "task and evaluations, is the smallest selection that shows declared metadata, independent "
                     "estimates and run evaluations side by side without merging them; every further repository, "
                     "dataset or task is a source-pack version bump",
}
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "huggingface-hub": {
        "publisher": "Hugging Face",
        "delivers": "model and dataset repository metadata per revision: declared licence, pipeline tag, library, "
                    "tags, base model, datasets named, languages, card front matter including self-reported "
                    "model-index results, file list",
        "access": "Hub HTTP API, JSON; anonymous for public, non-gated repositories",
        "endpoints": [
            "https://huggingface.co/api/models/{repo_id}",
            "https://huggingface.co/api/models/{repo_id}/revision/{sha}",
            "https://huggingface.co/api/datasets/{repo_id}",
            "https://huggingface.co/api/datasets/{repo_id}/revision/{sha}",
            "https://huggingface.co/api/models/{repo_id}/refs (verify)",
            "https://huggingface.co/{repo_id}/resolve/{sha}/README.md (datasets under /datasets/)",
            "https://huggingface.co/api/organizations/{name}/overview (verify)",
        ],
        "authentication": f"none for public, non-gated repositories; an optional token {SECRET_REF} "
                          "(optional-secret) only raises limits and never unlocks gated content in first coverage",
        "key_handling": "the token is sent as a bearer header only and never recorded in evidence, URLs, receipts or "
                        "records",
        "licence": "Hugging Face Terms of Service; repository content is the uploader's, under the licence the "
                   "repository declares (verify)",
        "redistribution": "metadata fields stored as facts with a locator; the card body is cited by revision URL and "
                          "digest, not stored",
        "rate_limits": "per-IP limits for anonymous use, higher with a token, HTTP 429 (verify numbers); paced at 1 "
                       "request/second; one retry after HTTP 429",
        "revision_model": "sha is the git commit of the revision and lastModified its time: a new sha is a new "
                          "revision, a pinned sha is immutable; a renamed repository redirects (verify) and the "
                          "redirect target is recorded as a source-stated rename, never followed to another host; a "
                          "repository that turns gated, disabled or answers 404 gets a withdrawn or removed_by_source "
                          "revision and keeps its history",
        "personal_data": "Hub user authors and commit authors are excluded; a user namespace is refused",
        "unavailable_fallback": "a failed unit (HTTP error, 401/403, 429 after one retry, redirect to an undeclared "
                                "host, schema drift, a response over the byte budget) fails with its code and a "
                                "receipt; earlier revisions stay current and nothing is marked removed",
        "dropped_fields": ["author", "downloads", "downloadsAllTime", "likes", "trendingScore", "spaces",
                           "card body", "commit history", "discussions"],
        "status": "unverified-live",
        "verify": ["refs and organisation endpoint paths", "rename redirect behaviour", "rate-limit numbers",
                   "license_name and license_link field names", "terms of service wording"],
    },
    "openml": {
        "publisher": "OpenML (Open Machine Learning Foundation)",
        "delivers": "dataset descriptions and versions with declared licence, computed data qualities, tasks, and run "
                    "evaluations per task",
        "access": "REST API v1 JSON, no key for reads (verify that v1 stays served after OpenML's server migration)",
        "endpoints": [
            "https://www.openml.org/api/v1/json/data/{id}",
            "https://www.openml.org/api/v1/json/data/qualities/{id}",
            "https://www.openml.org/api/v1/json/task/{id}",
            ("https://www.openml.org/api/v1/json/evaluation/list/task/{id}/function/{measure}/limit/{n} "
             "(verify path order)"),
        ],
        "authentication": "none for reads; the API key is for uploads only and is never configured",
        "key_handling": "no key; nothing secret is sent or stored",
        "licence": "OpenML metadata reusable with attribution, CC BY 4.0 (verify); each dataset carries its own "
                   "declared licence field, stored as stated",
        "redistribution": "attribution-required",
        "rate_limits": "undocumented (verify); paced at 1 request/second",
        "revision_model": "a dataset version is its own id with name and version; status (active, in_preparation, "
                          "deactivated) changes are new revisions; a changed description or md5_checksum is a new "
                          "revision; evaluations are immutable per run id, a run no longer listed becomes "
                          "not-returned",
        "personal_data": "creator, contributor, uploader and run uploader ids are excluded",
        "unavailable_fallback": "as Hugging Face Hub",
        "dropped_fields": ["creator", "contributor", "uploader", "uploader_name", "description text",
                           "citation text"],
        "status": "unverified-live",
        "verify": ["API v1 still served after the server migration", "evaluation list path order",
                   "the 412 no-results answer", "terms (CC BY 4.0)", "rate limits"],
    },
    "epoch-ai": {
        "publisher": "Epoch AI",
        "delivers": "curated records of notable models: organisation, publication date, domain, parameters, training "
                    "compute and dataset size as Epoch estimates them, with a confidence label",
        "access": "CSV download from the data page (verify URL and file name)",
        "endpoints": ["https://epoch.ai/data/ (verify the file path, e.g. a notable-models CSV)"],
        "authentication": "none",
        "key_handling": "no key; nothing secret is sent or stored",
        "licence": "CC BY 4.0 with attribution \"Epoch AI\" (verify)",
        "redistribution": "attribution-required",
        "rate_limits": "one file per run, 20 MB byte budget",
        "revision_model": "no version numbers: a release is dated by the page's \"last updated\" stamp (verify) or "
                          "the retrieval time, labelled retrieval_time; a changed content digest is a new vintage; a "
                          "revised estimate is a new revision of that row; a declared row missing from a complete "
                          "later file is removed_by_source",
        "personal_data": "Authors is excluded",
        "unavailable_fallback": "as Hugging Face Hub",
        "dropped_fields": ["Authors", "notes columns", "Abstract", "Citations"],
        "status": "unverified-live",
        "verify": ["file URL and name", "column names", "last-updated stamp on the data page", "terms (CC BY 4.0)"],
    },
}
LIVE_VERIFICATION = {
    provider: {"status": "unverified-live", "checked": None, "evidence": None,
               "intended": "verified-live after a dated bounded run (AI13, #2742)",
               "note": "no dated live run from this runtime (huggingface.co, www.openml.org and epoch.ai blocked by "
                       "the egress proxy); authored offline fixtures only"}
    for provider in PROVIDERS
}
MINIMISATION = {
    "decision": "organisation-level registry metadata only; no stored field identifies a person",
    "stored": "repository id, revision sha, lastModified, declared licence fields as stated, pipeline tag, library, "
              "tags, base model, named datasets, languages, gated/disabled flags, file names; card front matter "
              "fields and digest; OpenML dataset id, name, version, status, declared licence, format, qualities, task "
              "definitions and evaluation values per run id; Epoch model name, organisation, publication date, "
              "domain, stated estimates with their confidence label, reference link",
    "redacted": "a repository namespace that is a user account is not stored as an organisation; first coverage "
                "declares organisation namespaces only, checked against the organisation endpoint before "
                "acquisition",
    "excluded": "card body text, commit history and commit authors, discussions, Hub author when it is a user, "
                "download, like and trending counts; OpenML creator, contributor, uploader and run uploader ids; "
                "Epoch Authors; model weights and dataset files are never fetched",
    "retention": "revisions are kept for provenance with their run; no stored field identifies a person, so no "
                 "erasure workflow applies",
    "who_may_query": "knowledge:technical:ai-models:read with namespace access; writes "
                     "knowledge:technical:ai-models:write; identity reviews knowledge:technical:ai-models:review",
}
# Card front-matter keys kept (allow-list): anything else, a person field above all, is never stored.
FRONT_MATTER_KEYS = (
    "license", "license_name", "license_link", "language", "library_name", "pipeline_tag", "tags", "datasets",
    "base_model", "base_model_relation", "metrics", "task_categories", "task_ids", "size_categories",
    "source_datasets", "pretty_name", "paperswithcode_id", "doi", "arxiv",
)
LICENCE_KEYS = ("license", "license_name", "license_link")
# Epoch columns (verify against the live file) and how they are kept.
EPOCH_COLUMNS = {
    "model": "Model",
    "organization": "Organization",
    "publication_date": "Publication date",
    "domain": "Domain",
    "confidence": "Confidence",
    "link": "Link",
}
EPOCH_ESTIMATES = {
    "parameters": ("Parameters", "parameters"),
    "training_compute": ("Training compute (FLOP)", "FLOP"),
    "training_dataset_size": ("Training dataset size (datapoints)", "datapoints"),
}
SELF_REPORTED = "the repository's card (model-index), self-reported by the repository; not verified by Noesis"
OPENML_REPORTED = "OpenML run evaluation, computed by OpenML for a run uploaded to OpenML"
EPOCH_REPORTED = "Epoch AI, curated estimates with Epoch's confidence label"
_SHA = re.compile(r"^[0-9a-f]{40}$")
_REPO = re.compile(r"^[A-Za-z0-9][\w.-]{0,95}/[A-Za-z0-9][\w.-]{0,95}$")
_ARXIV = re.compile(r"(?:arxiv\.org/(?:abs|pdf)/|arxiv:)\s*(\d{4}\.\d{4,5})(?:v\d+)?", re.IGNORECASE)
_DOI = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>,;]+)", re.IGNORECASE)
_OPENML_DATASET = re.compile(r"openml\.org/(?:d/|search\?type=data&(?:amp;)?id=)(\d+)", re.IGNORECASE)
_PURL = re.compile(r"^pkg:(pypi|npm|cargo|maven)/(.+)$")
_HUB_URL = re.compile(r"https://huggingface\.co/(datasets/)?(?!api/|datasets/)([A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w.-]*)")
_STAMP = re.compile(r"last\s+updated[^0-9A-Za-z]{0,20}(\d{4}-\d{2}-\d{2}|[A-Z][a-z]+ \d{1,2},? \d{4})", re.IGNORECASE)


class AiModelsFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def unverified(provider: str) -> bool:
    return LIVE_VERIFICATION.get(provider, {}).get("status") != "verified-live"


def text(value: Any) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    return raw or None


def iso_instant(value: Any) -> str | None:
    """A source date or instant as UTC ISO-8601 (``...Z``); dates are UTC midnight; ``None`` when absent or invalid."""
    raw = text(value)
    if raw is None:
        return None
    raw = raw.replace(" ", "T").replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(raw if len(raw) > 10 else raw + "T00:00:00+00:00")
    except ValueError:
        for pattern in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"):
            try:
                moment = datetime.strptime(str(value).strip(), pattern).replace(tzinfo=UTC)
                break
            except ValueError:
                continue
        else:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return sorted({str(v).strip() for v in value if str(v).strip()})
    return [str(value).strip()] if str(value).strip() else []


def stated_identifiers(*texts: Any) -> dict[str, list[str]]:
    """arXiv ids, DOIs, OpenML dataset ids, Hub repository URLs and package URLs exactly as stated."""
    arxiv, doi, openml, packages, hub = set(), set(), set(), set(), set()
    for item in texts:
        for value in (item if isinstance(item, (list, tuple, set)) else [item]):
            if value is None:
                continue
            raw = str(value).strip()
            arxiv |= {m.group(1) for m in _ARXIV.finditer(raw)}
            if raw.casefold().startswith("doi:"):
                raw_doi = raw[4:].strip()
                if _DOI.fullmatch(raw_doi):
                    doi.add(raw_doi.lower().rstrip("."))
            else:
                doi |= {m.group(1).lower().rstrip(".") for m in _DOI.finditer(raw)}
            openml |= {m.group(1) for m in _OPENML_DATASET.finditer(raw)}
            hub |= {f"{'dataset' if m.group(1) else 'model'}:{m.group(2)}" for m in _HUB_URL.finditer(raw)}
            purl = _PURL.fullmatch(raw)
            if purl:
                packages.add(raw)
    return {"arxiv": sorted(arxiv), "doi": sorted(doi), "openml_dataset": sorted(openml), "packages": sorted(packages),
            "hub": sorted(hub)}


# ------------------------------------------------------------------ declarations


def ai_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    """The source's bounded selection, checked against the audited caps before anything is fetched."""
    config = dict(source.get("ai_models") or {})
    provider, fmt = config.get("provider"), config.get("format")
    if provider not in PROVIDERS or FORMATS.get(str(fmt), {}).get("provider") != provider:
        raise SourcePackError("invalid_manifest", "ai-models sources declare a known provider and its runtime format")
    host = (urlsplit(str(source.get("endpoint") or "")).hostname or "").casefold()
    if host != PROVIDER_HOSTS[provider]:
        raise SourcePackError("invalid_manifest", f"{provider} is fetched from {PROVIDER_HOSTS[provider]}")
    auth = dict(source.get("auth") or {})
    if provider == "huggingface-hub":
        if auth.get("kind") not in {"none", "optional-secret"} or (
                auth.get("kind") == "optional-secret" and auth.get("secret_ref") != SECRET_REF):
            raise SourcePackError("invalid_manifest", f"the Hub uses the optional secret {SECRET_REF} or none")
    elif auth.get("kind") != "none":
        raise SourcePackError("invalid_manifest", f"{provider} reads carry no credential (an upload key is never "
                                                  "configured)")
    selection = dict(config.get("selection") or {})
    budgets = dict(source.get("budgets") or {})
    max_pages = int(budgets.get("max_pages", 1))
    caps = CAPS[provider]
    units: list[dict[str, Any]] = []
    if provider == "huggingface-hub":
        repositories = [dict(r) for r in selection.get("repositories") or []]
        if not 1 <= len(repositories) <= caps["repositories"]:
            raise SourcePackError("unbounded_source", f"the Hub source declares 1-{caps['repositories']} repositories")
        seen = set()
        for repo in repositories:
            repo_id, kind = str(repo.get("repo_id") or ""), repo.get("kind")
            if not _REPO.fullmatch(repo_id):
                raise SourcePackError("invalid_manifest", f"{repo_id!r}: repositories are declared as "
                                                          "namespace/name under an organisation namespace")
            if kind not in HUB_KINDS:
                raise SourcePackError("invalid_manifest", f"{repo_id}: kind is one of {HUB_KINDS}")
            revisions = [str(r) for r in repo.get("revisions") or []]
            if len(revisions) > caps["revisions_per_repository"] or len(set(revisions)) != len(revisions):
                raise SourcePackError("unbounded_source", f"{repo_id}: at most "
                                                          f"{caps['revisions_per_repository']} distinct revisions")
            if any(not _SHA.fullmatch(r) for r in revisions):
                raise SourcePackError("invalid_manifest", f"{repo_id}: declared revisions are pinned 40-hex shas")
            if (kind, repo_id.casefold()) in seen:
                raise SourcePackError("invalid_manifest", f"{repo_id} is declared twice")
            seen.add((kind, repo_id.casefold()))
            units.append({"unit": "repository", "repo_id": repo_id, "kind": kind, "revisions": revisions})
    elif provider == "openml":
        datasets = [int(d) for d in selection.get("datasets") or []]
        tasks = [dict(t) for t in selection.get("tasks") or []]
        limit = int(selection.get("evaluation_limit") or caps["evaluation_rows"])
        if not 1 <= len(datasets) <= caps["datasets"] or len(set(datasets)) != len(datasets):
            raise SourcePackError("unbounded_source", f"OpenML declares 1-{caps['datasets']} distinct dataset ids")
        if len(tasks) > caps["tasks"]:
            raise SourcePackError("unbounded_source", f"OpenML declares at most {caps['tasks']} task")
        if not 1 <= limit <= caps["evaluation_rows"]:
            raise SourcePackError("unbounded_source", f"OpenML evaluations are capped at {caps['evaluation_rows']} "
                                                      "rows")
        units += [{"unit": "dataset", "dataset_id": d} for d in datasets]
        for task in tasks:
            measures = [str(m) for m in task.get("measures") or []]
            if not measures or len(set(measures)) != len(measures) or any(
                    not re.fullmatch(r"[a-z_]+", m) for m in measures):
                raise SourcePackError("invalid_manifest", "an OpenML task names its evaluation measures")
            units.append({"unit": "task", "task_id": int(task["id"]), "measures": measures,
                          "limit_per_measure": max(1, limit // len(measures))})
    else:
        models = [str(m).strip() for m in selection.get("models") or [] if str(m).strip()]
        if not 1 <= len(models) <= caps["rows"] or len(set(models)) != len(models):
            raise SourcePackError("unbounded_source", f"Epoch declares 1-{caps['rows']} distinct model names")
        file_url, page_url = str(selection.get("file") or ""), selection.get("page")
        for url in [file_url, *([page_url] if page_url else [])]:
            parts = urlsplit(str(url))
            if parts.scheme != "https" or (parts.hostname or "").casefold() != host:
                raise SourcePackError("invalid_manifest", "Epoch files and pages are HTTPS resources on epoch.ai")
        if int(budgets.get("max_bytes", 0)) > caps["bytes"]:
            raise SourcePackError("unbounded_source", "the Epoch byte budget is 20 MB")
        units.append({"unit": "file", "file": file_url, "page": page_url, "models": models})
    if len(units) > max_pages:
        raise SourcePackError("invalid_manifest", "more declared units than the source's page budget")
    return {"provider": provider, "format": fmt, "namespace": config.get("namespace") or "global", "units": units,
            "live_verification": config.get("live_verification") or "unverified-live"}


# ------------------------------------------------------------------ parsing: Hugging Face Hub


def split_card(raw: bytes) -> dict[str, Any]:
    """Front matter (allow-listed keys) and digests of a card file; the body is never returned."""
    content = raw.decode("utf-8", errors="replace")
    front_text, body = "", content
    if content.startswith("---"):
        match = re.match(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", content, re.DOTALL)
        if match:
            front_text, body = match.group(1), content[match.end():]
    front: dict[str, Any] = {}
    if front_text:
        import yaml

        try:
            loaded = yaml.safe_load(front_text)
        except yaml.YAMLError as exc:
            raise AiModelsFormatError("schema_drift", "card front matter is not YAML") from exc
        front = dict(loaded) if isinstance(loaded, Mapping) else {}
    kept = {k: json.loads(canonical(front[k])) for k in FRONT_MATTER_KEYS if front.get(k) not in (None, "", [])}
    model_index = json.loads(canonical(front.get("model-index"))) if front.get("model-index") else None
    return {"front_matter": kept, "front_matter_sha256": sha256_bytes(front_text.encode()) if front_text else None,
            "body_sha256": sha256_bytes(body.encode()), "sha256": sha256_bytes(raw), "bytes": len(raw),
            "model_index": model_index}


def _licence(card_front: Mapping[str, Any], card_data: Mapping[str, Any], tags: Sequence[str]) -> dict[str, Any]:
    """Declared licence fields as stated, with where they were stated; never mapped here."""
    for origin, values in (("card front matter", card_front), ("repository card data", card_data)):
        stated = {k: values[k] for k in LICENCE_KEYS if values.get(k) not in (None, "", [])}
        if stated:
            return {**{k: (stated[k] if isinstance(stated[k], str) else canonical(stated[k])) for k in stated},
                    "stated_in": origin}
    tagged = sorted(t.split(":", 1)[1] for t in tags if t.startswith("license:"))
    if tagged:
        return {"license": tagged[0] if len(tagged) == 1 else " ".join(tagged), "stated_in": "repository tags"}
    return {"stated_in": "none stated"}


def hub_card_url(kind: str, repo_id: str, sha: str) -> str:
    prefix = "datasets/" if kind == "dataset" else ""
    return f"https://huggingface.co/{prefix}{repo_id}/resolve/{sha}/README.md"


def hub_revision_url(kind: str, repo_id: str, sha: str) -> str:
    prefix = "datasets/" if kind == "dataset" else ""
    return f"https://huggingface.co/{prefix}{repo_id}/tree/{sha}"


def parse_hub_revision(meta: Mapping[str, Any], *, kind: str, repo_id: str, card: Mapping[str, Any] | None,
                       card_status: str) -> list[dict[str, Any]]:
    """One Hub revision statement plus its self-reported results; allow-listed fields only."""
    sha = str(meta.get("sha") or "")
    if not _SHA.fullmatch(sha):
        raise AiModelsFormatError("schema_drift", "the Hub response states no revision sha")
    last_modified = iso_instant(meta.get("lastModified"))
    if last_modified is None:
        raise AiModelsFormatError("schema_drift", "the Hub response states no lastModified")
    if str(meta.get("id") or repo_id) != repo_id:
        raise AiModelsFormatError("schema_drift", "the Hub answered for another repository")
    card_data = dict(meta.get("cardData") or {})
    tags = _strings(meta.get("tags"))
    front = dict((card or {}).get("front_matter") or {})
    siblings = [str(dict(s).get("rfilename")) for s in meta.get("siblings") or [] if dict(s).get("rfilename")]
    files = sorted(set(siblings))
    declared = {
        "pipeline_tag": text(meta.get("pipeline_tag") or card_data.get("pipeline_tag")),
        "library_name": text(meta.get("library_name") or card_data.get("library_name")),
        "tags": tags,
        "base_model": _strings(card_data.get("base_model") or front.get("base_model")),
        "datasets": _strings(card_data.get("datasets") or front.get("datasets")),
        "languages": _strings(card_data.get("language") or front.get("language")),
        "licence": _licence(front, card_data, tags),
    }
    card_view: dict[str, Any] = {"status": card_status, "url": hub_card_url(kind, repo_id, sha)}
    if card is not None:
        card_view.update({k: card[k] for k in ("sha256", "body_sha256", "bytes", "front_matter",
                                               "front_matter_sha256")})
        card_view["body"] = "cited by revision URL and digest; never stored"
    identifiers = stated_identifiers(tags, front.get("doi"), front.get("arxiv"), _strings(front.get("tags")),
                                     _strings(front.get("source_datasets")), card_data.get("paperswithcode_id"))
    statement = {
        "record_type": "hub_repository_revision", "source": "huggingface-hub", "kind": kind, "repo_id": repo_id,
        "organisation": repo_id.split("/", 1)[0], "sha": sha, "last_modified": last_modified, "state": "published",
        "declared": {k: v for k, v in declared.items() if v not in (None, [], {})},
        "files": {"names": files[:MAX_FILE_NAMES], "count": len(files), "truncated": len(files) > MAX_FILE_NAMES},
        "card": card_view, "identifiers": identifiers, "url": hub_revision_url(kind, repo_id, sha),
    }
    results = []
    model_index = (card or {}).get("model_index") if card is not None else card_data.get("model-index")
    for entry in model_index or []:
        entry = dict(entry or {})
        for result in entry.get("results") or []:
            result = dict(result or {})
            task, dataset = dict(result.get("task") or {}), dict(result.get("dataset") or {})
            for metric in result.get("metrics") or []:
                metric = dict(metric or {})
                if metric.get("value") is None:
                    continue
                results.append({
                    "record_type": "observation", "observation_kind": "self_reported_result",
                    "source": "huggingface-hub", "subject": {"kind": kind, "repo_id": repo_id}, "sha": sha,
                    "reported_by": SELF_REPORTED, "model_name": text(entry.get("name")),
                    "task": {k: text(task.get(k)) for k in ("type", "name") if text(task.get(k))},
                    "dataset": {k: text(dataset.get(k)) for k in ("type", "name", "config", "split", "revision")
                                if text(dataset.get(k))},
                    "metric": {**{k: text(metric.get(k)) for k in ("type", "name") if text(metric.get(k))},
                               "value_text": str(metric["value"]),
                               "verified": bool(metric.get("verified")) if "verified" in metric else None},
                    "result_source": {k: text(dict(result.get("source") or {}).get(k)) for k in ("name", "url")
                                      if text(dict(result.get("source") or {}).get(k))},
                    "url": hub_card_url(kind, repo_id, sha),
                })
    return [statement, *results]


def hub_state_statement(kind: str, repo_id: str, state: str, detail: Mapping[str, Any]) -> dict[str, Any]:
    """A source-stated state revision (withdrawn, removed_by_source or renamed); earlier revisions are kept."""
    prefix = "datasets/" if kind == "dataset" else ""
    return {"record_type": "hub_repository_revision", "source": "huggingface-hub", "kind": kind, "repo_id": repo_id,
            "organisation": repo_id.split("/", 1)[0], "state": state, "state_detail": dict(detail),
            "url": f"https://huggingface.co/{prefix}{repo_id}"}


def parse_hub_refs(payload: Any, *, kind: str, repo_id: str) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise AiModelsFormatError("schema_drift", "refs response is not an object")
    refs = {}
    for group in ("branches", "tags"):
        refs[group] = sorted(({"name": str(dict(r).get("name")), "target_commit": str(dict(r).get("targetCommit"))}
                              for r in payload.get(group) or [] if dict(r).get("name")), key=lambda r: r["name"])
    return {"record_type": "hub_refs", "source": "huggingface-hub", "kind": kind, "repo_id": repo_id,
            "organisation": repo_id.split("/", 1)[0], "refs": refs,
            "url": f"https://huggingface.co/api/{kind}s/{repo_id}/refs"}


# ------------------------------------------------------------------ parsing: OpenML


def _openml_url(kind: str, native_id: int) -> str:
    return f"https://www.openml.org/search?type={kind}&id={native_id}"


def parse_openml_dataset(description: Any, qualities: Any, *, dataset_id: int) -> dict[str, Any]:
    body = dict(dict(description or {}).get("data_set_description") or {})
    if not body or str(body.get("id")) != str(dataset_id):
        raise AiModelsFormatError("schema_drift", "OpenML answered for another dataset or no dataset")
    status = text(body.get("status"))
    if status not in {"active", "in_preparation", "deactivated"}:
        raise AiModelsFormatError("schema_drift", f"unknown OpenML dataset status {status!r}")
    rows = dict(dict(qualities or {}).get("data_qualities") or {}).get("quality") or []
    kept = {}
    for row in rows[:MAX_QUALITIES]:
        row = dict(row)
        if text(row.get("name")):
            kept[str(row["name"])] = None if row.get("value") in (None, "") else str(row["value"])
    description_text = text(body.get("description"))
    statement = {
        "record_type": "openml_dataset_revision", "source": "openml", "dataset_id": int(dataset_id),
        "name": text(body.get("name")), "version": text(body.get("version")),
        "version_label": text(body.get("version_label")), "status": status,
        "upload_date": iso_instant(body.get("upload_date")), "licence": {"licence": text(body.get("licence")),
                                                                         "stated_in": "OpenML licence field"},
        "format": text(body.get("format")), "md5_checksum": text(body.get("md5_checksum")),
        "description_sha256": sha256_bytes(description_text.encode()) if description_text else None,
        "default_target_attribute": text(body.get("default_target_attribute")),
        "tags": _strings(body.get("tag")), "qualities": kept,
        "identifiers": stated_identifiers(body.get("citation"), body.get("paper_url"), body.get("original_data_url")),
        "url": _openml_url("data", dataset_id),
    }
    return {k: v for k, v in statement.items() if v is not None}


def parse_openml_task(payload: Any, *, task_id: int) -> dict[str, Any]:
    body = dict(dict(payload or {}).get("task") or {})
    if not body or str(body.get("task_id")) != str(task_id):
        raise AiModelsFormatError("schema_drift", "OpenML answered for another task or no task")
    inputs = {str(dict(i).get("name")): dict(i) for i in body.get("input") or []}
    source = dict(inputs.get("source_data", {}).get("data_set") or {})
    procedure = dict(inputs.get("estimation_procedure", {}).get("estimation_procedure") or {})
    measures = dict(inputs.get("evaluation_measures", {}).get("evaluation_measures") or {}).get(
        "evaluation_measure")
    if not source.get("data_set_id"):
        raise AiModelsFormatError("schema_drift", "the task states no source dataset")
    return {
        "record_type": "openml_task_revision", "source": "openml", "task_id": int(task_id),
        "task_type": text(body.get("task_type")), "task_type_id": text(body.get("task_type_id")),
        "dataset_id": int(source["data_set_id"]), "target_feature": text(source.get("target_feature")),
        "estimation_procedure": {k: text(procedure.get(k)) for k in ("id", "type") if text(procedure.get(k))},
        "evaluation_measures": _strings(measures), "url": _openml_url("task", task_id),
    }


def parse_openml_evaluations(payload: Any, *, task_id: int, measure: str, limit: int) -> list[dict[str, Any]]:
    rows = dict(dict(payload or {}).get("evaluations") or {}).get("evaluation") or []
    if isinstance(rows, Mapping):
        rows = [rows]
    if len(rows) > limit:
        raise AiModelsFormatError("budget_exhausted", "more evaluation rows than requested")
    out, seen = [], set()
    for row in rows:
        row = dict(row)
        if str(row.get("task_id")) != str(task_id) or str(row.get("function")) != measure:
            raise AiModelsFormatError("schema_drift", "an evaluation row names another task or measure")
        run_id = int(row["run_id"])
        if run_id in seen:
            raise AiModelsFormatError("schema_drift", "a run is listed twice")
        seen.add(run_id)
        out.append({
            "record_type": "observation", "observation_kind": "openml_run_evaluation", "source": "openml",
            "task_id": int(task_id), "run_id": run_id, "measure": measure,
            "value_text": None if row.get("value") is None else str(row["value"]),
            "flow_id": text(row.get("flow_id")), "flow_name": text(row.get("flow_name")),
            "setup_id": text(row.get("setup_id")), "dataset_id": int(row["data_id"]) if row.get("data_id") else None,
            "upload_time": iso_instant(row.get("upload_time")), "reported_by": OPENML_REPORTED,
            "url": _openml_url("run", run_id),
        })
    listing = {"record_type": "evaluation_listing", "source": "openml", "task_id": int(task_id), "measure": measure,
               "run_ids": sorted(seen), "complete": len(rows) < limit,
               "url": _openml_url("task", task_id)}
    return [*[{k: v for k, v in o.items() if v is not None} for o in out], listing]


# ------------------------------------------------------------------ parsing: Epoch AI


def last_updated_stamp(raw: bytes) -> str | None:
    match = _STAMP.search(raw.decode("utf-8", errors="replace"))
    return iso_instant(match.group(1)) if match else None


def parse_epoch_csv(raw: bytes, *, models: Sequence[str], file_url: str) -> list[dict[str, Any]]:
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        header = list(reader.fieldnames or [])
        rows = list(reader)
    except (UnicodeDecodeError, csv.Error) as exc:
        raise AiModelsFormatError("schema_drift", "the Epoch file is not CSV") from exc
    required = {EPOCH_COLUMNS["model"], EPOCH_COLUMNS["organization"], EPOCH_COLUMNS["publication_date"]}
    missing = sorted(required - set(header))
    if missing:
        raise AiModelsFormatError("schema_drift", f"the Epoch file lacks columns {missing}")
    wanted = set(models)
    found: dict[str, dict[str, str]] = {}
    for row in rows:
        name = (row.get(EPOCH_COLUMNS["model"]) or "").strip()
        if name in wanted:
            if name in found:
                raise AiModelsFormatError("schema_drift", f"the Epoch file states {name!r} twice")
            found[name] = row
    statements = []
    for name in sorted(found):
        row = found[name]
        estimates = {}
        for key, (column, unit) in EPOCH_ESTIMATES.items():
            value = text(row.get(column))
            if value is not None:  # never inferred where Epoch states none
                estimates[key] = {"value_text": value, "unit": unit, "column": column}
        confidence = text(row.get(EPOCH_COLUMNS["confidence"]))
        link = text(row.get(EPOCH_COLUMNS["link"]))
        statement = {
            "record_type": "epoch_model_revision", "source": "epoch-ai", "model": name,
            "organization": text(row.get(EPOCH_COLUMNS["organization"])),
            "publication_date": iso_instant(row.get(EPOCH_COLUMNS["publication_date"])),
            "domain": text(row.get(EPOCH_COLUMNS["domain"])),
            "estimates": {k: {**v, "confidence": confidence} for k, v in estimates.items()},
            "confidence": confidence, "reported_by": EPOCH_REPORTED,
            "reference": {"link": link} if link else {},
            "identifiers": stated_identifiers(link),
            "url": file_url,
        }
        statements.append({k: v for k, v in statement.items() if v is not None})
    statements.append({"record_type": "epoch_listing", "source": "epoch-ai", "declared": sorted(wanted),
                       "present": sorted(found), "complete": True, "url": file_url})
    return statements


# ------------------------------------------------------------------ adapter


class AiModelsAdapter:
    """Fetch the declared units on the runtime's default transport; one page per repository, dataset, task or file."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None, sleep: Callable[[float], None] | None = None,
                 clock: Callable[[], float] | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.declared = ai_declaration(self.source)
        self.provider = self.declared["provider"]
        # Only the Hub source may carry the optional token; OpenML and Epoch requests never send a credential.
        self.secret = secret if self.provider == "huggingface-hub" else None
        live = transport is None
        if live:
            from functools import partial

            transport = partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.transport = transport
        self.sleep = sleep or (time.sleep if live else (lambda _s: None))
        self.clock = clock or time.monotonic
        self.pacing_s = PACING_S[self.provider]
        self._last: float | None = None
        self._organisations: dict[str, bool] = {}
        self.requests = 0
        self.retries = 0
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "ai_models": {"provider": self.provider, "format": self.declared["format"],
                          "units": len(self.declared["units"]), "caps": CAPS[self.provider],
                          "pacing_s": self.pacing_s, "keyed": bool(self.secret),
                          "live_verification": LIVE_VERIFICATION[self.provider]["status"]},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError("parameter_forbidden", "runtime adapter received undeclared controls")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "ai-models runs fetch the declared selection only")

    # -------------------------------------------------------------- transport

    def _pace(self) -> None:
        if self.pacing_s and self._last is not None:
            wait = self.pacing_s - (self.clock() - self._last)
            if wait > 0:
                self.sleep(wait)
        self._last = self.clock()

    def _get(self, url: str, *, accept: str = "application/json",
             allow: frozenset[int] = frozenset()) -> tuple[int, bytes, str, str]:
        """One paced request with one retry after HTTP 429; returns status, body, final URL and evidence origin."""
        from src.ingestion.source_pack_runtime import _retry_after_ms

        host = PROVIDER_HOSTS[self.provider]
        parts = urlsplit(url)
        if (parts.hostname or "").casefold() != host or parts.scheme != "https":
            raise SourcePackError("network_policy", "declared units are fetched from the provider's host only")
        headers = {"Accept": accept, "User-Agent": USER_AGENT}
        if self.secret:
            headers["Authorization"] = f"Bearer {self.secret}"
        base, _, query = url.partition("?")
        for attempt in (0, 1):
            self._pace()
            self.requests += 1
            response = self.transport(url=base, params=parse_qsl(query, keep_blank_values=True), headers=headers,
                                      timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
            final_url = str(response.get("final_url") or url)
            if (urlsplit(final_url).hostname or "").casefold() != host:
                raise SourcePackError("network_policy", "response was served from another host; never followed")
            status = int(response.get("status", 200))
            response_headers = {str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()}
            content = response.get("content", b"")
            raw = content.encode() if isinstance(content, str) else bytes(content)
            if len(raw) > int(self.definition["limits"]["max_bytes"]):
                raise SourcePackError("response_too_large", "response exceeds its byte limit")
            if status == 429:
                wait_ms = _retry_after_ms(response_headers.get("retry-after") or 1)
                if attempt == 0 and wait_ms / 1000 <= MAX_RETRY_WAIT_S:
                    self.retries += 1
                    self.sleep(wait_ms / 1000)
                    continue
                raise SourcePackError("rate_limited", "provider quota is temporarily exhausted after one retry",
                                      retry_after_ms=wait_ms)
            if status in allow:
                return status, raw, final_url, "fixture" if response.get("origin") == "fixture" else "live"
            if status in {401, 403}:
                raise SourcePackError("authentication_failed", f"request refused (HTTP {status})")
            if status >= 500:
                raise SourcePackError("source_unavailable", f"provider returned HTTP {status}")
            if status >= 400:
                raise SourcePackError("schema_drift", f"request returned HTTP {status}")
            return status, raw, final_url, "fixture" if response.get("origin") == "fixture" else "live"
        raise SourcePackError("rate_limited", "provider quota is temporarily exhausted")  # pragma: no cover

    @staticmethod
    def _json(raw: bytes) -> Any:
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourcePackError("schema_drift", "response is not JSON") from exc

    # -------------------------------------------------------------- Hub

    def _organisation(self, namespace: str) -> None:
        if namespace not in self._organisations:
            status, _raw, _final, _origin = self._get(
                f"https://huggingface.co/api/organizations/{quote(namespace)}/overview", allow=frozenset({404}))
            self._organisations[namespace] = status == 200
        if not self._organisations[namespace]:
            raise SourcePackError("user_namespace", f"{namespace!r} is not an organisation namespace; user namespaces "
                                                    "are refused and nothing of the repository is acquired")

    def _hub_unit(self, unit: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        kind, repo_id = unit["kind"], unit["repo_id"]
        self._organisation(repo_id.split("/", 1)[0])
        api = f"https://huggingface.co/api/{kind}s/{repo_id}"
        status, raw, final_url, origin = self._get(api, allow=frozenset({404}))
        info: dict[str, Any] = {"origin": origin, "responses": []}
        final_path = urlsplit(final_url).path.rstrip("/")
        if final_path != urlsplit(api).path.rstrip("/"):
            target = final_path.split(f"/api/{kind}s/", 1)[-1]
            return [hub_state_statement(kind, repo_id, "renamed", {
                "renamed_to": target, "basis": "the source redirected the repository id (same host); the target is "
                                               "recorded and not acquired"})], info
        if status == 404:
            return [hub_state_statement(kind, repo_id, "removed_by_source", {
                "http_status": 404, "basis": "the repository answers 404; earlier revisions stay in its history"})], info
        meta = self._json(raw)
        info["responses"].append(sha256_bytes(raw))
        gated, disabled = meta.get("gated"), meta.get("disabled")
        if gated not in (None, False, "false") or disabled is True:
            # Never unlocked, token or not: a gated or disabled repository is not read further.
            return [hub_state_statement(kind, repo_id, "withdrawn", {
                "gated": gated if gated not in (None, False, "false") else False, "disabled": bool(disabled),
                "basis": "the source states the repository gated or disabled; not read further"})], info
        current = str(meta.get("sha") or "")
        _status, refs_raw, _final, _origin = self._get(f"{api}/refs")
        statements = [parse_hub_refs(self._json(refs_raw), kind=kind, repo_id=repo_id)]
        shas = list(dict.fromkeys([*unit["revisions"], *([current] if current else [])]))
        for sha in shas:
            if sha == current:
                revision = meta
            else:
                _s, rev_raw, _f, _o = self._get(f"{api}/revision/{sha}")
                revision = self._json(rev_raw)
                if str(revision.get("sha")) != sha:
                    raise SourcePackError("schema_drift", "the Hub answered for another revision")
            card, card_status = None, "none"
            try:
                card_code, card_raw, _f, _o = self._get(hub_card_url(kind, repo_id, sha), accept="text/plain",
                                                        allow=frozenset({404}))
                if card_code != 404:
                    if len(card_raw) > CAPS["huggingface-hub"]["card_bytes"]:
                        card_status = "over_cap"
                    else:
                        card, card_status = split_card(card_raw), "stated"
            except SourcePackError as exc:
                if exc.code != "response_too_large":
                    raise
                card_status = "over_cap"
            except AiModelsFormatError as exc:
                raise SourcePackError("schema_drift", f"{exc.code}: {exc}") from exc
            try:
                statements += parse_hub_revision(revision, kind=kind, repo_id=repo_id, card=card,
                                                 card_status=card_status)
            except AiModelsFormatError as exc:
                raise SourcePackError("schema_drift", f"{exc.code}: {exc}") from exc
        return statements, info

    # -------------------------------------------------------------- OpenML

    def _openml_unit(self, unit: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        base = "https://www.openml.org/api/v1/json"
        if unit["unit"] == "dataset":
            dataset_id = int(unit["dataset_id"])
            _s, raw, _f, origin = self._get(f"{base}/data/{dataset_id}")
            _s, qualities, _f, _o = self._get(f"{base}/data/qualities/{dataset_id}")
            return [parse_openml_dataset(self._json(raw), self._json(qualities), dataset_id=dataset_id)], {
                "origin": origin, "responses": [sha256_bytes(raw), sha256_bytes(qualities)]}
        task_id = int(unit["task_id"])
        _s, raw, _f, origin = self._get(f"{base}/task/{task_id}")
        statements = [parse_openml_task(self._json(raw), task_id=task_id)]
        responses = [sha256_bytes(raw)]
        for measure in unit["measures"]:
            limit = int(unit["limit_per_measure"])
            status, rows, _f, _o = self._get(f"{base}/evaluation/list/task/{task_id}/function/{measure}/limit/{limit}",
                                             allow=frozenset({412}))
            responses.append(sha256_bytes(rows))
            if status == 412:
                # OpenML answers 412 with error code 372 when a list has no results (verify); anything else fails.
                code = str(dict(dict(self._json(rows) if rows else {}).get("error") or {}).get("code") or "")
                if code != "372":
                    raise SourcePackError("schema_drift", "OpenML refused the evaluation list (HTTP 412)")
                payload: Any = {"evaluations": {"evaluation": []}}
            else:
                payload = self._json(rows)
            statements += parse_openml_evaluations(payload, task_id=task_id, measure=measure, limit=limit)
        return statements, {"origin": origin, "responses": responses}

    # -------------------------------------------------------------- Epoch

    def _epoch_unit(self, unit: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        stamp = None
        if unit.get("page"):
            status, page, _f, _o = self._get(str(unit["page"]), accept="text/html", allow=frozenset({404}))
            stamp = last_updated_stamp(page) if status == 200 else None
        _s, raw, _f, origin = self._get(str(unit["file"]), accept="text/csv")
        if len(raw) > CAPS["epoch-ai"]["bytes"]:
            raise SourcePackError("response_too_large", "the Epoch file exceeds its 20 MB budget")
        statements = parse_epoch_csv(raw, models=unit["models"], file_url=str(unit["file"]))
        return statements, {"origin": origin, "responses": [sha256_bytes(raw)], "vintage": {
            "file_sha256": sha256_bytes(raw), "last_updated": stamp,
            "release_basis": "page_last_updated" if stamp else "retrieval_time"}}

    # -------------------------------------------------------------- pages

    @staticmethod
    def unit_key(provider: str, unit: Mapping[str, Any]) -> str:
        if provider == "huggingface-hub":
            return f"hub:{unit['kind']}:{unit['repo_id']}"
        if provider == "openml":
            return f"openml:{unit['unit']}:{unit.get('dataset_id') or unit.get('task_id')}"
        return f"epoch:{unit['file']}"

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        self._check(request)
        units = self.declared["units"]
        index = 0 if cursor is None else int(cursor) if str(cursor).isdigit() else -1
        if not 0 <= index < len(units):
            raise SourcePackError("cursor_drift", "cursor names no declared unit")
        unit = dict(units[index])
        before = self.requests
        try:
            if self.provider == "huggingface-hub":
                statements, info = self._hub_unit(unit)
            elif self.provider == "openml":
                statements, info = self._openml_unit(unit)
            else:
                statements, info = self._epoch_unit(unit)
        except AiModelsFormatError as exc:
            raise SourcePackError("budget_exhausted" if exc.code == "budget_exhausted" else "schema_drift",
                                  f"{exc.code}: {exc}") from exc
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(statements) > limit:
            # Never a truncated unit: a missing revision or row would read as one the source removed.
            raise SourcePackError("budget_exhausted", "unit has more statements than the run's result budget")
        unit_key = self.unit_key(self.provider, unit)
        header = {
            "contract": UNIT_CONTRACT, "provider": self.provider, "format": self.declared["format"],
            "unit_key": unit_key, "unit": unit, "evidence_origin": info.get("origin", "live"),
            "response_sha256": info.get("responses", []), "content_sha256": digest(statements),
            "statement_count": len(statements), "complete": True,
            "live_verification": LIVE_VERIFICATION[self.provider]["status"],
            **({"vintage": info["vintage"]} if info.get("vintage") else {}),
        }
        records = [{
            "id": f"{digest([unit_key, header['content_sha256']])[:16]}:{number}",
            "title": f"{unit_key} ({statement['record_type']})", "url": statement.get("url"), "language": "en",
            "published_at": None, "content": canonical(statement), "ai_statement": statement, "ai_unit": header,
        } for number, statement in enumerate(statements)]
        receipt = {"status": 200, "provider": self.provider, "unit": unit_key, "statements": len(records),
                   "requests": self.requests - before, "retries": self.retries, "keyed": bool(self.secret),
                   "evidence_origin": header["evidence_origin"], "final_page": index + 1 >= len(units)}
        next_cursor = str(index + 1) if index + 1 < len(units) else None
        return RuntimePage(tuple(records), next_cursor, sum(len(r["content"]) for r in records), receipt=receipt)


ADAPTERS = {CONNECTOR: AiModelsAdapter}


def fixture_key(url: str, params: Any) -> str:
    pairs = list(params.items()) if isinstance(params, Mapping) else list(params or [])
    query = urlencode(sorted((str(k), str(v)) for k, v in pairs))
    parts = urlsplit(url)
    return f"{parts.hostname}{parts.path}" + ("?" + query if query else "")


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay authored responses keyed by host, path and sorted query; the token header is never part of a key."""
    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del headers, timeout
        key = fixture_key(url, params)
        page = by_key.get(key)
        if page is None:
            raise SourcePackError("fixture_missing", f"no native page for {key}")
        body = page.get("body")
        content = body.encode() if isinstance(body, str) else b"" if body is None else json.dumps(body).encode()
        return {"status": int(page.get("status", 200)), "headers": dict(page.get("headers") or {}),
                "content": content, "origin": "fixture",
                **({"final_url": page["final_url"]} if page.get("final_url") else {})}

    return transport


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = AiModelsAdapter(source, transport=fixture_transport(list(fixture["native_pages"])),
                              secret=FIXTURE_SECRET)
    records, cursor = [], None
    while True:
        page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {},
                                   "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
        records += [dict(item) for item in page.records]
        cursor = page.next_cursor
        if cursor is None:
            return records


__all__ = [
    "ADAPTERS",
    "BOUNDED_COVERAGE",
    "CAPS",
    "CONNECTOR",
    "DOCUMENTED_NOT_ACQUIRED",
    "EXCLUSIONS",
    "FIXTURE_SECRET",
    "FORMATS",
    "LIVE_VERIFICATION",
    "MINIMISATION",
    "NEVER_SENTENCE",
    "PACING_S",
    "PROVIDERS",
    "PROVIDER_CONTRACTS",
    "SECRET_REF",
    "AiModelsAdapter",
    "AiModelsFormatError",
    "ai_declaration",
    "fixture_key",
    "fixture_transport",
    "parse_epoch_csv",
    "parse_hub_refs",
    "parse_hub_revision",
    "parse_openml_dataset",
    "parse_openml_evaluations",
    "parse_openml_task",
    "replay_native_fixture",
    "split_card",
    "stated_identifiers",
    "unverified",
]
