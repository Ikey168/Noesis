# Market insurance: supervisory statistics, SFCR figures and catastrophe-loss estimates

The Market pack's optional `insurance` feature (tracking issue #2230) is off by default. For an insurer,
a market or a catastrophe event, it returns the figures publishers released: EIOPA supervisory
statistics, the figures an insurer quotes in its SFCR, and published catastrophe-loss estimates. Each
figure comes with its revision history, its as-of time and a citation.

It does **not** assess solvency or ratings, model losses, or compute any ratio, reconciliation or
aggregate across publishers. Figures are answered exactly as each publisher published them.

## Sources and licence decisions

The audit (`docs/development/insurance-evidence/source-audit.md`, IN01) records one decision per source
or publisher. Every answer repeats these decisions in its `coverage` list.

| Source | Decision | What is stored |
| --- | --- | --- |
| EIOPA insurance statistics | in scope | supervisory indicators per release vintage; confidential cells keep their marker |
| Insurers' SFCR reports (Allianz SE group, 2023–2024) | in scope | quoted QRT cells with template, row and column; unreadable cells are `unknown` |
| NAIC public reports | metadata-only | publication references (title, year, URL, digest), without figures |
| NAIC licensed products, PERILS, Verisk PCS | excluded | nothing; a declaration naming one is refused (`licence_excluded`) |
| Florida OIR claims data | in scope | insured-loss estimates, one revision per report date |
| NOAA NCEI Billion-Dollar Disasters | in scope (archive) | economic cost estimates, one revision per publication |
| Swiss Re sigma, Munich Re NatCatSERVICE, ICA, broker reports | metadata-only | publication references only |

Every source is `unverified-live` until a dated live run is recorded (IN13, #2569).

## Enable and acquire

1. Select the feature: `market` with `features: ["insurance"]` in the composition plan. It binds
   `market.insurance`, `market.lei`, `platform.entity-identity`, `platform.subscriptions` and the
   source runtime.
2. Install and run the `insurance-supervisory-and-catastrophe-losses` source pack
   (`config/source_packs/market-insurance.json`) with the shared source-pack tools. Verify each
   declared URL, sheet name and header first; the scope texts say what to check.

## Answer questions (MCP server `noesis-market`)

| Tool | Answers |
| --- | --- |
| `insurance_insurer_as_of(namespace, insurer, as_of, lei_namespace?)` | an insurer's SFCR figures and insurer-level statistics as of a date; group and solo reports stay apart, and a group report appears beside a subsidiary only through a GLEIF parent relationship |
| `insurance_market_as_of(namespace, country, as_of, indicator?, line_of_business?)` | supervisory figures per publisher, with definition, currency, period, release and history |
| `insurance_event_estimates(namespace, event, as_of)` | every publisher's estimate series for an event, listed separately and never merged, with the revision in force and all revisions known then |
| `insurance_revision_history(namespace, record_id, as_of?)` | the full revision history of one estimate, figure or report |
| `insurance_coverage()` | the licence decisions and the live-verification status |

`as_of` is a date. A publication date counts as the end of that day (UTC). If the source states none,
the first observation time counts instead. `acquired_by_ms` additionally limits an answer to what had
been acquired by then (the Market `public_and_acquired` policy). An insurer, market or event with no
record returns `status: none_on_record`.

## Identity and links

* **Insurer identity.** A published LEI or NAIC company code matches exactly, with no review needed.
  `propose_insurance_identity_matches` offers name + jurisdiction candidates against ownership
  entities. You then review them with `review_insurance_identity_match`. A similar name alone can
  never be accepted, and rejected or reverted candidates are never used.
* **Links to events.** `link_insurance_loss_estimates(namespace, hazard_namespace)` links estimates
  to Natural Hazards events. A published identifier (NHC storm ID, GLIDE, USGS ID) or an explicit
  citation creates a link. A name and date match only creates a candidate, which you settle with
  `review_insurance_event_link`. Without hazard records, the call reports `none_on_record`.

## Monitors

`create_insurance_monitor(namespace, request_key, watch, target)` watches one of three targets:

* an `insurer` (LEI, NAIC code or party key);
* a `market` (a country);
* an `event` (a name, an identifier or a hazard record id).

The monitor is a knowledge subscription. It is evaluated by `run_insurance_monitor` at the watermarks
that source-pack runs commit, so it needs no scheduler of its own. It emits these events, each with a
receipt that cites the revision:

* `new_vintage`
* `new_insurer_report` or `corrected_insurer_report`
* `new_loss_estimate_revision`
* `identity_match_change`

An unchanged publication emits nothing.

## Offline evidence

`tests/unit/domains/test_insurance_acceptance.py` runs the journey with sockets disabled, on authored
fictional fixtures (`tests/fixtures/insurance/`). It goes from an insurer and from an event to cited
statistics, SFCR figures and loss-estimate revisions. It also replays the production pack through
the runtime. `python -m tests.unit.insurance_harness` regenerates the PDF fixtures and re-pins the
production fixture hashes.
