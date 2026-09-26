# Products pack scope

Status: implemented offline, 2026-09-26 (see `docs/guides/products-pack.md`).
Live acceptance for both providers is outstanding: no credentialed provider
run or verified cross-provider overlap is claimed.

Tracking: [#1706](https://github.com/Ikey168/Noesis/issues/1706).
Related Geospatial pack: [#1695](https://github.com/Ikey168/Noesis/issues/1695).
Reuse shared runtime improvements without making either pack depend on the
other pack's delivery. Audit existing product/company relationship work from
[#1674](https://github.com/Ikey168/Noesis/issues/1674) before designing new storage.

## Delivery state (2026-09-26)

Audited against the repository on 2026-09-26 (C09.1, [Ikey168/Noesis#1840](https://github.com/Ikey168/Noesis/issues/1840)). Every item below cites a file or tool that exists in this checkout; where this section and older text disagree, this section is current.

- **Shipped:** #1708–#1718 — family/model/variant identities (`src/kb/products.py`), Open Icecat and EPREL acquisition from fixtures (`src/ingestion/product_sources.py`, `config/source_packs/products.json`), reviewable matching, display normalization, revisions, refresh and withdrawal, datasheets, the pack and source profile, lookup and comparison, and offline acceptance. The earlier note that "no existing Icecat, EPREL, GTIN or product-catalog implementation was found" is out of date.
- **Partial:** #1719 — the three-model comparison exists offline; the live run was `blocked`/`failed` (`docs/development/products-evidence/live-check-build-environment.json`).
- **Not shipped:** #1707 — access and cross-source overlap validation (`"cross_source_overlap": "outstanding"`).
- **Composition:** `packs/products` is composition-managed (C09.3): it contributes the generated `noesis.products` provider, which owns the product records written by the `noesis-product-record-v1` projector.
- **Composition dependency of remaining work:** none. #1707 and #1719 need credentialed live runs.

## GitHub implementation issues

- [ ] [#1707](https://github.com/Ikey168/Noesis/issues/1707) — Validate Open Icecat and EPREL access and display-model overlap.
- [ ] [#1708](https://github.com/Ikey168/Noesis/issues/1708) — Define product family, model and variant identities.
- [ ] [#1709](https://github.com/Ikey168/Noesis/issues/1709) — Implement bounded native Open Icecat acquisition.
- [ ] [#1710](https://github.com/Ikey168/Noesis/issues/1710) — Implement bounded EPREL public API acquisition.
- [ ] [#1711](https://github.com/Ikey168/Noesis/issues/1711) — Add reviewable cross-provider product matching.
- [ ] [#1712](https://github.com/Ikey168/Noesis/issues/1712) — Normalize display specifications with comparable measurement semantics.
- [ ] [#1713](https://github.com/Ikey168/Noesis/issues/1713) — Persist acquired product revisions and evidence links.
- [ ] [#1714](https://github.com/Ikey168/Noesis/issues/1714) — Handle product refresh, withdrawal and partial catalogue coverage.
- [ ] [#1715](https://github.com/Ikey168/Noesis/issues/1715) — Acquire model-linked datasheets and product information sheets.
- [ ] [#1716](https://github.com/Ikey168/Noesis/issues/1716) — Package Products capabilities and the two-provider source profile.
- [ ] [#1717](https://github.com/Ikey168/Noesis/issues/1717) — Expose evidence-linked product lookup and comparison.
- [ ] [#1718](https://github.com/Ikey168/Noesis/issues/1718) — Add end-to-end offline Products pack acceptance coverage.
- [ ] [#1719](https://github.com/Ikey168/Noesis/issues/1719) — Validate live product sources and publish the comparison demo.

## Outcome

Find a physical product model, inspect source-linked specifications and
datasheets, and compare explicitly matched variants on compatible attributes.
The initial profile is EU-market electronic displays (monitors/TVs), subject
to confirming useful overlapping model coverage in the selected sources.

Personal knowledge remains in Modulo. Organizational knowledge and cultural
collections are not part of this proposal.

## Sources

| Source | Contribution | Access/readiness work |
| --- | --- | --- |
| Open Icecat | Brand-authorized product descriptions, specifications and datasheets | Confirm account/access mechanism, available fields and models, update behavior and content reuse terms for the Open catalogue. Do not assume Full Icecat coverage. |
| EPREL public API | Registered product models, energy-label parameters and product information sheets | Complete the public API access process and validate supported endpoints, quotas, terms and category-specific fields. Public website access does not imply an already usable API credential. |

Official sources:

- [Icecat structured product content](https://icecat.com/structured-data-content-users/)
  describes Open Icecat and XML/CSV/JSON/HTML delivery.
- [EPREL overview](https://energy-efficient-products.ec.europa.eu/eprel_en)
  describes model information and product groups including displays.
- [EPREL public API access](https://eprel.ec.europa.eu/screen/requestpublicapikey)
  confirms a programmatic public-data service and its application process.

These records represent provider or supplier assertions, not independent
product testing. Preserve that distinction in comparisons.

## Reuse and additions

Reuse Noesis source-pack scheduling, acquisition budgets, retry/quarantine,
source revision history, evidence references, document ingestion, units and
quantitative calculations, comparisons and report/intake mechanisms wherever
their existing contracts fit. This is intended reuse, not a claim that every
product-specific path is already wired.

New work:

1. Pin provider contracts and bounded reference fixtures for both sources.
2. Add native Open Icecat and EPREL acquisition through the existing runtime,
   with explicit credential/readiness status and bounded refresh.
3. Define product-family, model and variant identities. Preserve provider IDs,
   brand/manufacturer, model designation, market, and GTIN/MPN when supplied.
   Do not collapse regional variants, bundles or different sizes by title.
4. Preserve source-specific assertions and propose cross-source matches using
   identifiers and corroborating attributes. Ambiguous matches stay separate
   and reviewable; a similar name is insufficient to merge records.
5. Normalize a bounded display attribute set: diagonal, resolution, dimensions,
   and energy parameters where available. Retain native values, units, test
   conditions, label scheme/version, source location and observation time.
   Missing, conflicting and non-comparable values remain explicit.
6. Link permitted datasheet/product-information-sheet acquisition to exact
   model and source revisions using existing document processing. Separate
   metadata/link availability from permission and ability to retain an asset.
7. Add a thin Products domain manifest and source profile, readiness reporting,
   model lookup and a comparison view/report using existing surfaces.

No existing Icecat, EPREL, GTIN or product-catalog implementation was found in
the bounded repository search of src, config, packs and docs. A detailed
implementation audit should precede schema design.

## Acceptance

- A pinned fixture cohort exercises matched models, an ambiguous match,
  regional/size variants, missing attributes and conflicting specifications.
- Each comparison cell links to a source revision; category, units, operating
  mode and measurement basis are checked before numerical comparison.
- Repeat import is idempotent, source updates retain history, and absence from
  a partial catalogue response never implies discontinued status or deletion.
- Credentials, source coverage and optional acquisition failures are surfaced
  honestly. One available provider does not imply cross-provider validation.
- Offline source-pack and product workflow checks pass with expected reference
  results; live acceptance is a separate dated bounded run for each provider.
- Demonstration: compare three resolved display models and inspect the evidence
  supporting their specifications, energy parameters and any disagreements.

## Delivery order and limits

First validate access and cross-source model overlap; then implement identity
and acquisition, followed by normalization, comparison and pack acceptance.
If display overlap is inadequate, record that result and select a different
single category before expanding the data model.

Defer prices, stock, retailer feeds, shopping recommendations, affiliate links,
reviews, inferred compatibility, services, comprehensive lifecycle monitoring,
and unrestricted catalogue mirroring. Add manufacturer manuals/support sources
only as a later explicit source profile with named providers and access checks.

Primary uncertainties are API access, Open Icecat coverage, cross-provider
identity matching and comparable attribute semantics. Resolve these before
estimating calendar effort or asserting a percentage of implementation reuse.
