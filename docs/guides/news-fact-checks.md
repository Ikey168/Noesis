# News fact-checks: ClaimReview records, publishers and ratings as published

The News bundle's `news.fact-checks` provider (tracking issue #2659) adds the
fact-checks publishers publish: ClaimReview records from the Google Fact Check
Tools API and the Data Commons ClaimReview feed, and the IFCN Code of
Principles signatory list with its status history. Given a claim, a claimant, a
news article or a publisher, it returns the fact-checks publishers published,
with the claim as quoted, the claimant as named and the rating as published,
each citing its source, record revision and as-of time.

**Exclusions.** No truth verdict by Noesis, no rating normalisation presented as
the publisher's, no automatic claim matching without review, and no scraping
beyond each publisher's terms. A rating is the publisher's text and, where
published, its numeric value with the publisher's own best/worst scale.
Ratings from different publishers are shown side by side, never merged,
averaged or mapped onto a common scale. (The older lookup in
`src/argument_mining/factcheck.py`, which normalises verdicts, is not used.)

## Claimant data minimisation (FC01)

The binding decision is in `docs/development/fact-checks-evidence/source-audit.md`:

* stored: publisher, review URL, title, date and language; claim text as quoted;
  claimant as named, with the claimant type and `sameAs` identifiers the
  publisher published; claim date and appearance URLs; ratings as published;
* never stored: natural-person review authors, images and logos, claimant job
  titles and other personal attributes, appearance authors, article bodies. The
  parser drops them (listed under `minimisation.withheld`) and the store refuses
  any record that still carries them or any verdict/normalised-rating key;
* claimant searches, claimant matches and claimant monitors need
  `knowledge:news:fact-checks:claimant:read` in addition to
  `knowledge:news:fact-checks:read`.

## Sources and coverage

Three sources ship in `bounded-public-osint` 1.2.0
(`config/source_packs/osint.json`; the News bundle has no source pack of its
own), each `unverified-live` and declaring the minimisation policy. Selections
are placeholders until the live validation (#2722):

| Source | Provider | Records | Feature |
| --- | --- | --- | --- |
| `fact-checks-google-claim-search` | Google Fact Check Tools `claims:search` | fact-checks per (publisher site, review URL) for declared queries or publisher sites | `fact-checks-google` |
| `fact-checks-datacommons-feed` | Data Commons ClaimReview `DataFeed` | fact-checks per release (a vintage), filtered to declared sites and a review window | `fact-checks-datacommons` |
| `fact-checks-ifcn-signatories` | IFCN signatories listing | publisher records with status label and date as published | `fact-checks-ifcn` |

The Google source needs the secret `NOESIS_GOOGLE_FACTCHECK_API_KEY` (sent as a
header, never in a URL or receipt). The IFCN listing refuses a live fetch until
an operator adds `fact_checks.terms_confirmation` to the source after reading
the site's terms.

## Records and revisions

`src/kb/fact_checks_records.py` keeps one record per source and key with an
append-only revision log:

* a fact-check is keyed by publisher site and canonical review URL (`wa-canon-v1`
  rules shared with the web-archives provider), so the same review from Google
  and Data Commons is one key under two sources with both provenance records;
* a publisher's update (review date, rating, claims) is a new revision; a
  replayed older response adds nothing; an older observation never becomes
  current;
* an item missing from a later complete release (Data Commons) or listing (IFCN)
  gets an `absent-from-*` revision, never a deletion. A Google search result that
  stops appearing is not a removal;
* `as_of(record_key, day)` returns, per source, the revision published by that
  day.

## Reviewable identity and links

`src/kb/fact_checks_identity.py` proposes match assertions with method,
evidence and confidence; a reviewer accepts, rejects or reverts them and nothing
is merged:

| Kind | Target | Methods (strongest first) |
| --- | --- | --- |
| `claimant-entity` | `canonical_entities` | `published-identifier` (a `sameAs` such as a Wikidata QID is an entity alias), `alias-name` |
| `claim-argument` | `argument_claims` | `appearance-url`, `review-url` (legacy stored lookup), `lexical-overlap` (from `src/kb/claim_links.py`) |
| `publisher-source` | source identities (`src/kb/source_identity.py`) | `published-domain` (a reviewed domain alias) |

Accepted claimant and publisher matches are also `EntityHistoryStore` decisions;
generic claimants are never proposed. `src/kb/fact_checks_links.py` then links
current fact-check revisions to news documents they cite (by URL), and through
accepted claim matches to argument claims, OSINT corroboration results
(referenced, grades not copied) and claim-timeline states. Missing providers and
targets are reported.

## Questions answered

* **Fact-checks of a claim** (`fact_checks_for_claim`): by argument claim id
  (accepted matches only; unreviewed candidates listed apart) or by a text
  search over quoted claims (labelled a search). As of a date, with each rating
  verbatim, conflicting ratings side by side and the publisher's IFCN status in
  effect on the review date.
* **Fact-checks of a claimant** (`fact_checks_for_claimant`): as named, or every
  claimant accepted as matching a canonical entity; needs the claimant scope.
* **Fact-checks citing an article** (`fact_checks_citing_url`): by URL or news
  document id, matched with the stated URL rules, with archived captures when
  the web-archives provider holds them.
* **Evidence bundle** (`export_fact_checks_evidence_bundle`): every assertion
  quotes the rating and cites the fact-check revision, source and observation
  time.

## Monitors

`create_fact_checks_monitor` watches a claimant, a topic query or a publisher
site through `platform.subscriptions`; notices (`fact_check_published`,
`fact_check_revised`, `fact_check_withdrawn`, `publisher_listed`,
`publisher_status_changed`) cite the new and previous revision and state what
changed. No new scheduler: the source-pack schedule acquires and the maintenance
orchestrator commits watermarks.

## Enabling

Select any of the News bundle features `fact-checks-google`,
`fact-checks-datacommons` and `fact-checks-ifcn` (default off). Each binds
`news.fact-checks`, `platform.entity-identity`, `platform.subscriptions` and
`platform.source-runtime`; news documents, OSINT corroboration, claim timelines
and web-archive captures are used when present and reported as unavailable when
absent. Offline acceptance: `tests/unit/domains/test_fact_checks_acceptance.py`.
