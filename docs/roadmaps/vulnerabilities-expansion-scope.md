# Vulnerability and supply-chain advisory evidence expansion scope

Status: planned, 2026-09-27. An expansion of the existing Technology bundle;
it does not create a separate domain or pack. It never produces an
exploitability or risk verdict, patch or remediation advice, or a severity the
source does not give; scores are quoted with their publisher, date and model
version.

## Delivery state (audited 2026-09-27)

- Shipped: nothing yet. Planning issues #1922–#2025 are open.
- Composition dependency: a `technology.vulnerabilities` provider descriptor
  and optional `vulnerabilities` feature in the Technology bundle
  (`packs/technology/`), composed with `technology.knowledge`,
  `technology.standards`, `products.identities`, `news.articles`,
  `platform.entity-identity`, `platform.subscriptions`,
  `platform.source-acquisition` and the document revision store.

Tracking: [#1913](https://github.com/Ikey168/Noesis/issues/1913).

## Outcome

Given a software component, product or technical inventory entry, assemble the
advisories that name it: vulnerability records with revision history, affected
version ranges per ecosystem, weakness classification, dated exploitation
evidence and published scores, each with its source and as-of time visible.
Registration of an advisory, the vendor statement and exploitation evidence
stay distinct records. Conflicting assertions from different sources are kept
side by side, never merged, and a rejected or withdrawn CVE keeps its history.

## GitHub implementation issues

- [ ] [#1922](https://github.com/Ikey168/Noesis/issues/1922) — V01 Audit vulnerability and advisory source contracts and select bounded provider coverage.
- [ ] [#1928](https://github.com/Ikey168/Noesis/issues/1928) — V02 Define advisory, vulnerability, affected-version-range, weakness and exploitation-evidence records with revisions.
- [ ] [#1938](https://github.com/Ikey168/Noesis/issues/1938) — V03 Acquire NVD CVE records with CPE applicability statements and change history.
- [ ] [#1946](https://github.com/Ikey168/Noesis/issues/1946) — V04 Acquire OSV and GitHub Advisory Database advisories with ecosystem version ranges.
- [ ] [#1956](https://github.com/Ikey168/Noesis/issues/1956) — V05 Acquire CISA KEV entries and EPSS scores as dated exploitation and scoring evidence.
- [ ] [#1964](https://github.com/Ikey168/Noesis/issues/1964) — V06 Acquire CWE and CPE reference data as ontology modules through the schema registry.
- [ ] [#1975](https://github.com/Ikey168/Noesis/issues/1975) — V07 Normalise identifiers, version ranges and severity vectors across sources without merging conflicting assertions.
- [ ] [#1984](https://github.com/Ikey168/Noesis/issues/1984) — V08 Match advisories to technical inventory components and product records through reviewable component identity.
- [ ] [#1994](https://github.com/Ikey168/Noesis/issues/1994) — V09 Answer which inventory components are affected as of a date through the existing technical impact tools.
- [ ] [#2004](https://github.com/Ikey168/Noesis/issues/2004) — V10 Monitor new advisories, KEV additions, score changes and withdrawn CVEs through subscriptions.
- [ ] [#2012](https://github.com/Ikey168/Noesis/issues/2012) — V11 Register the `technology.vulnerabilities` provider descriptor and optional feature in the Technology bundle.
- [ ] [#2021](https://github.com/Ikey168/Noesis/issues/2021) — V12 Add offline component-to-advisory acceptance coverage.
- [ ] [#2025](https://github.com/Ikey168/Noesis/issues/2025) — V13 Validate live advisory coverage and publish a cited impact demo.

Order: V01 → V02 → {V03, V04, V05, V06} → V07 → V08 → V09 → V10 → V11 → V12 → V13.

## Sources

- [NVD CVE API 2.0](https://nvd.nist.gov/developers/vulnerabilities): CVE
  records, CPE applicability statements, CVSS metrics per publisher and change
  history; keyed and unkeyed rate limits differ.
- [OSV](https://osv.dev/docs/): open-source advisories with ordered ecosystem
  range events; the `osv-api` entry already exists in
  `config/source_packs/technical.json`.
- [CISA Known Exploited Vulnerabilities catalog](https://www.cisa.gov/known-exploited-vulnerabilities-catalog):
  dated catalog listings, recorded as exploitation evidence, not as a verdict.
- [GitHub Advisory Database](https://github.com/github/advisory-database):
  GHSA records in the OSV schema with CVE aliases and withdrawal markers.
- [FIRST EPSS API](https://www.first.org/epss/api): dated scores with model
  version and percentile; never averaged across dates or publishers.
- [NVD CPE dictionary](https://nvd.nist.gov/products/cpe): product names for a
  versioned ontology module and reviewable crosswalks to product identities.
- [CWE downloads](https://cwe.mitre.org/data/downloads.html): weakness
  definitions for a versioned ontology module.
- [CVE Services](https://www.cve.org/AllResources/CveServices): CVE
  registration records and rejected status.

V01 verifies the documented access method, terms, authentication, rate
limits, identifiers and cadence per provider and records an `unverified-live`
or `not-implemented` decision for each. These links establish source
candidates, not guaranteed APIs or complete live coverage.

## Expansion of existing capabilities

**Technology (host):** the bundle gains a `technology.vulnerabilities`
provider descriptor and an optional `vulnerabilities` feature that defaults
off. The record owner is `src/kb/vulnerabilities.py`; the OSV and CVE adapters
in `src/domains/technical/advisories.py` and the range logic in
`src/domains/technical/model.py` are extended, not duplicated. Technology keeps
working with the feature off.

**Technical knowledge and impact (`technology.knowledge`):** `assess_inventory`,
the impact report store and `kb_technical` read affected ranges from the new
store next to `technical_advisory_ranges`, cite the advisory revision and
range event behind each finding and answer as of a date. Findings stay
`affected`, `unaffected_under_assessed_ranges` or `unknown`; no priority order
or remediation is produced.

**Standards (`technology.standards`):** an advisory links to a standard
edition in `src/kb/standards.py` only through an explicit reference in the
advisory.

**Products (`products.identities`):** CPE vendor/product names are matched to
product records in `src/kb/products.py` through reviewable crosswalk decisions;
unmatched names stay as source strings.

**Entity identity (`platform.entity-identity`):** component and vendor matches
are recorded as identity decisions in `src/kb/entities.py` and
`entity_history.py`, with evidence, and can be undone. A naming collision
across ecosystems yields separate candidates.

**Source acquisition and revisions (`platform.source-acquisition`):** all
providers run through `src/ingestion/source_pack_runtime.py` with bounded
budgets and commit document revisions in `src/ingestion/revisions.py`;
re-acquisition adds revisions and never rewrites earlier ones.

**Subscriptions and news (`platform.subscriptions`, `news.articles`):** new
advisories, KEV additions, score changes and withdrawals are delivered through
the existing subscriptions, deduplicated and owner-authorised; news items that
mention a CVE are related context, not evidence.

**Schema registry and ontology:** CWE and CPE reference data are versioned
ontology modules published through `src/kb/ontology.py`, following the
`clinical-mesh` pattern.

**Rights and terms:** each provider's data licence and API terms are recorded
in the source entry; NVD API keys are credentials, never stored in the pack.

## V1 acceptance

- Pinned fixtures in the documented NVD 2.0, OSV, GHSA, KEV, EPSS, CWE and CPE
  shapes cover a CVE asserted by several sources, disagreeing version ranges,
  differently quoted CVSS vectors, an EPSS model-version change, a withdrawn
  advisory and a rejected CVE.
- Repeated acquisition is idempotent; every source's advisory keeps its own
  revision series and no assertion is merged across sources.
- A component matches an advisory only through canonical package coordinates,
  explicit identifiers or reviewed identity decisions; suggested matches stay
  candidates and decisions are reversible.
- The affected-as-of-a-date answer cites the advisory revision and range event
  behind each finding and reports `unknown` where data is missing.
- Monitoring is deduplicated, replay-safe and owner-authorised.
- Offline replay and bounded live checks are recorded separately per provider
  in `docs/development/vulnerability-evidence/`. The demo takes a fixture
  inventory to its cited advisories, ranges, weakness classification,
  exploitation evidence and scores, offline when live access is blocked.

## Deferred

Defer exploitability or risk scoring of any kind, remediation and patch
recommendations, SBOM generation, container or binary scanning, vendor PSIRT
feeds beyond the listed providers, unrestricted mirroring of the NVD or CWE
corpora and automatic application of identity crosswalks. Provider record
counts do not imply complete coverage of a component's vulnerabilities.

There is no standalone Vulnerability and supply-chain advisory evidence domain
manifest, source-pack manifest, enablement flag, or separate installation
lifecycle in this scope.
