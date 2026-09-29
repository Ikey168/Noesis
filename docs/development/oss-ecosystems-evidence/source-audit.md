# Open-source Software Ecosystems: source audit and bounded coverage (OS01)

Tracking: [#2192](https://github.com/Ikey168/Noesis/issues/2192) · delivery issue
[#2193](https://github.com/Ikey168/Noesis/issues/2193) · recorded 2026-09-28.

This audit decides which public registries and archives the Open-source
Software Ecosystems pack reads in v1, on what terms, what history each exposes
and which personal data each returns and how it is dropped. It was written
**without network access**: endpoints, field names, rate limits and licence
clauses come from the providers' published documentation as the author knows
it. **Every item marked _verify_ must be checked against the live terms page
and a published response before the first dated live run (OS13, #2205).** No
source is `live` until that run exists; all are `unverified-live`.

The machine-readable copy of these decisions is `PROVIDER_CONTRACTS` in
`src/ingestion/oss_ecosystem_sources.py`, returned by the MCP tool
`oss_source_contracts`.

## Rules that apply to every source

- **No verdicts.** No code-quality, security-posture, health, popularity or
  trust score is derived from anything below. Download counts, stars and
  dependents counts are not read.
- **No people.** Maintainer, author, owner-user, publisher-user, committer and
  contributor fields are dropped *at parse time*, before a record exists; no
  record type has a field that could hold them (`FORBIDDEN_FIELDS` in
  `src/kb/oss_ecosystem_records.py`, asserted by a schema test). Only
  organisation-level publisher declarations are kept: PyPI organisations, npm
  scopes (as a namespace, see below), Maven `groupId` namespaces and crates.io
  teams. Individual account pages are never requested.
- **Documented public APIs and archives only.** No HTML scraping.
- **Advisories are cited, never copied.** Where a source returns advisory
  identifiers (PyPI `vulnerabilities`, deps.dev `advisoryKeys`) they are dropped
  at parse time; advisories come from `technology.vulnerabilities` by citation.
- **No licence interpretation.** Declared licences are quoted and normalised to
  SPDX expressions against a pinned SPDX License List version; they are never
  interpreted for compliance.
- **Tokens** go through the existing secret mechanism (`auth.secret_ref`,
  resolved by the source-pack runtime); none is committed.

## Registries

| Source | Endpoint(s) | Auth | Terms and attribution | Rate limits | Stable identifiers | History exposed | Decision |
| --- | --- | --- | --- | --- | --- | --- | --- |
| PyPI JSON API | `GET https://pypi.org/pypi/{project}/json`, `GET https://pypi.org/pypi/{project}/{version}/json` | none | PyPI Terms of Service; package metadata is supplied by publishers under their own licences (verify) | no published hard limit; CDN-served, polite use expected (verify) | project name normalised per PEP 503; version string as published | per release: files with `upload_time_iso_8601`, `yanked` and `yanked_reason` (PEP 592); per version: `requires_dist`, `license`, `license_expression` (core metadata 2.4, PEP 639), `classifiers`, `project_urls`; organisation ownership where the API states it (`ownership.organization`, verify) | **implement** |
| PyPI Simple API (PEP 503/691) | `GET https://pypi.org/simple/{project}/` with `Accept: application/vnd.pypi.simple.v1+json` | none | as above | as above | as above | per file: `yanked` (bool or reason string, PEP 592/691) | **link-only**: the JSON API carries the same yank flags and reasons per file (verify); the Simple API is the fallback if a later audit finds them diverging |
| PyPI BigQuery datasets | `bigquery-public-data.pypi.*` | Google Cloud account | Google Cloud terms | billed queries | — | download statistics, file metadata | **out of scope** (non-goal: download counts are popularity signals; billed access) |
| npm registry | `GET https://registry.npmjs.org/{name}` (full packument; scoped names as `@scope%2Fname`) | none | npm Terms of Use and Open-Source Terms; package metadata under publishers' licences (verify) | undocumented; bulk crawling discouraged (verify) | package name (lower-case, scoped `@scope/name`); version (semver) | `versions[v]` with `dependencies`, `devDependencies`, `peerDependencies` (+ `peerDependenciesMeta.optional`), `optionalDependencies`, `license` (string, or legacy `{type}` object / `licenses` array), `deprecated` message; `time` with `created`, `modified` and one publication time per version; a version removed by unpublish stays in `time` but leaves `versions`, and a fully unpublished package's packument carries `time.unpublished` (verify both) | **implement** |
| crates.io API | `GET https://crates.io/api/v1/crates/{name}`, `GET .../crates/{name}/{version}/dependencies`, `GET .../crates/{name}/owner_team` | none | crates.io Data Access Policy and crawler policy: at most 1 request per second, a `User-Agent` naming the tool and a contact is required (verify); crates.io metadata under the crate authors' licences | 1 request/second (crawler policy, verify) | crate name (case-insensitive, stored lower-case); `num` version | per version: `created_at`, `updated_at`, `yanked`, `yank_message` (verify), `license` (SPDX expression; legacy `/` means OR per Cargo's documentation, verify); dependencies with `req`, `kind` (`normal`/`dev`/`build`), `optional`, `target` | **implement**; the `owner_user` endpoint is never called |
| Maven Central | search `GET https://search.maven.org/solrsearch/select?...&core=gav` (versions and publication timestamps), `GET https://repo1.maven.org/maven2/{group path}/{artifact}/{version}/{artifact}-{version}.pom`; `maven-metadata.xml` is link-only in v1 (the search listing carries the versions with timestamps, verify) | none | Sonatype Central terms of service; artifacts under their own licences (verify) | undocumented; search throttled (verify) | `groupId:artifactId` (case-sensitive), version | versions and `lastUpdated` (metadata); per POM: `<dependencies>` with `scope` and `optional`, `<licenses>`, `<scm>`, `<organization>`; publication timestamps from the search API. Maven Central has **no yank, deprecation or unpublish**; artifacts are immutable (verify). Relocation (`distributionManagement/relocation`) is not a state and is not read in v1 | **implement** |

### Personal data per registry and the parse-time rule

| Source | Fields holding individual names, emails or usernames | Rule |
| --- | --- | --- |
| PyPI JSON | `info.author`, `info.author_email`, `info.maintainer`, `info.maintainer_email`, `ownership.roles[].user` (verify), file `uploaded_via` is a tool string (kept out anyway) | never read; only `ownership.organization` is kept as an organisation declaration |
| npm | `maintainers[]`, `author`, `contributors[]`, `_npmUser`, `versions[v]._npmUser`, `versions[v].maintainers` | never read; the scope of a scoped name is kept as a **namespace** declaration only. npm does not state whether a scope is an organisation or a user scope (verify), so scopes are recorded with `organisation_kind: npm-scope` and are **never proposed** as organisation identity candidates |
| crates.io | `versions[].published_by`, `versions[].audit_actions[].user`, `owner_user` endpoint | never read / never requested; `owner_team` teams (`github:{org}:{team}`) are organisation-level and kept |
| Maven POM | `<developers>`, `<contributors>` | never read; `groupId` is kept as the namespace declaration and `<organization><name>` as the organisation the POM states |

## Secondary sources

| Source | Endpoint(s) | Auth | Terms and attribution | Rate limits | Stable identifiers | What it adds | Decision |
| --- | --- | --- | --- | --- | --- | --- | --- |
| deps.dev API v3 | `GET https://api.deps.dev/v3/systems/{system}/packages/{name}`, `.../versions/{version}`, `.../versions/{version}:dependencies` | none | Open Source Insights terms; data under CC-BY 4.0 with attribution "Open Source Insights (deps.dev)" (verify) | undocumented (verify) | `system` (`PYPI`, `NPM`, `CARGO`, `MAVEN`, `GO`), package name, version | per version: `publishedAt`, `isDefault`, `licenses` (deps.dev's SPDX rendering), `relatedProjects` (`SOURCE_REPO` links), and a resolved dependency graph (`nodes` with `relation` `SELF`/`DIRECT`/`INDIRECT`, `edges` with the `requirement` as declared). The graph is **deps.dev's resolution with deps.dev's semantics**, stored as a separate published graph and never merged with Noesis's declared-constraint resolution. `advisoryKeys`, `slsaProvenances` and attestation fields are dropped | **implement** (optional feature `oss-deps-dev`) |
| SPDX License List | `GET https://raw.githubusercontent.com/spdx/license-list-data/v{version}/json/licenses.json` and `.../exceptions.json` | none | list data CC0-1.0; licence texts under their own terms (verify) | GitHub raw content limits (verify) | `licenseListVersion`; `licenseId`, `licenseExceptionId` | licence and exception identifiers with `isDeprecatedLicenseId` and names, per pinned list version. Every normalisation cites the list version it used | **implement** (pinned releases only) |
| Software Heritage API | `GET https://archive.softwareheritage.org/api/1/origin/{origin_url}/visits/`, `GET .../api/1/snapshot/{snapshot_id}/?target_types=release,revision` | optional bearer token (raises the anonymous limit, verify) | Software Heritage terms of use; archive metadata may be reused with attribution, content licences vary (verify) | anonymous about 120 requests/hour, authenticated higher; `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset` headers (verify) | origin URL; visit number; SWHIDs `swh:1:snp:…`, `swh:1:rel:…`, `swh:1:rev:…` | visit dates and status, snapshot SWHID, and tag branches (`refs/tags/…`) with their target SWHIDs. **Only origins asserted in `repository_link_assertion` records are requested.** Release and revision objects (which carry author and committer identities and messages) are never fetched; only identifiers and dates from visits and snapshots are stored | **implement** (optional feature `oss-software-heritage`) |
| Libraries.io | `https://libraries.io/api/…` | API key | API terms of service; data dumps CC BY-SA 4.0 (verify) | 60 requests/minute (verify) | platform + name | aggregated package, dependency and repository metadata, contributor and popularity signals | **not implemented**: its dependency and licence data duplicate the registries and deps.dev we read at source; CC BY-SA 4.0 share-alike would attach to derived records (verify); its repository contributor and popularity fields are exactly what the non-goals exclude |

## Bounded v1 coverage

- **Explicit package lists only.** Each source declares `oss.selection.packages`
  (1–50 names per source, per ecosystem). Nothing enumerates a registry, an
  organisation or a dependents list.
- **Release window.** Per package an explicit list of at most 50 versions is
  read through the per-version endpoints (PyPI version JSON, crates.io
  dependencies, Maven POMs, deps.dev versions). Release *states* come from the
  project-level document (PyPI project JSON, npm packument, crates.io crate,
  Maven metadata) and cover every version it lists.
- **Graph depth and size.** `dependency_graph_as_of` resolves at most depth 5
  (default 3) and 200 nodes; an edge beyond either limit, or to a package not in
  any selection, is listed as unresolved with its reason. deps.dev graphs are
  read only for the explicit versions.
- **Software Heritage.** At most 20 origins per run, each from a repository link
  assertion, 1 visit listing (`per_page` ≤ 20) and 1 snapshot page (≤ 1000
  branches) per origin.
- **SPDX.** Pinned list versions only (v1 pins 3.25, verify the tag name).

## Identity

Repositories and publisher organisations connect to packages only through
source-stated links (registry metadata, deps.dev related projects, archive tag
evidence) proposed as candidates and accepted by review, recorded as entity
identity decisions in `src/kb/entity_history.py` and revertible. A repository
claimed by two packages is flagged, never resolved. The same package name in
two ecosystems is two packages. Individual accounts are never candidates.
