# Platform transparency source audit (SP01, #2585)

Status: audited 2026-09-30 for the OSINT pack's `osint.platform-transparency`
provider (tracker #2580, subdomain `social-platforms`, ADR-005). Machine-readable
copy: `PROVIDER_CONTRACTS`, `BOUNDED_COVERAGE`, `MINIMISATION`,
`EXCLUDED_SOURCES` and `LIVE_VERIFICATION` in
`src/ingestion/platform_transparency_sources.py`, served by the
`platform_transparency_source_contracts` MCP tool.

**Terms were not re-verified live.** The source pages
(`transparency.dsa.ec.europa.eu`, `www.facebook.com/ads/library/api`,
`adstransparency.google.com`, `lumendatabase.org`) were blocked by this
runtime's egress proxy on 2026-09-30. This audit is written from the tracker's
references and the providers' published documentation as known without
network access. Every endpoint, field name, value vocabulary and licence term
marked *verify* must be checked by an operator before the dated live run of
SP14 (#2650). No source is `verified-live`.

## Decisions

| Source (`source_id`) | Provider | Access decision | Live status |
| --- | --- | --- | --- |
| `dsa-sor-dumps` | EU DSA Transparency Database | implemented: declared daily light dumps | `unverified-live` |
| `meta-ad-library-political` | Meta Ad Library API | implemented: declared pages, countries and windows, token required | `unverified-live` |
| `google-political-ads` | Google political ads transparency bundle | implemented: declared advertiser ids | `unverified-live` |
| `lumen-notices` | Lumen | implemented as a **gated** connector; researcher access not granted to this deployment | `gated-not-granted` |

Documented, not acquired: the Meta Ad Library *Report* (aggregates per
advertiser and region; the per-ad API answers the bounded questions), the
Google BigQuery dataset `bigquery-public-data.google_political_ads` (needs a
billed Google Cloud project; same data as the bundle), the TikTok ad library
research API (application-gated, terms forbid redistribution) and an X ads
repository (no stable machine access).

Lumen is the one source whose terms make the intended use conditional:
researcher access is granted by Lumen on application, and this deployment has
none. Its connector is implemented and fixture-tested so an operator holding a
researcher token can run it, but without `NOESIS_LUMEN_API_TOKEN` a run fails
with `authentication_failed`, readiness reports the provider as `unavailable`
with the reason, and **live Lumen coverage is not implemented** until access is
granted. The `social-platforms` subdomain is covered offline by the other three
sources.

## Per-source contracts

### EU DSA Transparency Database (`dsa-sor-dumps`)

- **Endpoints.** Index `https://transparency.dsa.ec.europa.eu/data-download`;
  files `https://dsa-sor-data-dumps.s3.eu-central-1.amazonaws.com/sor-{platform}-{YYYY-MM-DD}-{full|light}.zip`
  (*verify* naming and host). Each ZIP holds CSV parts, possibly as nested ZIP
  parts; the connector reads both. The submission API
  (`/api/v1/statement`) is for platforms and is not used.
- **Authentication.** None.
- **Licence and redistribution.** Commission reuse policy (Decision
  2011/833/EU), CC BY 4.0 for Commission data unless stated otherwise
  (*verify* the database's own terms). Attribution: "Source: European
  Commission, DSA Transparency Database".
- **Rate limits.** None documented for the dump host (*verify*). One declared
  file per unit; a file with more than 5,000 rows is `budget_exhausted`, never
  truncated.
- **Identifiers.** Statement `uuid` with `platform_uid`; the dump file name.
- **Updates, corrections and removals.** Statements are immutable once
  submitted. A dump republished with different bytes is a new `dump-release`
  revision (the dump version is file name, variant and SHA-256 of the bytes,
  recorded in the receipt). A statement that changes in a republished dump is a
  new `revised` revision; one no longer present is a `not-returned` revision.
  Nothing is deleted.

### Meta Ad Library API (`meta-ad-library-political`)

- **Endpoint.** `https://graph.facebook.com/{version}/ads_archive` with
  `ad_type=POLITICAL_AND_ISSUE_ADS`, `ad_reached_countries`, `search_page_ids`,
  `ad_delivery_date_min/max`, `ad_active_status=ALL` and an explicit `fields`
  list (*verify* the Graph API version, here `v21.0`).
- **Authentication and key handling.** A user access token of an
  identity-confirmed developer account, held as the required secret
  `NOESIS_META_AD_LIBRARY_TOKEN` and sent as `Authorization: Bearer`, never as
  the `access_token` query parameter, in a record or in a receipt. The token
  Meta embeds in `ad_snapshot_url` is stripped before storage; the store
  refuses any record still carrying `access_token=`.
- **Licence and redistribution.** Meta Platform Terms and the Ad Library API
  terms: research and transparency use; no sale, advertising use or profiling
  (*verify*).
- **Rate limits.** Graph API application and business-use-case limits; HTTP 429
  is `rate_limited` with `Retry-After` (*verify* the error codes).
- **Paging.** Cursor paging (`paging.cursors.after`), 100 per page, at most 5
  pages per unit; longer is `budget_exhausted`.
- **Identifiers.** Ad archive `id`, `page_id`.
- **Updates, corrections and removals.** Stop times and spend and impression
  ranges change while an ad runs; a changed ad is a `revised` revision. An ad no
  longer returned for the same declared selection is a `not-returned` revision
  stated as an observed absence, not a stated deletion.

### Google political ads (`google-political-ads`)

- **Endpoint.** `https://storage.googleapis.com/transparencyreport/google-political-ads-transparency-bundle.zip`
  (*verify*), members `google-political-ads-advertiser-stats.csv`,
  `google-political-ads-creative-stats.csv` and
  `google-political-ads-updated.csv` (`Report_Data_Updated_Time`, *verify*).
- **Authentication.** None.
- **Licence and redistribution.** Google Transparency Report data, reuse with
  attribution (*verify*).
- **Rate limits.** None documented. The live bundle is large: the source
  declares the maximum byte budget (100 MB); an operator may need to raise it
  or move to BigQuery, which is recorded rather than worked around.
- **Identifiers.** `Advertiser_ID` (`AR…`), `Ad_ID` (`CR…`) and
  `Public_IDs_List` as published (FEC committee ids are read from it for
  identity).
- **Updates, corrections and removals.** The bundle is regenerated; its refresh
  time is stored on every record (`data_as_of`). A changed row is a `revised`
  revision; an ad no longer listed for a declared advertiser is `not-returned`.
  Spend ranges (`Spend_Range_Min/Max_{CUR}`) and impression buckets are stored
  as the strings Google published.

### Lumen (`lumen-notices`)

- **Endpoint.** `https://lumendatabase.org/notices/search.json` with
  `recipient_name`, a date-received facet, `per_page` and `page` (*verify*
  the facet parameter names).
- **Authentication and key handling.** Researcher token
  `NOESIS_LUMEN_API_TOKEN` in the `X-Authentication-Token` header; granted by
  Lumen on application. Not granted here (see Decisions).
- **Licence and redistribution.** Researcher terms: research use; no
  republication of notice bodies or URLs beyond the public notice page
  (*verify* the terms of any granted access).
- **Rate limits.** Per token (*verify*); at most 4 pages of 50 per unit.
- **Identifiers.** Notice `id`.
- **Updates, corrections and removals.** Lumen may redact or update a notice; a
  changed notice is a `revised` revision with the redactions as published
  (`[Private]`, `[REDACTED]` are kept verbatim and listed).

## Minimisation

Policy `platform-transparency-minimisation-v1` (enforced in the parser and
again at write time by `PlatformTransparencyStore.project`, which refuses a
record with `minimisation_violation` before anything is written).

| Record | Stored | Never stored |
| --- | --- | --- |
| Statement of reasons | platform, uuid, decision types (visibility, monetary, provision, account), decision ground and legal or terms reference, category and specification, content type and language, account type, source type, automated detection and automated decision flags, territorial scope, dates | `decision_facts`, `illegal_content_explanation`, `incompatible_content_explanation`, `puid` (the platform's content id), `source_identity` (the notifier), content URLs |
| Ad | platform, ad id, advertiser as declared, funding entity as declared (`bylines`), delivery dates, spend and impression ranges and currency as published, languages, publisher platforms, regions and ad-level targeting as published, a token-free locator | creative bodies, link titles, captions and descriptions; `demographic_distribution`; `delivery_by_region`; access tokens |
| Advertiser | advertiser id and names, published public ids, election labels and totals as published | — |
| Takedown notice | id, type, title, sender, principal and recipient names as published (redactions preserved), dates, topics, jurisdictions, action taken, counts of works and URLs | notice body, work descriptions, infringing and copyrighted URLs, sender addresses, e-mail or telephone |

- **Individuals.** No stored field identifies a user of a platform.
  Advertisers and funding entities are stored as the platform published its
  political-ad disclosure. Identity matching targets organisation records only
  (campaign-finance committees and regulated entities, lobbying registrants and
  clients, Corporate Ownership legal entities); a natural-person record (a
  campaign-finance or elections candidate) is never a target, so a page named
  after a person stays unmatched.
- **Retention.** Revisions are retained with their source run; the stored
  fields carry no personal identifier, so nothing personal remains to purge.
  No automatic expiry in the first coverage.
- **Who may query.** `knowledge:osint:platform-transparency:read` (with
  namespace access) for statements, ads and advertisers. Lumen notices also
  need `knowledge:osint:platform-transparency:notices:read`; without it they
  are counted, never returned.

## Bounded first coverage

- **DSA:** declared (platform, day) light dumps, at most 20 units per source
  and 5,000 statements per unit. Moderation counts are computed only over
  stored statements and state the stored window, the days without a dump and
  the dump versions used. Justification: daily dumps of large platforms run to
  millions of rows; a bounded first coverage picks platforms and days whose
  files fit the unit bound.
- **Meta:** declared pages with reached countries and a delivery window of at
  most 366 days, at most 20 units and 500 ads per unit; an optional declared
  elections id per unit.
- **Google:** at most 20 declared advertiser ids from one bundle and 2,000
  creative rows; advertiser election labels mapped to elections ids only where
  the selection declares the mapping.
- **Lumen:** one declared recipient platform and at most 92 days per unit,
  under researcher access only.

The offline fixtures (`tests/fixtures/platform_transparency/`) are authored in
each provider's documented shape and name fictional platforms (Exampla Social,
Northwind Video), pages, advertisers and notices only.

## Exclusions

No user-level profiling, no collection of private content, no inference of
coordinated behaviour and no conversion of spend or impression ranges into
point estimates. Answers never contain midpoints or sums of ranges; monitors
report record changes, not assessments.
