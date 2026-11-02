# Economics tax transparency guide

The Economics pack's `economics.tax` provider (#2889) answers two questions:
what did the OECD and the EU Tax Observatory publish about a jurisdiction's
corporate taxation, offshore wealth and billionaire tax rates, edition by
edition, as of a date; and which public country-by-country reports (public
CbCR) has a company published? Each source is its own optional feature, all off
by default:

| Feature | Source | Values |
| --- | --- | --- |
| `tax-oecd-cbcr` | `oecd-cts-cbcr`: OECD Corporate Tax Statistics, anonymised and aggregated CbCR statistics | published statistics |
| `tax-oecd-rates` | `oecd-cts-rates`: statutory corporate income tax rates and forward-looking effective tax rates | statutory rates: published statistics; effective rates: model estimates |
| `tax-eutax-offshore-wealth` | `eutax-offshore-wealth`: EU Tax Observatory, Atlas of the Offshore World | model estimates |
| `tax-eutax-billionaire-etr` | `eutax-billionaire-etr`: EU Tax Observatory, Global Tax Evasion Report | model estimates |
| `tax-public-cbcr` | `public-cbcr`: reports under Directive (EU) 2021/2101 | link-only references, no figures |

Figures are stored as published, each edition a vintage. **Model estimates keep
their method and edition, carry the status `estimated`, are answered in their
own section and are never presented as observations**; they are not written to
the Economics observation storage. Nothing computes an effective tax rate, a
ratio, a share, a rank or a score, blends sources, fills a jurisdiction or year,
converts a currency or forecasts.

Not implemented: the Tax Justice Network's Corporate Tax Haven Index and
Financial Secrecy Index. Their data portal is registration-gated and free only
for non-commercial use; commercial use needs a paid licence. Questions about
them answer `not-implemented`, never an empty result.

Source contracts, licences (the EU Tax Observatory licence is **not
established**), the minimisation decision and the bounded coverage are in the
[source audit](../development/tax-transparency-evidence/source-audit.md). It
was written without live access to the publishers; every source is
`unverified-live` until a dated run verifies it.

## Enable it

Select any of the features above in the Economics bundle composition, for
example `features: ["tax-oecd-rates", "tax-public-cbcr"]`. Each adds
`economics.tax`; the two OECD features also bind `economics.core` (published
statistics live in the Economics series storage), the four source features bind
`platform.source-runtime`, and `tax-public-cbcr` binds
`platform.entity-identity`. The sources ship in the separate source pack
`economic-tax-transparency` 1.0.0
(`config/source_packs/economic-tax-transparency.json`); the Economics bundle's
other source-pack pins do not change.

## Acquire

Install the source pack, accept each source's terms and run its sources through
the source-pack runtime (`noesis-tax-transparency-record-v2` pages are projected
by `src.kb.tax_store.TaxProjector`). The EU Tax Observatory sources state that
their licence is not established: verify the terms before accepting them. Each
declared document is one edition:

- a dataset from the audited list (`DSD_CBCR@DF_CBCRI`, `DSD_TAX_CIT@DF_CIT`,
  `DSD_ETR@DF_ETR`, `atlas-offshore-financial-wealth`,
  `gter-billionaire-effective-tax-rates`; every id is _verify_);
- the edition label and its publication date;
- the CSV column roles (long form: jurisdiction, period, indicator,
  `obs_value`, unit, status letters; wide form: one value column per
  indicator), the declared ISO 3166 alpha-3 jurisdictions and indicators and at
  most five annual periods;
- for model estimates, the method (label, description, https reference).

An edition is never truncated (`budget_exhausted`), an identical re-publication
adds no vintage, an edition dated before a series' latest vintage is refused
(`stale_release`) and a failed document leaves earlier editions current and the
source reads as stale.

Public CbCR references are recorded by an operator with
`record_tax_company_reports`: company name as published, identifiers the report
states (for example an LEI), financial year, https report URL, publication date
and channel (`company-website` or `business-register`). A reference carrying a
profit, tax, revenue or employee figure is refused (`reference_only`); a changed
reference is a new revision.

## Ask

- `tax_indicator_for_jurisdiction(namespace, jurisdiction="NLD",
  as_of="2025-03-31")` returns one row per series from the edition published by
  that date, with the edition, definition (CbCR caveats included), status
  letters, confidential cells as their status, vintage and citation. Published
  statistics and model estimates are separate sections; each estimate row
  carries its method and `observation: false`.
- `tax_series_history(namespace, series_id)` lists every edition with the
  periods it added, revised or dropped, method and definition changes and
  removals by the source.
- `export_tax_evidence_bundle(namespace, jurisdiction)` returns assertions that
  each cite source, edition, record revision and as-of time.
- `tax_company_reports_for_entity(namespace, ownership_key)` returns the public
  CbCR references of an ownership record or entity reached through **accepted**
  identity decisions only.

## Review company identity

`propose_tax_company_matches(namespace, ownership_namespace)` offers candidates
between public CbCR references and ownership legal entities: published
identifiers first (`exact-identifier`), equal names only as low evidence
(`name-jurisdiction`). Nothing is accepted automatically.
`review_tax_company_match` accepts or rejects with a reason (an entity identity
decision), `revert_tax_company_match` undoes it, and
`list_tax_company_matches` shows candidates and the references still unmatched.
Reference keys start with `tax-cbcr:`, so these decisions never regroup
ownership entities. Without ownership records the proposal answers
`ownership_absent`.

## Evidence

- Offline acceptance: `tests/unit/domains/test_tax_acceptance.py` (sockets
  blocked; fictional reference years 2091-2093, editions dated 2024 and 2025).
- Covered offline only; no live run exists, and offline coverage is never
  reported as live coverage.
