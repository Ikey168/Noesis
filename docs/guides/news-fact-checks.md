# News fact-checks: published ClaimReview records, publishers and ratings as published

The News pack's `news.fact-checks` provider (tracking issue #2659) adds the
fact-checks that publishers publish. Given a claim, a claimant, a news article
or a publisher, it returns the fact-checks with the claim as quoted, the
claimant as named and the rating as published. Each item carries its source,
record revision and as-of time, and the publisher's IFCN signatory status at
the review date. It fills the `fact-checks` subdomain of the domain coverage
program ([ADR-005](../architecture/decisions/ADR-005-domain-coverage-program.md))
with **offline** coverage. Live coverage is outstanding (#2722).

**Exclusions.** No truth verdicts by Noesis. No rating normalisation presented
as the publisher's: ratings are the publisher's own text, and any numeric value
is kept with its scale. When publishers disagree, their ratings are shown side
by side and never reconciled. No automatic claim matching without review. No
scraping beyond each publisher's terms. The older `src/argument_mining/factcheck.py`
verdict mapping is not used.

## Features and sources

Three independent optional features of the `news` bundle, all default off:

| Feature | Source (`bounded-public-osint` 1.2.0) | Provider | Records |
| --- | --- | --- | --- |
| `fact-checks-google` | `news-fact-checks-google` | Google Fact Check Tools API (`claims:search`, API key `NOESIS_GOOGLE_FACTCHECK_API_KEY`) | fact-checks for declared searches |
| `fact-checks-datacommons` | `news-fact-checks-datacommons` | Data Commons ClaimReview feed | fact-checks per release (vintage) |
| `fact-checks-ifcn` | `news-fact-checks-ifcn` | IFCN Code of Principles signatory listing | publishers with dated status revisions |

Each feature binds `news.fact-checks`, `platform.entity-identity`,
`platform.subscriptions` and `platform.source-runtime`. News articles, OSINT
corroboration, claim timelines, source identities and web archives are used
when present. When they are absent the answer degrades to an `unavailable`
report, so no feature requires them. Every source is `unverified-live`, and
the terms were not re-verified live: see the
[source audit](../development/fact-checks-evidence/source-audit.md).

## Records and revisions

- A **fact-check** is keyed by the publisher, the review URL and the reviewed
  claim (`fact-check:review:<domain>:<url digest>:<claim digest>`). A
  publisher's update (a new review date, a changed rating, a corrected claim) is
  a new revision.
- The same review from the API and from a Data Commons release has the same
  key. Each source keeps its own provenance chain, and answers show both.
- A **publisher** is keyed by its website domain. Each IFCN status (verified,
  expired, under review) is in force from the date the listing states.
- A review that a later release no longer carries, and a signatory missing from
  a later listing, get an `absent-from-release` or `absent-from-listing`
  revision. Nothing is deleted.

## Data minimisation (FC01)

Claimants keep only their name and published `@type`, plus Wikidata or ROR
identifiers. Images, job titles, contact details, birth dates and social
profiles are dropped by the parser, and so is a review's individual author.
Appearances on social platforms are stored as the host plus the SHA-256 of the
canonical URL. They match only when the caller supplies the same URL. The store
refuses any record that still carries a withheld field.

## Journey

1. Acquire through the source-pack runtime (`run_source_pack_execution` for
   `bounded-public-osint`, sources `news-fact-checks-*`), or re-read one source
   within its bounds with `FactCheckMonitor.refresh`.
2. `propose_fact_check_identity_matches` proposes candidates. Claimants are
   matched to canonical entities, published Wikidata id first and then the name
   as published. Fact-check claims are matched to `argument_claims`, by shared
   appearance URL or quoted-text overlap. Publishers are matched to source
   identities by published domain. A reviewer accepts or rejects each candidate
   with `review_fact_check_identity_match` and can revert the decision; nothing
   is automatic.
3. `link_fact_checks` links fact-checks to the news documents they cite
   (`wa-canon-v1` URL rules). Accepted matches also link claim timelines, OSINT
   corroboration and source identities. Missing targets are reported.
4. `fact_checks_of_claim_or_claimant` takes a `claim_id` or a `claimant` and an
   optional `as_of` publication cut-off. `known_at` restricts the answer to what
   had been acquired by then. The answer gives ratings verbatim, side by side,
   with the publisher's status at review time.
5. `fact_checks_citing_article` takes a `url` or a `document_id` and returns the
   fact-checks that cite it, the stated URL rule, and archived captures near the
   review date when the web-archive store is present.
6. `export_fact_check_evidence_bundle` cites every assertion with source,
   record revision and as-of time.
7. `create_fact_check_monitor` watches a claimant, a topic query or a
   publisher. `run_fact_check_monitor` notices cite the new and previous
   revisions: new fact-checks, rating and review-date changes, absences and
   IFCN status changes.

## Evidence

- Offline: `tests/unit/domains/test_fact_checks_acceptance.py` (the journey
  through the real runtime with sockets blocked) and the other
  `test_fact_checks_*` suites, over fictional fixtures in
  `tests/fixtures/fact_checks/`.
- Live: none yet (#2722). Offline coverage is not live coverage.
