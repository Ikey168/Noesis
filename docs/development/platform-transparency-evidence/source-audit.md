# Platform transparency: source-contract audit, data minimisation and bounded coverage (SP01)

Tracking: #2580 · delivery issue #2585 · recorded 2026-09-30.

This audit sets out, per source, what the OSINT pack's platform-transparency
features (`platform-transparency-dsa`, `platform-transparency-meta`,
`platform-transparency-google`, `platform-transparency-lumen`) may acquire,
how, on what terms, and how personal data is minimised. It was written from a
runtime whose egress proxy blocks most official pages. Everything below says
which page it was read from and when; **every point marked _unverified_ could
not be read from an official page and must be checked against the live
documentation, the live terms and a real response before the first dated live
run (SP14, #2650). No source is `verified-live` until that run exists.**

The machine-readable copy of these decisions is `PROVIDER_CONTRACTS`,
`LIVE_VERIFICATION`, `BOUNDED_COVERAGE` and `MINIMISATION` in
`src/ingestion/platform_transparency_sources.py`. Each source entry in
`config/source_packs/osint.json` (`bounded-public-osint` 1.2.0) states
`platform_transparency.live_verification: unverified-live`, and the MCP tool
`platform_transparency_source_contracts` returns the same decisions.

Non-goals for every source: no user-level profiling, no collection of private
content, no inference of coordinated behaviour and no conversion of spend or
impression ranges into point estimates. The OSINT review gate
(`docs/security/osint-review-gate.md`) is followed: nothing here adds a
gated capability, `narrative_coordination` is never applied to these records,
and no tool takes a person, handle, e-mail or IP as input.

## Pages read

| Page | Read on | Result |
| --- | --- | --- |
| https://transparency.dsa.ec.europa.eu/ (API documentation, data download, terms) | 2026-09-30 | **Blocked** by the egress proxy; _unverified_ |
| https://github.com/digital-services-act/transparency-database (README; `app/Models/Statement.php`, `app/Exports/StatementExportTrait.php`, `app/Services/DayArchiveService.php`, `app/Http/Controllers/DataDownloadController.php`, `routes/web.php`, `routes/api.php`, branch `main`) | 2026-09-30 | Read: the database's own published source code (GPLv2) |
| https://www.facebook.com/ads/library/api/ and https://developers.facebook.com/docs/graph-api/reference/ads_archive/ | 2026-09-30 | **Blocked**; _unverified_ |
| https://github.com/facebookresearch/Ad-Library-API-Script-Repository (README, `python/fb_ads_library_api.py`, `python/fb_ads_library_api_utils.py`, `python/fb_ads_library_api_cli.py`) | 2026-09-30 | Read: Meta's own example client |
| https://adstransparency.google.com/political, https://support.google.com/adspolicy/answer/6014595, https://console.cloud.google.com/marketplace/product/transparency-report/google-political-ads, https://docs.cloud.google.com/bigquery/public-data | 2026-09-30 | **Blocked**; _unverified_ |
| https://storage.googleapis.com/transparencyreport/google-political-ads-transparency-bundle.zip | 2026-09-30 | Read: 2,566 bytes, last modified 2022-06-02, an empty folder placeholder with no data files |
| https://github.com/Wesleyan-Media-Project/google_ads_archive (README) | 2026-09-30 | Read: **secondary** source (a research project) for the BigQuery table and column names |
| https://lumendatabase.org/pages/researchers and https://lumendatabase.org/pages/api_terms | 2026-09-30 | **Blocked**; _unverified_ |
| https://github.com/berkmancenter/lumendatabase/wiki/Lumen-API-Documentation | 2026-09-30 | Read: Lumen's own API documentation |

## Access decisions

| Source (source-pack id) | Provider | Delivers | Decision | Reason |
| --- | --- | --- | --- | --- |
| `platform-transparency-dsa-sor` | EU DSA Transparency Database, daily dumps | statements of reasons of declared platforms and days (light version) | `unverified-live` | Dump routes, versions and columns read from the database's published source code; the Commission's pages and terms were not readable |
| `platform-transparency-meta-ads` | Meta Ad Library API (`ads_archive`) | political and issue ads of declared page ids, countries and window | `unverified-live` | Request and field names read from Meta's own example client; documentation and terms not readable; access needs Meta's identity confirmation |
| `platform-transparency-google-political-ads` | Google political ads (BigQuery `google_political_ads`) | advertiser and creative rows of declared advertiser ids per region | `unverified-live` | The legacy download bundle is empty; column names come from a secondary source; Google's pages and terms not readable |
| (none) | Lumen database | takedown notices | **`not-implemented`** | See below |

## DSA Transparency Database

* **Endpoints.** The database's routes (`routes/web.php`) serve per-platform
  daily archives at
  `/explore-data/download/sor-{platformSlug}-{date}-{version}.zip` and the
  checksum at the same path with `.zip.sha1`, with `version` one of `full` or
  `light` and `platformSlug` the platform's slugified name (`global` is the
  whole database). The download controller answers with a redirect to the
  archive's stored URL (`redirectToArchiveUrl`), which is on another host; the
  source-pack runtime refuses cross-host redirects, so the storage host must be
  verified and declared before a live run (_unverified_).
* **Authentication.** None for dumps. The API in the repository is a
  submission API for platforms behind authentication; the README states that a
  research search API is "considered ... in future releases", so reads use the
  dumps.
* **Columns (light version).** `uuid`, `decision_visibility`,
  `decision_visibility_other`, `end_date_visibility_restriction`,
  `decision_monetary`, `decision_monetary_other`,
  `end_date_monetary_restriction`, `decision_provision`,
  `end_date_service_restriction`, `decision_account`,
  `end_date_account_restriction`, `account_type`, `decision_ground`,
  `decision_ground_reference_url`, `illegal_content_legal_ground`,
  `incompatible_content_ground`, `incompatible_content_illegal`, `category`,
  `category_addition`, `category_specification`,
  `category_specification_other`, `content_type`, `content_type_other`,
  `content_language`, `content_date`, `content_id_ean`, `application_date`,
  `source_type`, `source_identity`, `automated_detection`,
  `automated_decision`, `platform_name`, `platform_uid`, `created_at`
  (`StatementExportTrait::headingsLight`). The full version adds the free-text
  explanations, `territorial_scope` and `decision_facts`. Values are the
  model's keys (for example `DECISION_GROUND_ILLEGAL_CONTENT`,
  `AUTOMATED_DECISION_FULLY`); `automated_detection` is `Yes` or `No`. The
  encoding of multi-valued columns inside the CSV is _unverified_; the parser
  keeps a JSON array or a comma-separated list as published.
* **Licence.** The Commission's reuse policy (Decision 2011/833/EU) is
  expected to apply; the terms page could not be read (_unverified_).
* **Rate limits.** None documented for dumps (_unverified_); one dump and one
  checksum per unit.
* **Revisions.** A statement is keyed by platform and `uuid`. A dump is
  identified by platform, day and version and its published SHA-1, verified on
  acquisition; a republished dump with another checksum is a new revision of
  its `dump-release` record, and a changed row a new revision of the statement.
  Statements are never deleted.

## Meta Ad Library API

* **Endpoint.** `https://graph.facebook.com/{version}/ads_archive` with
  `ad_type=POLITICAL_AND_ISSUE_ADS`, `ad_reached_countries`,
  `search_page_ids`, `ad_delivery_date_min`/`max`, `fields` and `limit`;
  responses carry `data` and `paging` (`cursors.after`, `next`). Meta's example
  client defaults to Graph API `v14.0`; the source declares that version and
  the current version is _unverified_.
* **Fields requested.** `id`, `page_id`, `page_name`, `bylines`,
  `ad_creation_time`, `ad_delivery_start_time`, `ad_delivery_stop_time`,
  `currency`, `spend`, `impressions`, `publisher_platforms`, `languages` (all
  in the example client's list of valid fields). `spend` and `impressions` are
  ranges with `lower_bound` and `upper_bound`; they are stored as published and
  an absent upper bound stays absent.
* **Token handling.** A user access token of a developer who completed Meta's
  identity confirmation (required secret `NOESIS_META_AD_LIBRARY_TOKEN`), sent
  as an `Authorization: Bearer` header (whether the Graph API accepts it is
  _unverified_), never in a URL, receipt or record. `ad_snapshot_url` embeds
  the token and is never stored; the locator is the public Ad Library page of
  the ad. The pagination cursor is followed; the `next` URL (which embeds the
  token) is not.
* **Licence and rate limits.** Meta Platform Terms and Graph API rate limits
  (_unverified_); at most 5 pages of 100 ads per unit.
* **Revisions and removals.** No revision stamp is published. Each acquisition
  of a declared unit is compared with the stored revision; changed ranges are a
  revision; an ad that a complete listing of the same unit no longer returns
  receives a `not-returned` revision citing the listing (the platform does not
  say why), and a `relisted` revision if it returns. A response identical to an
  earlier one is treated as a replay.

## Google political ads

* **Route.** The legacy bundle `google-political-ads-transparency-bundle.zip`
  is an empty placeholder (read 2026-09-30), so the documented public BigQuery
  dataset `bigquery-public-data.google_political_ads` is read through the
  BigQuery REST API: `tables.get` for `advertiser_stats` and `creative_stats`
  (their `lastModifiedTime` is the data refresh date) and two fixed,
  parameterised `jobs.query` POSTs per unit (`@advertiser_id`, `@region`).
* **Columns.** From the secondary source: `advertiser_stats` has
  `advertiser_id`, `advertiser_name`, `public_ids_list`, `regions`,
  `elections`, `total_creatives` and `spend_<currency>`; `creative_stats` has
  `ad_id`, `ad_url`, `ad_type`, `regions`, `advertiser_id`, `advertiser_name`,
  `date_range_start`, `date_range_end`, `num_of_days`, `impressions` (a
  bucket), `first_served_timestamp`, `last_served_timestamp` and
  `spend_range_min_<currency>`/`spend_range_max_<currency>`, plus targeting
  columns that are never selected. All _unverified_ against Google's schema.
* **Authentication.** An OAuth access token for the operator's own Google
  Cloud billing project (required secret `NOESIS_GOOGLE_BIGQUERY_TOKEN`) as a
  bearer header; query costs fall on that project (_unverified_ free tier).
* **Licence and rate limits.** Google Cloud Public Datasets and Transparency
  Report terms; BigQuery quotas (_unverified_).
* **Revisions.** Stored per record: the refresh date (`source_as_of`) and the
  refresh order. A refresh with unchanged values adds no revision (the receipt
  records the refresh); changed values are a revision; an ad no longer returned
  for the same advertiser and region is a `not-returned` revision. A query that
  states more rows than one bounded page (`pageToken`) is `budget_exhausted`,
  never truncated.

## Lumen: not implemented

Lumen's own API documentation (read 2026-09-30) states that searches are
disabled without an authentication token (`X-Authentication-Token`), that
tokens are requested from the Lumen team and are "intended for research use
only", subject to the API Terms of Use, and that requests are throttled at
about one request per second. **Decision: not implemented.** No research token
has been granted to this deployment, the API Terms of Use and the researcher
page could not be read, so it cannot be established that storing and citing
notice fields is permitted; notices name senders and recipients who may be
individuals. No source-pack entry exists. The `takedown-notice` record kind
and its minimisation rule are defined (sender stored only when Lumen publishes
an organisation, redactions kept verbatim, never the notice body or infringing
URLs) so that a granted access can be added without a new store; the
`platform-transparency-lumen` feature adds no source.

## Data-minimisation decision

* **Stored.** Statements of reasons: `uuid`, platform name, decision type
  fields and end dates, account type as published, decision ground and its
  reference URL, legal or contractual ground, category fields, content type,
  language and date, product EAN, application date, automated detection and
  automated decision as published, `created_at`. Ads: ad id, page id and page
  name (advertiser as declared), `bylines` (funding entity as declared),
  delivery dates, spend and impression ranges with currency as published,
  publisher platforms, languages; Google advertiser ids and names, published
  identifiers, ad type, regions, date range, impression bucket and spend range.
* **Never stored.** `platform_uid`/`puid` (the platform's identifier of the
  user's content or account), `source_identity` (the notifier), decision facts
  and free-text explanations, `*_other` free text, territorial scope rows,
  creative text and `ad_snapshot_url`, demographic, regional and audience-size
  breakdowns, age, gender and geographic targeting, any user, viewer or
  account identifier, takedown-notice bodies and infringing URLs. They are
  dropped by the parser before any record, document or receipt exists and
  listed under `minimisation.withheld`; the store refuses a record carrying one
  (`minimisation_violation`), and MCP tools refuse an output carrying one.
* **Ranges.** Stored as the published bounds and bucket text; never converted
  to a midpoint, a sum of midpoints or any other point estimate; no answer
  sums spend.
* **Persons.** Advertisers and funding entities are legally required public
  disclosures and are stored as declared. They are matched only to
  organisational records (committees, regulated entities, party lists,
  lobbying registrants and clients, legal entities); election candidate
  records and other natural persons are never targets.
* **Retention.** Retained with their source revision; no user identifier is
  stored, so nothing user-level remains to purge; no automatic expiry in the
  first coverage.
* **Who may query.** `knowledge:osint:platform-transparency:read` with
  namespace access; identity review needs `knowledge:ownership:review`. No
  scope returns a withheld field.

## Bounded first coverage

* **DSA.** Light daily dumps of the platforms and days named in the selection
  (at most 31 platform-days per source, 20,000 statements per dump, larger is
  `budget_exhausted`); the `global` dump is refused as unbounded. Counts are
  computed over stored records only and state the window, the days without a
  stored dump and the dump versions used.
* **Meta.** At most 10 page ids per unit, declared countries and a delivery
  window of at most 366 days, at most 5 pages of 100 ads.
* **Google.** Declared advertiser ids per region, at most 500 creatives each.
* **Elections.** Reached only through accepted identity matches with party
  lists or with campaign-finance committees whose filings the campaign-finance
  feature has linked to a contest.

The first coverage in `config/source_packs/osint.json` names synthetic
placeholder platforms, pages and advertisers; the SP14 live run replaces them
with a declared real selection and records the outcome per source.
