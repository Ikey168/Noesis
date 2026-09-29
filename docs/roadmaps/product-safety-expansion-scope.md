# Product safety notices and recalls expansion scope

Status: planned, 2026-09-27. An expansion of the existing Products bundle; it
does not create a separate domain or pack. It produces no safety verdict for a
product without a notice, never infers that a similar model is affected, and
gives no consumer advice beyond quoting the authority's corrective action.

## Delivery state (audited 2026-09-27)

- Shipped: nothing yet. Planning issues #1935–#2033 are open.
- Composition dependency: a `products.safety` provider descriptor and optional
  `safety` feature (default false) in the Products bundle (`packs/products/`),
  composed with `products.identities` (`products.core`), `technology.standards`,
  `legal.works`, `news.articles`, `platform.entity-identity`,
  `platform.subscriptions` and `platform.source-acquisition`.

Tracking: [#1916](https://github.com/Ikey168/Noesis/issues/1916).

## Outcome

Given a product, brand, model or GTIN, assemble the safety notices and recalls
that name it: hazard, affected-product identification, corrective action,
issuing authority, notice revisions and the standards and legal acts the notice
cites, each with source and as-of time. A notice is what an authority
published. Product matches are reviewable and reversible; a product with no
matching notice is reported as having none on record, not as safe.

## GitHub implementation issues

- [ ] [#1935](https://github.com/Ikey168/Noesis/issues/1935) — R01 Audit product-safety and recall source contracts and select bounded provider coverage.
- [ ] [#1950](https://github.com/Ikey168/Noesis/issues/1950) — R02 Define notice, hazard, affected-product-identification, corrective-action, issuing-authority and notice-revision records.
- [ ] [#1959](https://github.com/Ikey168/Noesis/issues/1959) — R03 Acquire EU Safety Gate alerts with revision history.
- [ ] [#1972](https://github.com/Ikey168/Noesis/issues/1972) — R04 Acquire CPSC and NHTSA recall notices.
- [ ] [#1979](https://github.com/Ikey168/Noesis/issues/1979) — R05 Acquire RASFF food and feed notifications.
- [ ] [#1990](https://github.com/Ikey168/Noesis/issues/1990) — R06 Match notices to product records through reviewable identity on GTIN, brand and model.
- [ ] [#2001](https://github.com/Ikey168/Noesis/issues/2001) — R07 Link notices to cited standards and legal acts by citation.
- [ ] [#2011](https://github.com/Ikey168/Noesis/issues/2011) — R08 Answer whether a product is subject to any notice as of a date with cited notice revisions.
- [ ] [#2020](https://github.com/Ikey168/Noesis/issues/2020) — R09 Monitor new notices and notice updates through subscriptions.
- [ ] [#2026](https://github.com/Ikey168/Noesis/issues/2026) — R10 Register the `products.safety` provider descriptor and optional feature in the Products bundle.
- [ ] [#2030](https://github.com/Ikey168/Noesis/issues/2030) — R11 Add offline product-to-notices acceptance coverage.
- [ ] [#2033](https://github.com/Ikey168/Noesis/issues/2033) — R12 Validate live coverage and publish a cited product-notice demo.

Order: R01 → R02 → {R03, R04, R05} → R06 → R07 → R08 → R09 → R10 → R11 → R12.

## Sources

- [EU Safety Gate alerts](https://ec.europa.eu/safety-gate-alerts/screen/webReport):
  weekly alerts on dangerous non-food products with follow-ups; the open-data
  or API access method is to be verified.
- [CPSC Recalls API](https://www.cpsc.gov/Recalls/CPSC-Recalls-Application-Program-Interface-API-Information):
  US consumer-product recalls with recall numbers, hazards and remedies.
- [NHTSA datasets and APIs](https://www.nhtsa.gov/nhtsa-datasets-and-apis):
  US vehicle and equipment recall campaigns by make, model and year.
- [RASFF Window](https://webgate.ec.europa.eu/rasff-window/screen/search):
  EU food and feed notifications with hazard, origin and distribution fields;
  machine access to be verified.
- [BAuA product recalls](https://www.baua.de/DE/Themen/Anwendungssichere-Chemikalien-und-Produkte/Produktsicherheit/Produktrueckrufe/):
  German recall pages; expected `not-implemented` unless supported machine
  access is documented, since nothing is scraped.
- [Regulation (EU) 2023/988 (GPSR)](https://eur-lex.europa.eu/eli/reg/2023/988):
  the legal act many notices cite, acquired through the existing CELLAR source
  of the `legal-research` pack.

R01 verifies documented access, terms, authentication, rate limits,
identifiers, revision semantics and cadence per provider and records an
`unverified-live` or `not-implemented` decision for each. These links establish
source candidates, not guaranteed APIs or complete live coverage.

## Expansion of existing capabilities

**Products (`products.core`, `products.identities`):** notices are their own
source-backed records in `src/kb/product_safety.py`, acquired by adapters in
`src/ingestion/product_sources.py` through new sources of the
`products-displays` source pack (`config/source_packs/products.json`).
Affected-product identification stays native (GTIN, model, batch, brand). A
notice attaches to a `product_identities` row only through a reviewable match
on GTIN or exact brand plus designation; ambiguous and contradicted candidates
stay separate, and a notice about one model never reaches its siblings, sizes
or regional variants.

**Standards (`technology.standards`, `src/kb/standards.py`):** references such
as `EN 60335-1` resolve to existing standard editions on an exact reference
match with `basis: cited`; unresolved references stay raw. Certificate-product
links are not notice evidence.

**Legal (`legal.works`, `src/kb/legal.py`):** cited acts (GPSR, the RASFF
basis in Regulation (EC) No 178/2002) resolve to `legal_works` on an exact
CELEX or ELI match; the acts themselves come from the existing CELLAR source by
adding CELEX numbers to its selection, not through a second legal store.

**News (`news.articles`):** articles that mention a notice number appear in a
dossier as reporting about the notice, labelled as such and never as the notice.

**Entity identity (`platform.entity-identity`, `src/kb/entity_history.py`):**
manufacturer and importer names resolve to `canonical_entities` through
recorded, reversible decisions; unmatched names stay as source strings.

**Subscriptions (`platform.subscriptions`, `src/kb/subscriptions.py`):** a
product-notice monitor is a knowledge subscription, as the procurement monitor
is; watermarks advance only after a complete run of the notice sources.

**Source acquisition (`platform.source-acquisition`,
`src/ingestion/source_pack_runtime.py`):** bounded explicit selections,
budgets, retries, quarantine, checkpoints, classified failures and revision
receipts are the runtime's; no crawl and no scraping.

**Rights and terms:** each source entry records its terms URL and reuse
conditions with "operator must confirm" where unverified. Authority text is
quoted, not paraphrased; retention of linked documents follows the existing
`product.documents` policy.

## V1 acceptance

- Authored fixtures for Safety Gate, CPSC, NHTSA and RASFF cover an updated
  alert with two revisions, two authorities naming one GTIN, a contradicted
  GTIN match, a sibling model with no notice, a reversed review and a crash
  between projection and checkpoint.
- Repeated ingestion is idempotent; every revision, match decision and
  citation link keeps its source revision and locator.
- `lookup_product_notices` answers a GTIN, brand plus model or Products model
  ID as of a date with each notice's hazard, verbatim corrective action,
  authority, cited standards and acts, and the match that connected it;
  conflicting notices are shown side by side, later revisions are named as
  later, and a product without a match returns `no notice on record`.
- No response field carries a safety verdict, risk score or recommendation.
- Products resolves with the `safety` feature off and on; v1
  `packs/products/pack.json` is unchanged.
- Offline replay and bounded live checks are recorded separately per provider;
  live evidence lives under `docs/development/product-safety-evidence/`.

## Deferred

Defer risk scoring, safety verdicts for products without a notice, inference
across similar models or product families, consumer or legal advice, retailer
and marketplace takedown feeds, injury and incident databases, manufacturer
press releases as a source, BAuA page scraping and any unrestricted mirroring
of authority databases. Notice counts never imply that an unlisted product is
safe or that a listed hazard applies beyond the identification the authority
published. There is no standalone Product safety notices and recalls domain
manifest, source-pack manifest, enablement flag, or separate installation
lifecycle in this scope.
