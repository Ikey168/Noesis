# Technology vulnerabilities: source-contract audit and provider coverage (V01)

Tracking: #1913 · delivery issue #1922 · recorded 2026-09-27.

This audit fixes, per source, what the Technology bundle's optional
`vulnerabilities` feature may acquire, how, and on what terms. It was written
without network access: endpoints, formats, limits and terms are recorded from
the providers' published documentation as known to the author. **Every item
marked _verify_ must be checked against the live page before the first dated
live run (V13, #2025), and no source is `live` until that run exists.** The
machine-readable copy of these decisions is `PROVIDER_CONTRACTS` in
`src/ingestion/vulnerability_sources.py`; the MCP tool
`vulnerability_source_contracts` returns it.

Non-goals apply to every source: no exploitability or risk verdict, no patch or
remediation advice, and no severity where the source states none. Sources are
never merged: an NVD record, an OSV advisory, a GitHub advisory and a CISA KEV
entry about one CVE are separate revision series kept side by side.

## Access decisions

| Source | Source entry | Decision | Reason |
| --- | --- | --- | --- |
| NVD CVE API 2.0 | `nvd-cve-api` | `unverified-live` | parser and fixtures follow the documented 2.0 response; no dated live run |
| NVD CVE Change History API 2.0 | `nvd-cve-history` | `unverified-live` | parser follows the documented `cveChanges` shape; no dated live run |
| OSV API | `osv-api` (existing entry, reviewed) | `unverified-live` | `GET /v1/vulns/{id}` for a pinned id list; no dated live run |
| GitHub Advisory Database | `github-advisory-database` | `unverified-live` | OSV-format files from the public repository for pinned paths |
| CISA Known Exploited Vulnerabilities | `cisa-kev` | `unverified-live` | documented JSON feed; no dated live run |
| FIRST EPSS API | `first-epss` | `unverified-live` | documented JSON envelope; where the model version is carried is _verify_ |
| CVE Services (CVE JSON 5 records) | `cve-services` | `unverified-live` | public read of published records; _verify_ that no credential is needed |
| NVD CPE dictionary (Products API 2.0) | `nvd-cpe-dictionary` | `unverified-live` | pinned `cpeMatchString` selection only |
| CWE list downloads | `cwe-downloads` | `unverified-live` | versioned XML (zip) download; parser accepts the zip or the XML |

**Review of the existing `osv-api` entry.** It was declared with the generic
`declarative-rest` connector, the `technical-record-v1` mapping and the pack's
shared fixture, and had never been run through a vulnerability parser. It is
now served by the `vulnerability-feed` connector with the `osv` format, a
pinned id selection and its own fixture; its endpoint and licence entry are
unchanged, and the provider's `modified` timestamp drives revisions.

**Unavailable-access fallback.** When a provider cannot be reached (HTTP error,
refused redirect, rate limit exhausted after retries, schema drift, budget
exceeded), the run records the failure class and projects nothing for that
source. Earlier revisions stay authoritative for their dates. A CVE with no
KEV entry or no EPSS observation reads `unknown`, never "not exploited"; a
component with no acquired advisory reads `unknown`, never "unaffected".

## Per-source contracts

Delivers, per source: **R** advisory registration, **V** vendor/CNA statement,
**G** version ranges, **X** exploitation evidence, **S** scores,
**D** reference data.

### NVD CVE API 2.0 (R, G via CPE applicability, S, weakness classification)

- **Access:** `GET https://services.nvd.nist.gov/rest/json/cves/2.0` with
  `cveId`, or `lastModStartDate`/`lastModEndDate` (ISO-8601, both required
  together, at most 120 days apart), `startIndex`, `resultsPerPage` (max 2000).
- **Authentication:** optional API key sent in the `apiKey` request header;
  requested at `https://nvd.nist.gov/developers/request-an-api-key`. The key is
  a credential (`NOESIS_NVD_API_KEY`, `optional-secret`) and is never stored in
  the pack, a receipt or a document.
- **Rate limits:** 5 requests per rolling 30 seconds without a key, 50 with a
  key; NIST recommends sleeping about 6 seconds between requests. The adapter
  paces live requests at 6 s (unkeyed) or 0.6 s (keyed) (_verify_ current
  limits).
- **Pagination:** `startIndex` + `resultsPerPage`; `totalResults` ends paging.
- **Cadence / modified-since:** records change continuously;
  `lastModStartDate`/`lastModEndDate` select a window. `lastModified` is the
  per-record revision time.
- **Identifiers:** CVE id; CPE 2.3 names and `matchCriteriaId` in
  applicability statements; CWE ids in `weaknesses`.
- **Semantics:** `vulnStatus` (Received, Awaiting Analysis, Analyzed, Modified,
  Deferred, Rejected …) is kept verbatim; Rejected CVEs keep their history.
  CVSS metrics are quoted per `source` and `type` (Primary/Secondary) and per
  CVSS version (2.0, 3.0, 3.1, 4.0); nothing is averaged.
- **Retained evidence:** each CVE record as a document revision plus a
  vulnerability-store revision (content digest, `lastModified`, observation).
- **Terms:** NVD terms of use (`https://nvd.nist.gov/developers/terms-of-use`):
  public data, notice "This product uses the NVD API but is not endorsed or
  certified by the NVD" required (_verify_).

### NVD CVE Change History API 2.0 (dated change events)

- **Access:** `GET https://services.nvd.nist.gov/rest/json/cvehistory/2.0` with
  `cveId` or `changeStartDate`/`changeEndDate` (120 days), `startIndex`,
  `resultsPerPage` (max 5000). Same key, limits and terms as the CVE API.
- **Identifiers / semantics:** `cveChangeId`, `eventName` (e.g. Initial
  Analysis, CVE Modified, CVE Rejected), `created`, `details` (action, type,
  old/new value). Stored as dated change events of the CVE, keyed by
  `cveChangeId`; re-acquisition adds new events and never rewrites one.

### OSV API (R, G)

- **Access:** `GET https://api.osv.dev/v1/vulns/{id}` for a pinned id list
  (`POST /v1/query` and `/v1/querybatch` exist but the runtime transport is
  GET-only). Bulk exports per ecosystem exist at
  `https://osv-vulnerabilities.storage.googleapis.com/{ecosystem}/all.zip`
  and are not used (unbounded).
- **Authentication / limits:** none / no documented limit (_verify_); one
  request per page.
- **Identifiers:** OSV id (e.g. `PYSEC-…`, `GHSA-…`), `aliases` (CVE ids),
  package `ecosystem` + `name`, `purl` where given.
- **Semantics:** `modified` is the revision time; `withdrawn` marks a
  withdrawn advisory; ranges are ordered `introduced`/`fixed`/`last_affected`/
  `limit` events per range type (`ECOSYSTEM`, `SEMVER`, `GIT`). Ecosystem
  strings are kept as written and canonicalised only through
  `canonical_ecosystem`.
- **Terms:** aggregated data; each advisory keeps its upstream licence (for
  example CC-BY 4.0 for the GitHub and PyPA databases); the OSV schema is
  Apache-2.0 (_verify_ per upstream).

### GitHub Advisory Database (R, G)

- **Access:** OSV-format JSON files from the public repository
  `github/advisory-database`, fetched from
  `https://raw.githubusercontent.com/github/advisory-database/main/advisories/`
  + pinned paths (`github-reviewed/YYYY/MM/GHSA-…/GHSA-….json`). The REST API
  (`/advisories`) returns a different shape and is not used.
- **Authentication / limits:** none for raw files (_verify_ rate limiting of
  raw.githubusercontent.com); one file per page.
- **Identifiers:** GHSA id, CVE aliases, ecosystem ranges, `withdrawn`,
  `database_specific.cwe_ids` and `severity` (CVSS vector as stated).
- **Terms:** CC-BY 4.0 (`https://github.com/github/advisory-database/blob/main/LICENSE.md`).

### CISA Known Exploited Vulnerabilities (X)

- **Access:** `GET https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json`
  (also CSV). One file per run; an entry count above the run budget fails the
  run rather than truncating it.
- **Authentication / limits / pagination:** none / undocumented / none.
- **Identifiers / fields:** `cveID`, `vendorProject`, `product`,
  `vulnerabilityName`, `dateAdded`, `shortDescription`,
  `knownRansomwareCampaignUse`, `notes`, `cwes`; catalog `catalogVersion` and
  `dateReleased`. `requiredAction` and `dueDate` are federal remediation
  directives and are deliberately not stored, so no remediation text is
  repeated.
- **Semantics:** presence is evidence of a catalog listing on `dateAdded`, not
  an exploitability verdict; absence is `unknown`.
- **Terms:** U.S. Government work (_verify_ reuse notice).

### FIRST EPSS API (S)

- **Access:** `GET https://api.first.org/data/v1/epss?cve=CVE-…,CVE-…` (up
  to 100 CVEs per request in this adapter); `date` and `scope=time-series`
  exist. The CVEs requested are the pinned selection or those present in the
  vulnerability store (run parameter `cve_ids`, built by
  `store_cve_ids`).
- **Authentication / limits:** none / not documented (_verify_).
- **Pagination:** `offset` + `limit`; one page per request here.
- **Semantics:** each row `cve`, `epss`, `percentile`, `date`. The model
  version (v2023.03.01 for EPSS v3, v2025.03.14 for EPSS v4) is printed in the
  daily CSV header; whether and where the JSON API returns it is _verify_. The
  adapter records `model_version` when the response carries it (per row or at
  the envelope) and otherwise stores it as unknown; it is never guessed.
  Scores from different dates or model versions are separate observations and
  are never averaged or merged with CVSS.
- **Terms:** FIRST EPSS usage terms (attribution requested) (_verify_).

### CVE Services (R, V)

- **Access:** `GET https://cveawg.mitre.org/api/cve/{cveId}` returns the
  published CVE JSON 5 record (CNA container, ADP containers). CNA write
  operations need `CVE-API-ORG`/`CVE-API-USER`/`CVE-API-KEY` headers and are
  never used; public read is assumed credential-free (_verify_). The bulk
  `CVEProject/cvelistV5` repository is an alternative not used here.
- **Identifiers / semantics:** `cveMetadata.cveId`, `state` (PUBLISHED or
  REJECTED), `dateUpdated`; CNA `affected` product/version statements are the
  vendor statement; CNA and ADP CVSS metrics are quoted per provider.
- **Terms:** CVE Program terms of use (`https://www.cve.org/Legal/TermsOfUse`)
  (_verify_).

### NVD CPE dictionary (Products API 2.0) (D)

- **Access:** `GET https://services.nvd.nist.gov/rest/json/cpes/2.0` with a
  pinned `cpeMatchString` selection, `startIndex`, `resultsPerPage`. Same key,
  limits and terms as the CVE API.
- **Identifiers / semantics:** CPE 2.3 name, `cpeNameId`, titles,
  `deprecated` and `deprecatedBy`, `lastModified`. Published as the
  `technology-cpe` ontology module; unchanged data re-publishes idempotently.

### CWE list downloads (D)

- **Access:** `https://cwe.mitre.org/data/xml/cwec_latest.xml.zip` (versioned
  `cwec_v4.N.xml.zip` files exist). The archive contains one XML
  `Weakness_Catalog` with `Version` and `Date`.
- **Identifiers / semantics:** CWE id, name, abstraction, status, description,
  `ChildOf` relations in view 1000. Published as the `technology-cwe` ontology
  module per CWE version.
- **Terms:** CWE terms of use (free use with the MITRE copyright notice)
  (_verify_).

## Fixtures

Every fixture under `tests/fixtures/vulnerabilities/` and
`tests/fixtures/source_packs/technical-*.json` is authored in the documented
payload shape and names fictional packages, products and CVE numbers
(`CVE-2099-…`, `GHSA-f1x7-…`, `pkg:pypi:fixture-*`). None of it is live
evidence; offline fixture evidence and live evidence are reported separately.
