# Archive and discovery acquisition

These adapters use the existing binary snapshot and document stores. They are
caller-driven Python APIs; no new scheduler, hosted service, or paid default is
enabled. A DuckDB connection has one writer. Install HTML extraction with
`pip install '.[ingestion]'` and archive exchange with `pip install '.[archives]'`.

## Search candidates (#1359)

`BraveDiscovery.discover_for_project` already reserves request and USD ceilings
through the persistent research-project ledger. Its returned candidates can now
enter ordinary acquisition with `src.ingestion.discovery_acquisition.acquire_candidates`:

```python
from src.ingestion.discovery_acquisition import acquire_candidates

receipt = acquire_candidates(
    conn, candidates, acquisition_id="berlin-research-2026-09-06",
    allowed_hosts=["www.berlin.de"], language="de",
    max_candidates=20, max_bytes=2_000_000, timeout_s=15,
)
```

`candidates` is an iterable of the existing `SourceRef` objects, including those
returned by `BraveDiscovery.discover`. Use `language="de", country="DE"` for
German discovery. Language on acquired documents is explicitly caller-declared;
the search language is not evidence that a page is German. Only configured
public HTTPS hosts are fetched. Cross-host redirects fail. Each candidate makes
one bounded GET; a batch admits at most 200 candidates. Canonical URL duplicates
are skipped. The shared extractor supplies document content; snippets and search
titles never substitute for missing page content. Raw bytes and extractor
provenance are retained. Unknown publication dates remain unknown.

Acquisition IDs bind ordered inputs and limits. Reopening the database and
repeating an ID replays its stored outcomes without HTTP. Changed inputs under
an existing ID fail. To retry a failed candidate or observe a page update, use a
new ID. A process crash before the final receipt may repeat a fetch; this is not
an exactly-once network guarantee. Upstream Brave reservations remain separate
from these direct page GETs; paid extraction services are not called.

## OpenReview (#1477)

Install and enable `config/source_packs/openreview.json` using the existing
source-pack interface, and accept its per-note license policy. Run operation
`search` with an explicit `forum`, `id`, `invitation`, `replyto`, or `domain`
parameter. The adapter uses API v2 `/notes`, bounded `limit`/`after` pagination,
and `id:asc` ordering. The normal source runtime owns durable page checkpoints,
timeouts, byte/results/page budgets, document revisions, and replay.

Only explicitly public notes and public content fields are admitted. No token
is sent by this public-only adapter. Anonymous signatures remain attribution;
no identity is inferred. Submission/review/rebuttal/decision types are derived
from explicit invitation names; unfamiliar schemas remain `unknown`. Original
invitations, forum, direct reply parent, public content, license, signatures and
provider timestamps remain in `source_pack_native_json`. Missing or inaccessible
notes are not evidence of withdrawal. `pdate` supplies publication time when
present; creation/modification times are not silently substituted for it.

An `after` scan is not a frozen provider snapshot. Start a new bounded run to
discover changes to already visited notes; Noesis retains observed revisions,
not an unobserved complete provider edit history. The native fixture exercises
edits, replies, private fields, unavailable responses and durable restart/replay.
The live probe on 2026-09-05 returned HTTP 403 `ChallengeRequiredError`; live
availability is **unverified**, and the challenge was not bypassed.

Protocol references: [OpenReview notes](https://docs.openreview.net/getting-started/objects-in-openreview/introduction-to-notes)
and [official Python API client](https://github.com/openreview/openreview-py/blob/master/openreview/api/client.py).
No optional OpenReview package is required. API v2 and Noesis mapping v1 are the
recorded protocol versions. Per-note licenses govern redistribution.

## WARC/ARC exchange (#1497)

```python
from src.ingestion.warc_io import import_archive, export_archive

with open("captures.warc.gz", "rb") as stream:
    receipt = import_archive(conn, stream, archive_id="berlin-captures-v1")
data = export_archive(conn, [item["record_id"] for item in receipt["captures"]])
```

The optional, pinned Apache-2.0 `warcio==1.8.1` dependency reads WARC and legacy
ARC streams; export writes WARC 1.1. Defaults are 100 records, 2 MB per record,
and 20 MB of both compressed input and decompressed archive bytes. Import stages
only the bounded capture set and validates the whole archive before committing
captures and its hash-bound receipt atomically. Supplied WARC digests, truncated
records, gzip trailers, identity conflicts and size limits are checked. Archive
IDs cannot be reused for changed bytes.

Raw HTTP entity bytes and duplicate HTTP headers are retained alongside capture
timestamps, original URLs, WARC headers and SHA-256 blob identities. HTTP content
and transfer encoding remain as captured; archive gzip decompression is separate
from HTTP-body decoding. This prevents silent byte changes in evidence exchange.
The importer does not extract or execute captured HTML. ARC identities are
deterministic because legacy captures lack native WARC record IDs.

An identical-payload revisit must refer to a previously imported record ID;
missing bases, unsupported profiles and mismatched supplied payload digests fail.
Repeated content shares a binary blob, while capture observations remain distinct.
Import order matters for revisits; forward references and digest-only revisit
resolution are not supported. Export materializes selected revisits as response
records, preserves HTTP payload/header fidelity and capture dates, and regenerates
WARC framing. It does not reproduce the original archive bytes or all custom WARC
headers. Portable Noesis research-package verification is unchanged.

Reference: [warcio documentation](https://github.com/webrecorder/warcio).

## Wayback availability and capture acquisition (#1491)

```python
from src.ingestion.wayback import acquire_wayback

receipt = acquire_wayback(
    conn, "https://www.berlin.de/example-historical-page",
    timestamp="2024", request_id="berlin-history-v1", language="de",
)
```

The read-only [Availability API](https://archive.org/help/wayback_api.php) returns
the closest accessible capture to the requested timestamp. This is not exhaustive
history. Each invocation makes at most two GETs: a 100 KB discovery response and
a capture bounded to 2 MB by default, with 15 seconds per GET. There are no
automatic retries or requests to create new captures. A new request ID permits
an explicit retry; completed IDs replay locally after database restart.

Receipts distinguish `acquired`, `unavailable`, `redirected_capture` and `failed`.
Redirected or different-original captures require review and are not silently
stored as the requested page. Captured time, retrieval time and original URL
remain separate. Original publication dates stay unknown. HTML is the archived
replay response and may contain archive presentation elements; it is not claimed
to be the original server's exact wire response. Use WARC when that fidelity is
required. Source identity stays tied to the original URL, with a separate
document identity for each capture, so historical observations do not overwrite
live documents. License and archive coverage remain those of the original
material and provider. No live availability result is claimed from fixture tests.

## Wikidata statement evidence (#1485)

`src.ingestion.wikidata.acquire_entity(conn, "Q64", properties=["P17", "P31"],
request_id="berlin-identifiers-v1")` acquires a bounded property selection from
the documented entity JSON interface. Defaults select German/English labels and
aliases. Optional `revision` pins an explicit Wikidata revision; mismatches fail.
No API key or additional package is required. The request sends a descriptive
User-Agent. Source data is CC0; the adapter retains attribution to Wikidata.

One GET is permitted per invocation: at most 5 MB by default, 15 seconds, 20
selected properties, and 100 selected statements. Bounds can be explicitly
raised within the adapter's ceilings. Exceeding the statement bound fails rather
than truncating evidence. HTTP failures are explicit and have no automatic retry;
operators must respect provider Retry-After before submitting another request.
The provider returns the complete entity JSON, which is snapshotted before the
selected statements become a normal revisioned document. Missing entities produce
an unavailable receipt and no document.

Statement IDs (including the provider's mixed-case prefixes), ranks, unknown/no
values, temporal qualifiers and references survive normalization. Unreferenced
statements are labelled. Deprecated and conflicting statements remain evidence;
they are not silently discarded in favor of preferred values. Optional
`candidate_entity_ids` are retained as unreviewed/ambiguous local matches.
Acquisition never changes canonical entity properties or executes identity
merges. The existing review flow remains responsible for such decisions.

A live Berlin Q64 check acquired 24 statements at revision `2539733841`; its hash,
timestamp and initial failed observation are recorded in
`docs/development/workflow-implementation-evidence/wikidata-berlin-live.json`.
The failed observation identified mixed-case statement IDs; the repaired adapter
passed both native acquisition and its regression fixture. This is a successful
bounded protocol check, not independent validation of those statements' truth.
See [Wikidata data access](https://www.wikidata.org/wiki/Wikidata:Data_access).

## Reproduction

```sh
python -m pytest tests/unit/ingestion/test_openreview_api.py \
  tests/unit/ingestion/test_discovery_acquisition.py \
  tests/unit/ingestion/test_brave_discovery.py \
  tests/unit/ingestion/test_warc_io.py \
  tests/unit/ingestion/test_wayback.py \
  tests/unit/ingestion/test_wikidata.py -q --override-ini addopts=''
```

These authored German/English fixtures validate contracts and storage behavior;
they are not an independent human quality benchmark or a credentialed provider
comparison.
