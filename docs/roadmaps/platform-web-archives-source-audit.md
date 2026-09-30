# Platform web archives: Memento archive contract and access audit (WA01, #2240)

Parent: #2226. Extends the Internet Archive acquisition in `src/ingestion/wayback.py`
(#1491), the Common Crawl collection in `src/ingestion/common_crawl.py` and the citation
preservation records in `src/kb/citation_preservation.py`. No new store and no bulk
mirroring of any archive. The machine-readable form of every decision below is
`ARCHIVES`, `BOUNDED_COVERAGE`, `SAVE_PAGE_NOW` and `LIVE_VERIFICATION` in
`src/ingestion/memento.py`; this document and those constants must agree
(`tests/unit/ingestion/test_memento.py` checks the archive ids and decisions).

References: RFC 7089 (HTTP Framework for Time-Based Access to Resource States,
Memento), the Time Travel API guide (<https://timetravel.mementoweb.org/guide/api/>) and
the Wayback Machine APIs (<https://archive.org/help/wayback_api.php>).

## Status of the evidence

Everything here was recorded from the published documentation of each service. No live
request was made while writing it. Every archive is `unverified-live` in
`LIVE_VERIFICATION` until the bounded live run in WA15 (#2348) confirms endpoints, response
formats and access conditions against the verification URL set below. Offline tests replay
synthetic fixtures under `tests/fixtures/web_archives/` that follow the published formats.
They are not recorded responses.

## Vocabulary

- **URI-R**: the original resource (the cited URL). **URI-M**: one memento (an archived
  capture). **TimeGate**: datetime negotiation (`Accept-Datetime` request header; `302` to
  a URI-M plus a `Link` header). **TimeMap**: the list of mementos for a URI-R
  (`application/link-format`, or JSON where offered).
- **Resolver** vs **archive**: the Time Travel aggregator resolves mementos held by other
  archives. Noesis records the aggregator as the *resolver* and the archive that holds
  the memento (identified by the URI-M host) as the *archive*. It never records the
  aggregator as the archive.
- **Crawl corpus**: Common Crawl publishes WARC files with a CDX index. It is not a Memento
  archive and never answers a TimeGate question.

## Archive contracts

| Archive id | Kind | TimeGate | TimeMap | Formats | Digest | Rate limit / terms | Access decision |
|---|---|---|---|---|---|---|---|
| `timetravel` | Memento aggregator (LANL / ODU Time Travel) | `https://timetravel.mementoweb.org/timegate/{URI-R}` | `https://timetravel.mementoweb.org/timemap/link/{URI-R}`, `/timemap/json/{URI-R}`; datetime API `/api/json/{YYYYMMDDhhmmss}/{URI-R}` | link-format, JSON | none (the aggregator does not publish content digests) | no published hard limit; the guide asks for moderate use. Noesis: 1 request/s, at most 2 requests per resolution | **in scope** (resolver only) |
| `internet-archive` | Memento archive | `https://web.archive.org/web/{URI-R}` with `Accept-Datetime`, or `/web/{YYYYMMDDhhmmss}/{URI-R}` | `https://web.archive.org/web/timemap/link/{URI-R}`, `/web/timemap/json/{URI-R}` | link-format, JSON (CDX-style rows) | CDX `digest` field: SHA-1 of the payload, base32, as **published** | archive.org Terms of Use; CDX and TimeMap throttle heavy clients (HTTP 429). Noesis: at most 3 requests per resolution (TimeMap, CDX, optional TimeGate), 15 per minute | **in scope** |
| `uk-web-archive` | national Memento archive (British Library and the UK legal deposit libraries) | `https://www.webarchive.org.uk/wayback/archive/{URI-R}` with `Accept-Datetime` | `https://www.webarchive.org.uk/wayback/archive/timemap/link/{URI-R}` | link-format | none published through Memento | UKWA terms of use. Most legal-deposit captures can only be viewed in the reading rooms of the legal deposit libraries. Only the open-access subset replays online. Noesis: at most 2 requests per resolution, 6 per minute | **in scope**, metadata only; reading-room captures recorded with `access_condition = reading-room-only` and never fetched |
| `arquivo-pt` | national Memento archive (Portugal, FCT) | `https://arquivo.pt/wayback/{URI-R}` with `Accept-Datetime` | `https://arquivo.pt/wayback/timemap/link/{URI-R}` | link-format (CDX JSON also published) | CDX `digest` (not used; TimeMap only) | open API with published terms; Noesis: at most 2 requests per resolution, 6 per minute | **in scope** |
| `library-of-congress` | national Memento archive (US LoC web archives) | `https://webarchive.loc.gov/all/{URI-R}` | `https://webarchive.loc.gov/all/timemap/link/{URI-R}` | link-format | none published through Memento | many collections carry a one-year embargo and permissions-based access. The access terms need review before use | **deferred** (reported as `deferred_by_access_decision`) |
| `vefsafn-is` | national Memento archive (Iceland) | `https://vefsafn.is/is/{URI-R}` | `https://vefsafn.is/timemap/link/{URI-R}` (per the aggregator's archive list) | link-format | none | terms not reviewed | **deferred** |
| `bnf` | national legal-deposit archive (France) | none public | none public | none | none | legal-deposit captures are consultable on site only | **excluded** (no public Memento endpoint; reading-room only) |
| `archive-today` | on-demand capture service (archive.today / archive.ph and mirrors) | `https://archive.ph/timegate/{URI-R}` | `https://archive.ph/timemap/{URI-R}` | link-format | none | no published API terms. The service applies CAPTCHA and anti-automation controls, rate-limits aggressively and does not state that it honours publisher robots/exclusion requests | **excluded** (see below) |
| `common-crawl` | crawl corpus (not Memento) | none | none; CDX index `https://index.commoncrawl.org/{CRAWL}-index?url={URI-R}&output=json` | CDX JSON lines; WARC records by byte range from `https://data.commoncrawl.org/` | CDX `digest`: SHA-1 of the payload, base32, as **published**; verified by recomputation when a single WARC record is range-read | Common Crawl Terms of Use; index server throttles heavy clients (HTTP 503). Noesis: 1 index lookup per crawl, at most 2 crawls per resolution, WARC range reads only through the existing bounded `CommonCrawlCollection` | **in scope** as a crawl corpus |

### archive.today decision

The decision is **excluded**. Automated access would have to work around the service's
CAPTCHA and anti-automation controls, and the service does not publish a way to honour
publisher exclusion requests. Both conflict with the tracker's exclusions (no bypass of
robots, exclusion, paywalls or anti-automation controls). The decision is encoded as
`ARCHIVES["archive-today"]["access_decision"] = "excluded"`. Resolutions do not silently
omit archive.today. Every per-archive answer lists it as `excluded_by_access_decision`.
Mementos from its hosts that an aggregator returns are withheld and counted, not recorded.
If the decision is later changed to `in_scope`, the same code maps its TimeMap entries to
capture records. A CAPTCHA, HTTP 429 or blocked response then stops acquisition and is
recorded (`blocked_by_archive`). It is never retried.

### National archives

The UK Web Archive and Arquivo.pt are in scope. Their captures are listed through their
public Memento TimeMaps. A capture whose URI-M matches one of the archive's configured
reading-room patterns is recorded with `access_condition = reading-room-only` and is not
fetched. The reading-room URI patterns for UKWA are recorded from its public description
of the legal-deposit service and are `unverified-live`. WA15 must confirm them.
The Library of Congress and Vefsafn.is are deferred pending a terms review. BnF is excluded
because it has no public endpoint. Deferred and excluded archives are reported per archive
in every resolution. They are never silently dropped.

## Save Page Now (write operation)

Save Page Now (SPN2) is a **write** operation. It asks the Internet Archive to make a new
capture, so it is not a read of existing evidence.

- Endpoint: `POST https://web.archive.org/save` with form field `url`, `Accept:
  application/json` and `Authorization: LOW {access}:{secret}` (archive.org S3-style keys
  from the environment variables `NOESIS_IA_S3_ACCESS_KEY` and `NOESIS_IA_S3_SECRET_KEY`, never stored or
  echoed). The response names a `job_id`. Job status comes from `GET
  https://web.archive.org/save/status/{job_id}` (`pending`, `success` with `timestamp` and
  `original_url`, or `error` with `status_ext`).
- Terms: archive.org Terms of Use. The Internet Archive limits concurrent captures and daily
  captures per account and refuses robots-excluded or blocked URLs (`error:robots-txt`,
  `error:blocked-url`, `error:blocked`, `error:no-access`).
- Noesis controls: the dedicated scope `knowledge:citation:archive-request`, which
  read-only callers never hold. The optional `save-page-now` feature is off by default.
  The budget is 5 requests per namespace per UTC day and 1 status poll per call. There are
  no retries, no scheduled or bulk capture campaigns, and a refused URL is recorded as
  refused, never retried through another archive or proxy. While SPN is `unverified-live`, live requests (the real transport)
  are restricted to the verification URL set.

## Bounded coverage

| Budget | Value |
|---|---|
| Requests per resolution | aggregator 2, `internet-archive` 3, each national archive 2, `common-crawl` 1 per crawl (2 crawls) |
| Maximum TimeMap size | 2,000,000 bytes and 5,000 mementos per TimeMap. A larger TimeMap is recorded as `truncated` (partial), never paged further |
| Maximum capture records per resolution | 5,000 per archive |
| Save Page Now | 5 requests per namespace per UTC day, 1 status poll per call |
| Monitoring | one resolution plus one live-URL check and one check per pinned capture per monitor run |

Verification URL set (the only URLs used by the WA15 live run, and the only URLs Save Page
Now accepts on the real transport while it is `unverified-live`):

- `https://datatracker.ietf.org/doc/html/rfc7089`
- `https://timetravel.mementoweb.org/guide/api/`
- `https://www.gov.uk/government/organisations/hm-treasury`
- `https://arquivo.pt/`
- `https://example.com/`

## Robots, exclusion and paywalls

Noesis honours them **as published**. When an archive refuses or excludes a URL (Wayback
"Blocked Site Error", robots-based exclusion, SPN `error:robots-txt`), Noesis records the
refusal as `excluded_by_archive` or `refused` and stops. There is no fallback through
another archive, proxy or cache. Noesis does not fetch paywalled or reading-room content. It records only the archive's listing. No bypass path is planned.

## Records (WA02)

Capture, TimeMap snapshot, citation pin and capture match records are defined in
`contracts/schemas/jsonschema/noesis-web-archive-capture-v1.json`,
`noesis-web-archive-timemap-v1.json`, `noesis-citation-pin-v1.json` and
`noesis-web-archive-match-v1.json`. They are kept in the existing citation preservation
store.
