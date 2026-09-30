# Fact-checks: source-contract audit, data minimisation and bounded coverage (FC01)

Tracking: #2659 · delivery issue #2664 · recorded 2026-09-30.

This audit sets out, per source, what the News pack's fact-checks provider
(`news.fact-checks`, optional features `fact-checks-google`,
`fact-checks-datacommons` and `fact-checks-ifcn`) may acquire, how, on what
terms, and how personal data in fact-check markup is minimised.

**Terms were not re-verified live.** The documentation hosts
(`developers.google.com`, `datacommons.org`, `ifcncodeofprinciples.poynter.org`)
were unreachable from the runtime that wrote this audit (the egress proxy
blocked them on 2026-09-30). Endpoints, fields and terms come from the
providers' published documentation as the author knows it and from the tracker's
references. **Every item marked _verify_ must be checked against the live
documentation, the live terms and a real response before the first dated live
run (FC13, #2722). No source is `verified-live` until that run exists.**

The machine-readable copy of these decisions is `PROVIDER_CONTRACTS`,
`LIVE_VERIFICATION`, `BOUNDED_COVERAGE` and `MINIMISATION` in
`src/ingestion/fact_checks_sources.py`. Each source entry in
`config/source_packs/osint.json` (`bounded-public-osint` 1.2.0) states
`fact_checks.live_verification: unverified-live`, and the MCP tool
`fact_check_source_contracts` returns the same decisions.

Non-goals for every source: **no truth verdicts by Noesis, no rating
normalisation presented as the publisher's, no automatic claim matching without
review, and no scraping beyond each publisher's terms.** Ratings are kept as each
publisher published them (text, and any numeric value with its scale). Claim
matches, claimant matches and publisher-to-source matches are reviewable
assertions only.

An older module, `src/argument_mining/factcheck.py` (#97), calls the same Google
API and maps `textualRating` onto four verdict values stored on
`argument_claims`. The fact-checks provider does **not** use or extend that
mapping; its records never carry a normalised verdict, and the store refuses one.

## Access decisions

| Source (source-pack id) | Provider | Delivers | Decision | Reason |
| --- | --- | --- | --- | --- |
| `news-fact-checks-google` | `google-fact-check-tools` | ClaimReview records for declared searches | implemented, `unverified-live` | documented JSON API; API key required |
| `news-fact-checks-datacommons` | `datacommons-claimreview` | ClaimReview releases (vintages) | implemented, `unverified-live` | static JSON-LD feed; licence to verify |
| `news-fact-checks-ifcn` | `ifcn-signatories` | publisher (signatory) status history | implemented, `unverified-live` | no API or export; listing page only, terms to verify |

No source is recorded as "not implemented": each has a documented, bounded
access path. The IFCN source is the most fragile. If the live terms do not
allow reading the listing, the source stays declared but is not run. Publisher
status is then reported as `unavailable`, never inferred (see the IFCN section).

## Per-source contract

### Google Fact Check Tools API (`claims:search`)

- **Endpoint:** `GET https://factchecktools.googleapis.com/v1alpha1/claims:search`
  with `query`, `reviewPublisherSiteFilter`, `languageCode`, `maxAgeDays`,
  `pageSize` and `pageToken`. A search names a query, a publisher site, or both.
  `claims:imageSearch` and the ClaimReview markup-management `pages` endpoints
  are out of scope.
- **Response:** `claims[]` with `text` (the claim as quoted), `claimant` (a
  string), `claimDate`, and `claimReview[]` with `publisher.name`,
  `publisher.site`, `url`, `title`, `reviewDate`, `textualRating` and
  `languageCode`; `nextPageToken` for paging. The API returns no numeric rating
  and no claimant identifier.
- **Authentication and key handling:** a Google Cloud API key, declared as the
  required secret `NOESIS_GOOGLE_FACTCHECK_API_KEY`, is sent as the
  `X-Goog-Api-Key` header. It never goes in the `key` query parameter, a URL, a
  receipt or a record. Restrict the key to this API in the Cloud console. The
  adapter refuses to run without the key (`authentication_failed`); it never
  silently returns nothing.
- **Licence and redistribution:** the Google APIs Terms of Service apply
  (_verify_). The ClaimReview content is each publisher's own markup. Records
  keep short attributed fields (the publisher, the review URL and title, the
  claim as quoted, the rating) with a link to the review, and never mirror the
  review article.
- **Rate limits:** per-project quota set in the Google Cloud console (_verify_
  the default). HTTP 429 with `Retry-After` is reported as `rate_limited`.
- **Updates, corrections, removals:** there is no revision stamp. The API
  returns the current markup, so a changed `reviewDate`, title, rating or claim
  is a **new revision** of the record keyed by publisher, review URL and reviewed
  claim. One review page may review several claims, so the claim is part of the
  key, and `review_key` groups the claims of one page. Search results are not a
  complete listing, so a review missing from a later search is **not** a removal.

### Data Commons ClaimReview data feed

- **Endpoint:** `https://storage.googleapis.com/datacommons-feeds/claimreview/latest/data.json`
  (the release label `latest`, or a dated or labelled release path, is declared
  per unit; _verify_ the historical-release paths).
- **Format:** a schema.org `DataFeed` whose `dataFeedElement[].item[]` are
  `ClaimReview` objects: `url`, `claimReviewed`, `datePublished`, `author`
  (publishing organisation, with `url`), `reviewRating` (`alternateName`,
  `ratingValue`, `bestRating`, `worstRating`), `itemReviewed` (`Claim` with
  `author` (the claimant), `datePublished`, `appearance[]` and
  `firstAppearance`) and `inLanguage`.
- **Authentication:** none.
- **Licence and redistribution:** Data Commons terms of use, with attribution to
  each fact-checking publisher. _Verify_ whether the feed carries CC BY 4.0 or
  another licence before any redistribution; until then the records are kept for
  local research use only, and exports cite the publisher and the review URL.
- **Rate limits:** a static file, fetched once per declared release. The whole
  file must fit the source's `max_bytes` budget (50 MB), or the unit fails as
  `response_too_large` (_verify_ the current file size).
- **Updates, corrections, removals:** each release is a **vintage**, labelled by
  its `dateModified` (else the declared release label and the response digest).
  A review whose content changes is a revision. Republishing the same content in
  a later vintage adds nothing; the run's receipt records the vintage. A release
  is complete within its declared scope (publisher sites and review-date
  window), so a review that a later release no longer carries becomes an
  `absent-from-release` revision, never a deletion.
- **Duplicates with the API:** the same review (publisher, review URL and claim)
  from the API and from a release has the same record key. Each source keeps
  its own provenance chain, and answers list both.

### IFCN Code of Principles signatory list

- **Endpoint:** `https://ifcncodeofprinciples.poynter.org/signatories`. Only
  this one listing page is read. Profile pages, assessment reports and named
  staff are never read.
- **Format:** there is no documented API or bulk export. The parser reads one
  block per signatory: the organisation name, country, website, the status
  label as published, the status dates and the profile path. The listing markup
  in the fixtures is **assumed** and must be verified (_verify_).
- **Authentication:** none.
- **Licence and redistribution:** Poynter's website terms of use (_verify_ that
  reading the listing at most daily and citing the status facts with a link is
  permitted). If it is not, the source is not run and publisher status is
  reported as `unavailable`.
- **Rate limits:** undocumented. One request per run, scheduled at most daily.
- **Updates, corrections, removals:** the listing shows only the current status.
  Each acquisition is dated. A changed status (verified, expired, under review)
  or status date is a **dated revision**, in force from the date the listing
  states (the verification date, the expiry date or the review date). A
  signatory missing from a later listing becomes an `absent-from-listing`
  revision. Each publisher is keyed by its website domain, and links to source
  identities only as a reviewable assertion by that domain (FC06).

## Data-minimisation decision (binding for every fact-checks module)

Fact-check markup names people: claimants (politicians, officials and sometimes
private individuals or social-media accounts), the authors of reviews, and the
authors of the posts where a claim appeared. The decision:

- **Stored:** the publisher organisation (name and site as published), the review
  URL, title, date and language, the claim text as the publisher quoted it, the
  claimant's name and published `@type` as the publisher named them, claimant
  identifier URLs from Wikidata and ROR only, the claim date, appearance URLs on
  news and web hosts, the rating text, value and scale verbatim, and IFCN
  signatory organisation, website, country, status and status dates.
- **Redacted:** appearance and first-appearance URLs on social platforms (a
  closed host list in `SOCIAL_HOSTS`) are stored as the platform host plus the
  SHA-256 of the `wa-canon-v1` canonical URL. The record never holds the URL
  or the account handle. A caller who already holds the URL can still find the
  fact-checks that cite it (FC09).
- **Never stored:** a claimant's image, job title, contact details (address,
  email, telephone), birth date or social-media profile URLs (`sameAs` outside
  the identifier hosts), a review's individual author (a `Person` author of a
  `ClaimReview`), IFCN profile pages and named staff, and review article bodies.
  The parser drops these before any record, document or receipt exists and lists
  them under `minimisation.withheld`. The store refuses any record that still
  carries one of them (`minimisation_violation`).
- **Matching:** claimants are matched to canonical entities only as reviewable
  assertions, published identifier first (a Wikidata id on the claimant), then
  the name as published. Claimant accounts and social-platform appearances are
  never matched or expanded.
- **Retention:** records are retained with each revision. No personal identifier
  beyond the name as published is stored. A publisher's removal is a revision
  that stops the record being current; nothing is purged automatically in the
  first coverage.
- **Who may query:** holders of `knowledge:news:fact-checks:read` plus namespace
  read access. Identity review needs `knowledge:news:fact-checks:review`. MCP
  outputs are built from minimised records only, so no tool can return a
  withheld field.

## Bounded first coverage

| Source | Bound | Justification |
| --- | --- | --- |
| `news-fact-checks-google` | at most 20 declared searches, 4 pages of 50 claims each (a longer search is `budget_exhausted`, never truncated) | searches are topical by nature; a hard page bound keeps a run small and quota-safe |
| `news-fact-checks-datacommons` | at most 3 declared releases, each filtered to 1-20 publisher sites and a review-date window of at most 366 days; at most 2000 reviews per release | the release file is global; filtering to named publishers and a window keeps the scope explicit and complete within itself, so absences are meaningful |
| `news-fact-checks-ifcn` | the one listing page, at most 500 signatories, at most daily | the list is small and the status is the only fact needed |
| links | news articles, OSINT corroboration and claim timelines only by URL citation or accepted matches | no inferred links |

The declared selections in the source pack are placeholders (fictional
publishers under `.example`) until the FC13 live run replaces them with real
publishers and searches. Nothing implies complete coverage of any publisher,
language or topic.

## LIVE_VERIFICATION

| Provider | Status | What is outstanding |
| --- | --- | --- |
| `google-fact-check-tools` | `unverified-live` | quota, header-key support, current terms, a dated bounded run |
| `datacommons-claimreview` | `unverified-live` | release path, `dateModified`, licence, file size, a dated bounded run |
| `ifcn-signatories` | `unverified-live` | listing markup, status labels, terms, a dated bounded run |

Offline evidence (fixture replays) and live evidence are reported separately.
Offline coverage is never reported as live coverage.
