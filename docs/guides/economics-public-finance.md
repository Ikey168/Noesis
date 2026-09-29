# Economics public finance guide

Tracking: #1909. The Economics bundle's optional `public-finance` feature
(default off, coexisting with the planned `economics.demographics` feature)
takes a budget line, programme or beneficiary to a cited budget dossier. The
dossier holds:

- the plan, supplementary-plan and outturn figures of each fiscal year in the
  source's own hierarchy, with every outturn vintage;
- the figures that changed between vintages;
- beneficiary payments with reviewable identity;
- audit findings as quoted passages;
- the budget acts and legislative dossiers a plan cites;
- the Berlin district a line belongs to;
- procurement award history, shown as context only.

Every figure carries its source revision, publication date, unit and
accounting basis. Nothing here forecasts a fiscal outcome or determines waste
or fraud, and no figures on different accounting bases are netted without a
cited method.

## Pieces

| Piece | Where |
| --- | --- |
| Source audit and access decisions. Bundeshaushalt, Berlin, EU FTS and Eurostat GFS are `unverified-live`. EU budget pages and automated Bundesrechnungshof access are `not-implemented`. IMF GFS is blocked on its reuse terms | `docs/roadmaps/economics-public-finance-source-audit.md`, `PROVIDER_CONTRACTS` in `src/ingestion/public_finance_sources.py` |
| Acquisition (the `public-finance` connector, one release per declared file) | `bundeshaushalt-open-data`, `berlin-haushalt`, `eu-financial-transparency-system`, `eurostat-government-finance` in `config/source_packs/economic.json` (1.2.0) |
| Record owner (`noesis-public-finance-record-v1`) | `src/kb/public_finance.py`: releases, budget lines, budget plans, revisioned figures and their vintages, beneficiary payments, audit findings |
| Government finance statistics | `src/kb/public_finance_gfs.py`: the Eurostat connector's JSON-stat path into the dataset store, one economic release snapshot per Eurostat update |
| Beneficiary identity | `src/kb/public_finance_identity.py`: the ownership identity state machine and entity identity decisions |
| Berlin districts | `src/kb/public_finance_places.py`: the `alkis_bezirke:bezirksgrenzen` features and `berlin-bezirk` places, matched by published code only |
| Acts, dossiers and award context | `src/kb/public_finance_links.py` |
| Comparisons and dossiers | `src/kb/public_finance_queries.py` |
| Monitors | `src/kb/public_finance_monitoring.py` (knowledge subscriptions) |
| MCP tools | `tools/knowledge_engine_mcp/public_finance.py` |
| Provider and feature | `packs/economics/providers/economics.public-finance.json` and `packs/economics/composition.json` (the `public-finance` feature and the `economics.budget-review` profile) |

## Journey

1. Enable the feature with a coordinator selection
   (`features=["public-finance"]` for the `economics` bundle). The bundle works
   unchanged without it.
2. Declare the files each source fetches in its manifest entry. Each document
   is an HTTPS file on the endpoint's host, with its amount columns and the
   figure kind each one carries (for example `Soll` is a plan or a numbered
   supplementary plan, and `Ist` is an outturn). For plan documents, also
   declare the citations printed on the plan: the act (`de-bgbl`,
   `de-be-gvbl`, `celex`, `eli`) and the bill (`de-drucksache`,
   `eu-procedure`). Each citation is given as `{scheme, identifier}`.
3. Run the sources through the source-pack runtime. A file becomes one
   release, and an unchanged file adds nothing. A figure that differs from the
   revision in force at the file's `Stand` date becomes a new revision, so a
   reversion to earlier figures is also a new revision. Every release that
   states a figure is kept as a vintage of it. A file with an undeclared amount
   column or amount type, or an unknown unit, is refused rather than partly
   read.
4. Record audit findings with `import_audit_findings`: the report, passage
   locators, quoted text and cited lines. A sheet that carries a verdict is
   refused.
5. Link plans with `link_budget_acts` and `link_budget_dossier`, which link
   only by exact citation or by the dossier's own identifier. Shared words are
   candidates for `review_public_finance_link`, and every review can be
   reverted.
6. Offer beneficiaries to identity with
   `propose_public_finance_identity_matches`. The candidates are ownership
   entities by VAT number within its issuing country, funding awards by grant
   reference, and canonical entities by name (similar-name, never acceptable).
   Offer procurement suppliers with `propose_public_finance_award_parties`.
   Review or revert with `review_public_finance_identity_match` and
   `revert_public_finance_identity_match`.
7. Use `compare_budget_line` and `budget_line_dossier` for a line, and
   `beneficiary_dossier` for a beneficiary. A difference is computed only on
   the same accounting basis and currency, or under a reconciliation method
   recorded with its citation (`record_public_finance_reconciliation`).
   Conflicting sources are flagged, and unknown amounts stay unknown.
8. Watch a line, programme or beneficiary with `create_public_finance_monitor`
   and `run_public_finance_monitor`. These report supplementary plans, outturn
   vintages, payment publications and audit findings, each naming the revision
   it supersedes.

## Evidence

- Offline: `tests/unit/domains/test_public_finance_acceptance.py` replays the
  pinned fixtures through the source-pack runtime. The fixtures are authored,
  and their lines, beneficiaries and values are fictional.
- Live: no provider has a dated live run yet (#2014), so every provider stays
  `unverified-live` or `not-implemented` in `public_finance_readiness`.
