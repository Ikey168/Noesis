# Regulatory enforcement evidence

Evidence for the Legal pack's `legal.enforcement` provider (#2651) and its
optional features `enforcement-sec`, `enforcement-fca`, `enforcement-epa` and
`enforcement-edpb`: SEC litigation releases and administrative proceedings,
FCA final notices, EPA ECHO enforcement cases and EDPB Article 60 final
decisions.

* `source-audit.md` - EN01 (#2655) source contracts, licence and reuse terms,
  rate limits, revision models, the natural-person minimisation decision,
  bounded coverage and `LIVE_VERIFICATION` per provider. The official pages
  could not be fetched from the authoring runtime; the audit cites search
  snippets with their date and marks every such point _verify_.
* Offline evidence: `tests/unit/domains/test_enforcement_*.py` over authored
  fixtures in `tests/fixtures/enforcement/` and the pinned source-pack
  fixtures `tests/fixtures/source_packs/legal-enforcement-*.json`. Every
  action, respondent, identifier and figure there is fictional (the `Exampla`
  group of the ownership fixtures, years 2097-2099; SEC `LR-99901` and
  `34-99902`, FCA `exampla-uk-limited-2099` and
  `northwind-payments-limited-2099`, ECHO `04-2099-0101`, EDPB `99901` and
  `99902`). The FCA PDFs are authored with a simple text layer.
* Live evidence: none yet. Every provider is `unverified-live`; the dated live
  run and the cited demo belong to EN14 (#2720) and are recorded here,
  separately from the offline evidence, when they exist.
