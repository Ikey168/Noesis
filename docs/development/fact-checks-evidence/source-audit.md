# Fact-checks: source-contract audit, claimant data minimisation and bounded coverage (FC01)

Tracking: #2659 · delivery issue #2664 · recorded 2026-09-30.

This audit sets out, per source, what the News bundle's `news.fact-checks`
provider (features `fact-checks-google`, `fact-checks-datacommons`,
`fact-checks-ifcn`) may acquire, how, on what terms, and how personal data in
fact-checks is minimised.

**How the official pages were read.** On 2026-09-30 every official page named
below was requested with a web fetch from this runtime; each request was refused
by the network egress proxy (`EGRESS_BLOCKED` for `developers.google.com`,
`datacommons.org` and `ifcncodeofprinciples.poynter.org`). What is recorded
here therefore comes from (a) search-engine excerpts of those official pages
retrieved the same day, cited as "excerpt", and (b) the providers' documented
formats as the author knows them. **Every point marked _unverified_ must be
checked against the live page, the live terms and a real response before the
first dated live run (FC13, #2722). No source is `verified-live` until that run
exists.** Nothing below invents a licence term, endpoint or limit: where the
material read says nothing, this audit says so.

The machine-readable copy of these decisions is `PROVIDER_CONTRACTS`,
`LIVE_VERIFICATION`, `BOUNDED_COVERAGE` and `MINIMISATION` in
`src/ingestion/fact_checks_sources.py`; each source entry in
`config/source_packs/osint.json` (`bounded-public-osint` 1.2.0) states
`fact_checks.live_verification: unverified-live`, and the MCP tool
`fact_checks_source_contracts` returns the same decisions.

Non-goals for every source: **no truth verdict by Noesis, no rating
normalisation presented as the publisher's, no automatic claim matching without
review, and no scraping beyond each publisher's terms.** Ratings are kept as
each publisher published them, with their own scale; fact-check articles and
appearance pages are never fetched.

## Access decisions

| Source (source-pack id) | Provider | Delivers | Decision | Reason |
| --- | --- | --- | --- | --- |
| `fact-checks-google-claim-search` | Google Fact Check Tools API `v1alpha1/claims:search` | ClaimReview results of declared queries or review-publisher sites | `unverified-live` | Documented API with an API key; fixture-verified parser; header key handling and quota _unverified_ |
| `fact-checks-datacommons-feed` | Data Commons ClaimReview data feed (`DataFeed` of schema.org `ClaimReview`) | one release per unit, filtered to declared publisher sites and a review window | `unverified-live` | CC BY compilation (excerpt); feed path, size and cadence _unverified_ |
| (not acquired) | Data Commons historical research dataset | dated historical compilation | `documented-not-acquired` | Superseded for the bounded window by the feed; reserved for the FC13 cross-check |
| `fact-checks-ifcn-signatories` | IFCN Code of Principles signatories listing (HTML) | signatory name, profile, website, country, status label and date | `unverified-live`, **live fetch refused until terms are confirmed** | No API, download or reuse terms found; markup _unverified_ |
| (not used) | GitHub `IFCN/verified-signatories` (CC0-1.0) | a historical list | not used | The repository README points at the retired poynter.org code page; freshness _unverified_ (GitHub API access was not available to this runtime) |
| (not reused) | `src/argument_mining/factcheck.py` legacy lookup | first review attached to an argument claim with a normalised verdict | not reused | It normalises ratings into verdicts and attaches a review without review, both excluded here |

## Per-source contract

### Google Fact Check Tools API (Fact Checked Claim Search)

* **Endpoint.** `https://factchecktools.googleapis.com/v1alpha1/claims:search`
  (reference page `https://developers.google.com/fact-check/tools/api/reference/rest/v1alpha1/claims/search`,
  not fetchable, 2026-09-30). Query parameters per the reference excerpt:
  `query` (required unless `reviewPublisherSiteFilter` is given),
  `languageCode`, `reviewPublisherSiteFilter` ("e.g. nytimes.com"),
  `maxAgeDays` ("Age is determined by either claim date or review date, whichever
  is newer"), `pageSize`, `pageToken`, `offset`. Response fields used:
  `claims[].text`, `claimant`, `claimDate`, `claimReview[].publisher.name`,
  `publisher.site` (excerpt: "host-level site name, without the protocol or
  'www' prefix"), `url`, `title`, `reviewDate`, `textualRating`,
  `languageCode`, and `nextPageToken`.
* **Authentication and key handling.** A Google Cloud API key, the required
  secret `NOESIS_GOOGLE_FACTCHECK_API_KEY`, sent as the `X-Goog-Api-Key` header
  so it never appears in a URL, receipt, record or log (_unverified_ that the
  header form is accepted; the documented alternative is the `key` query
  parameter, which is not used). The legacy `GOOGLE_FACTCHECK_API_KEY` lookup in
  `src/argument_mining/factcheck.py` is not reused.
* **Licence and terms.** Excerpt of the API page (2026-09-30): "Use of the
  FactCheck Claim Search API is subject to Google's API Terms of Service." The
  Fact Check Tools API terms page `https://developers.google.com/fact-check/tools/api/terms`
  could not be fetched; storage duration, display and attribution conditions
  are _unverified_. Each fact-check belongs to its publisher; Noesis stores the
  ClaimReview metadata only and shows ratings as cited quotations with a link.
* **Rate limits.** None stated in the material read; per-project quota in the
  Google Cloud console (_unverified_). Bounded by at most 5 pages of 50 claims
  per declared unit; a longer result is `budget_exhausted`, never truncated.
* **Updates, corrections and removals.** The API publishes no revision stamp.
  `reviewDate` as published orders revisions; any change to a review (date,
  title, rating text, claims) is a new revision of the `(publisher site, review
  URL)` record. A result that stops appearing in a later search is **not** a
  removal (ranking, `maxAgeDays`), so the Google source never records absence.
* **Ratings.** `textualRating` verbatim; the API publishes no numeric rating.

### Data Commons ClaimReview feed

* **Endpoint.** The download page `https://datacommons.org/factcheck/download`
  (not fetchable, 2026-09-30) offers "a research dataset of historical fact
  checks and access to a data feed of fact check markups created via the Google
  Fact Check Markup Tool" and the ClaimReview Read/Write API (excerpt). The feed
  file path configured is
  `https://storage.googleapis.com/datacommons-feeds/claimreview/latest/data.json`
  (_unverified_; recorded from the author's knowledge of the page's link).
* **Format.** "All data is in DataFeed format and is updated on a frequent and
  regular basis" (excerpt): a schema.org `DataFeed` whose `dataFeedElement[]`
  items carry `ClaimReview` markup: `url`, `claimReviewed`, `author` (the
  publisher organisation, or a person), `datePublished`, `itemReviewed`
  (`Claim` with `author`, `datePublished`, `appearance[]`,
  `firstAppearance`), `reviewRating` (`ratingValue`, `bestRating`,
  `worstRating`, `alternateName`), `inLanguage`, `sdLicense`.
* **Authentication.** None.
* **Licence.** Excerpt of the download page (2026-09-30): "The compilation of
  the research dataset and the data feed from the Fact Check Markup Tool and the
  new ClaimReview Read/Write API are licensed under CC BY. The license on the
  structured data of each ClaimReview markup is specified in the field
  sdLicense. Additionally, each publisher may have their own license terms for
  content on their website." Noesis keeps each item's `sdLicense`, attributes
  Data Commons, and never fetches publishers' articles (_unverified_ until the
  page is read directly).
* **Rate limits.** None documented; one request per declared release. The file
  is read whole within the source byte budget (100 MB); a larger file is
  `budget_exhausted`, never truncated (_unverified_ current size).
* **Updates, corrections and removals.** Each release is a **vintage**: the
  feed `dateModified` and the response digest are recorded in the receipt and on
  every revision first observed in it. An item changed in a later release is a
  new revision; an item missing from a later release of the same selection is an
  `absent-from-release` revision, never a deletion.
* **Duplicates with the API.** The same review (same publisher site and
  canonical review URL, `wa-canon-v1`) acquired from Google and Data Commons is
  one record key under two sources; both provenance records are kept and shown
  side by side.

### IFCN Code of Principles signatories

* **Endpoint.** `https://ifcncodeofprinciples.poynter.org/signatories` (not
  fetchable, 2026-09-30). A search-engine excerpt of the listing (via the
  `mail.` host) shows status groups "verified active", "under renewal" and
  "expired"; the application-process page excerpt describes an annual renewal
  with "a three months period to complete their renewal process". No API or
  download was found in the material read.
* **Terms.** No reuse or automated-access terms were found in the material read
  (_unverified_). **Decision:** the adapter refuses a live fetch of the listing
  (`licensing` error) until an operator records `fact_checks.terms_confirmation`
  on the source after reading the site's terms; fixture replays are unaffected.
  Only the listing page is ever fetched (never profiles or applications).
* **Parser assumptions (_unverified_).** A signatory card is delimited by a
  link to `/profile/<slug>` (the IFCN identifier), whose text is the name; the
  card's status label is read from an element whose class names "status" or,
  failing that, from the published labels (verified, under renewal, under
  review, expired); a status date is read only when labelled ("verified since",
  "expires on", "expired on", ...); the website is the first outbound link.
  A listing without profile links is `schema_drift`, never an empty listing.
* **Updates and removals.** The status label is stored as published; a changed
  label or date is a dated revision (the published date, otherwise the
  acquisition date); a signatory missing from a later listing is an
  `absent-from-listing` revision. Absence is never read as a statement about
  the publisher.
* **Publisher links.** A signatory's website domain equal to a fact-check's
  publisher site is a shared identifier used to show the status in effect at
  review time; links to source identities (`src/kb/source_identity.py`) are only
  reviewable `published-domain` assertions.

## Claimant data-minimisation decision

Fact-checks name people: claimants (often public officials, sometimes private
individuals), review authors and appearance authors.

* **Stored.** Publisher name and site; review URL, title, date and language;
  claim text as quoted; the claimant **as named by the publisher**, the claimant
  type and any `sameAs` identifiers the publisher published; claim date and
  appearance URLs; the rating text and any numeric rating with the publisher's
  scale; IFCN signatory name, website, country and status.
* **Never stored (dropped in the parser, refused by the store).** Names of
  review authors who are natural persons; images (claimant, author, rating,
  logos); claimant job titles, birth dates, addresses, e-mail and telephone;
  appearance and first-appearance authors; article bodies. Each record lists the
  withheld paths under `minimisation.withheld`; `FactChecksStore.project`
  refuses a record that still carries a withheld attribute or any Noesis
  verdict or normalised-rating key (`minimisation_violation`).
* **Claimants are never enriched.** A claimant match to a canonical entity is a
  reviewable proposal only (published identifier first, then name); generic
  attributions ("social media users", "viral image") are never proposed.
* **Who may query.** Fact-checks are read with
  `knowledge:news:fact-checks:read`, which shows claimants as named inside the
  cited fact-check. Searching by claimant, listing or reviewing claimant
  matches, and claimant monitors need `knowledge:news:fact-checks:claimant:read`
  in addition.
* **Retention.** Records are retained with their revision chain; a publisher's
  correction or removal is a revision. An erasure request concerning a claimant
  is an operator action outside the first coverage; none is automated (open
  point for FC13).

## Bounded first coverage

* **Google:** declared queries or review-publisher sites, at most 20 units per
  source, `maxAgeDays` at most 366, at most 5 pages of 50 claims per unit. The
  fixture selection is a topic query and one publisher site.
* **Data Commons:** one release per unit filtered to at most 50 declared
  publisher sites and a review window of at most 366 days; at most 2,000
  fact-check records per release after filtering.
* **IFCN:** the signatories listing, at most 500 signatories; status history is
  what successive acquisitions observe plus any status date the listing shows.
* **Claims, articles, claimants:** reached only through citations and reviewed
  matches.

The selections in `config/source_packs/osint.json` are placeholders on
`*.example.*` sites until the FC13 live run replaces them with a dated, bounded
selection.

## Source-pack location

The News bundle is code-registered (`src/domains/news/`) and has no source pack
of its own: the composition adapter builds its manifest from the `DomainPack`
(which contributes no source packs) plus `packs/news/composition.json`. The
three sources are therefore added to the existing `bounded-public-osint` pack
(`config/source_packs/osint.json`, 1.1.0 → 1.2.0), as the spec suggests,
because it already holds public-web news discovery (BBC RSS, GDELT) and web
archive indices, its defaults carry the `respect_robots_and_terms` and review
policy these sources need, and the web-archive captures used for citing URLs
belong to the same Osint acquisition. The News composition references it with
`^1.2.0`. `guardian-news` was not used: it is one publisher's keyed pack.
