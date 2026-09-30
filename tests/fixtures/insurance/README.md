# Insurance fixtures (authored, fictional)

These fixtures were authored for the Market `insurance` feature (#2230). Every insurer (Fiktiva
Versicherung Gruppe SE, Fiktiva Leben AG, Fiktiva Casualty Company), every event (Hurricane Fiktiva,
Hurricane Beispiel) and every figure is fictional. The files follow the documented shapes (IN01,
`docs/development/insurance-evidence/source-audit.md`), but none of them is captured data. None of the
numbers is an EIOPA, Florida OIR, NCEI, NAIC or PERILS figure.

* `eiopa_release_2025-06.csv` and `eiopa_release_2025-12.csv`: two releases. The German motor premium is
  revised and a confidential cell (`c`) stays a marker. One row lies outside the declared countries, one
  outside the declared years, and one holds an undeclared marker, which makes it a rejection.
* `sfcr_*.pdf`: SFCR extracts written with PyMuPDF by `python -m tests.unit.insurance_harness`: a group
  report, its correction, and a subsidiary's solo report. One declared cell is deliberately absent.
* `eiopa_production_fixture.csv`: rows for the user-assigned country code `ZZ`, served for the production
  EIOPA URL (as an XLSX workbook), so the production scope emits no record.
* `florida_oir_claims.csv`: three successive publications for one event.
* `ncei_billion_dollar.csv`: two economic estimates (CPI adjustments) for the same event.
* `perils_press_release.csv`: a release from a licensed publisher, which the adapter refuses to acquire.
* `naic_market_share.csv`: used only under a test declaration that simulates an in-scope decision. The
  recorded decision for NAIC is metadata-only.
