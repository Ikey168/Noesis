# Crossref correction and retraction acquisition

`CrossrefNoticeCollection` in `src/ingestion/crossref_notices.py` acquires REST v1
update records, including Retraction Watch attribution where supplied. It is an
explicit collection API, not a background process enabled by default.

```python
import duckdb
from src.ingestion.crossref_notices import CrossrefNoticeCollection

conn = duckdb.connect("research.duckdb")
collection = CrossrefNoticeCollection(
    conn, "updates-september-1", from_date="2026-09-01", until_date="2026-09-02",
    targets={"10.1234/paper": "existing-document-id"}, rows=20, max_pages=10,
)
while collection.inspect()["status"] == "running":
    collection.step()
```

The dates bound **index changes**, including changes to old notices. The query
uses `is-update:true`, `from-index-date` and `until-index-date`. Use successive
overlapping date windows and distinct collection IDs for incremental collection;
notice hashes deduplicate replay. The source is mutable, so an API cursor is not
a provider snapshot. Stored page bytes and parameters provide the reproducible
local evidence. Empty/short pages finish a window; a full final budgeted page
reports `bounded`, never complete coverage. Cursor expiry/error preserves the
last accepted page; start a new overlapping bounded window if needed.

Each request has a 15-second timeout, default 2 MB response limit and 20 records.
Maximum controls are 20 MB, 100 records/page and 100 requests per collection.
At most 100 update relations per work and 1000 explicit target mappings are
accepted. Failed requests consume the request budget; 429 and other errors are
surfaced without automatic retries. Page snapshots and receipts commit together.
The default cumulative response ceiling is 20 MB; database storage also includes
documents and provenance. No API key or paid Crossref service is required.

The notice DOI belongs to the returned work; `update-to.DOI` identifies the
affected paper. Types remain separate: correction (including erratum/corrigendum),
retraction, expression of concern and withdrawal. Unsupported types, missing
DOIs and unknown attribution remain `unresolved`. Dates retain native partial
date fields. Retraction Watch and publisher assertions retain distinct source
attribution. Retraction Watch coverage is stronger for retractions than other
notice types; absent notices do not certify an article.

Only an explicit target DOI-to-document mapping attaches a notice to local
evidence. A previously unbound notice can be attached in a later collection;
conflicting target remapping is rejected. A target needs a committed revision.
The original document payload, lifecycle and source generation remain unchanged.
The separate notice document and binary snapshot are cited by the integrity
ledger. Existing report dependency assessments return
`provider_notice_requires_review`; authors can use the existing proposal/review
flow. This is a provider assertion requiring review, not an automatic judgment
of every claim in the paper. Existing watch matching sees a new integrity finding
and deduplicates repeat deliveries. More than 100 notices on one report dependency
marks assessment coverage incomplete.

Eight tests cover all four types, missing fields, provenance, pagination, 429,
bounds, rollback, late target binding, database restart, historical report/source
preservation, report proposals and persisted watch-event replay. The combined
notice/report/watch regression passed **35 tests** on 2026-09-06.

```sh
.venv/bin/python -m pytest tests/unit/ingestion/test_crossref_notices.py tests/unit/kb/test_report_updates.py tests/unit/kb/test_watches.py -q --override-ini addopts=''
```

A live REST request for one update indexed on 2026-05-04 returned a publisher
correction, retaining 3124 bytes and both DOIs. See
[live evidence](../development/workflow-implementation-evidence/crossref-notices-live.json).
A separate live `update-type:retraction` query also returned a Retraction Watch
relation, but its transient response was not retained as a repository fixture.
The German contract fixtures are synthetic and are not human evaluation labels.

API semantics and data coverage:
[Retraction Watch documentation](https://www.crossref.org/documentation/retrieve-metadata/retraction-watch/),
[current filters](https://www.crossref.org/documentation/retrieve-metadata/rest-api/rest-api-filters/).
Metadata access does not grant rights to publisher article full text; this
adapter acquires metadata only. Attribution and native response fields are kept.
