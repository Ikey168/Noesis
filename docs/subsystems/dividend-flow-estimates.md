# Dividend-flow estimates along ownership chains

Noesis records who holds what (Corporate Ownership assertions) and what issuers
declare (Market corporate actions and filed XBRL facts). Dividend distributions
are the main public trace of capital income that reaches holders.
`estimate-dividend-flows` (#2891) combines the two records into **derived
estimates**. An estimate is never a reported figure. No source states how much
dividend a holder received, and this query does not claim to know that amount.

- Code: `src/domains/market/dividend_flows.py`
- Contract: `contracts/schemas/jsonschema/noesis-dividend-flow-estimate-v1.json`
- MCP tool: `noesis-knowledge-engine.estimate_dividend_flows`. It is bound by the
  `estimate-dividend-flows` operation of the `ownership.core` provider descriptor,
  in the `ownership.graph` capability.
- Source audit: [dividend-flow source audit](../development/dividend-flow-evidence/source-audit.md)

The operation sits on the Corporate Ownership side for a structural reason. It
reads the ownership graph, and Corporate Ownership may depend on Market.
Market may not depend on Corporate Ownership (see the BaFin feature note in
`packs/market/composition.json`). The arithmetic reuses the Market stores and
reads them only.

## Method `noesis-dividend-flow-lookthrough@1.0.0`

For one declared dividend and one stated ownership path from the issuer up to a
holder, `issuer ← h1 ← h2 ← … ← hk`:

```text
estimate = per-share dividend × shares of h1 in the issuer × Π share of h(j+1) in h(j)
```

**Inputs.** Every input is a cited record revision:

| Role | Store | What is used |
|---|---|---|
| `dividend` | Market corporate actions | `cash_dividend` actions with status `confirmed`/`corrected` and type `regular`/`special`. The cash amount and currency are used as declared. The record date is the holding date, or the ex-date when no record date is stated. Identical observations from several providers form one event that cites all of them. Disagreeing observations are excluded and listed. |
| `dividend` (fallback) | Market financial facts | Used only for an issuer with no corporate-action dividend in the window. The facts are `us-gaap:CommonStockDividendsPerShareDeclared` or `ifrs-full:DividendsRecognisedAsDistributionsToOwnersPerShare`. The period end is the holding date, and the whole period counts as the timing gap. Overlapping periods are never both used: the longer or later period is kept and the other is listed. |
| `holding` | Ownership records | Holding statements in force at the holding date: 13F positions in shares, Schedule 13D/13G percentages and share counts, WpHG § 33 voting-rights percentages, and PSC, ROE or BODS share bands or exact shares. |
| `next-report` | Ownership records | The same 13F manager's position in the first report period after the holding date, when that report is on record by the as-of date. |
| `shares-outstanding` | Market financial facts | Used only when a percentage must become shares. The fact is `us-gaap:CommonStockSharesOutstanding` or `ifrs-full:NumberOfSharesOutstanding`, taking the instant nearest the holding date. The gap in days is flagged. |
| `context` | Market financial facts | `PaymentsOfDividendsCommonStock` and `PaymentsOfDividends` are listed under `reported_context`, labelled `reported`, and never attributed to a holder. |

**Which statements count.** Only `shareholding`, WpHG § 33 `voting_rights`, and
13F `reported_holding` in shares (`SH`) carry a share of the dividend.

- WpHG § 38 instruments and the § 39 total are not used. Instruments carry no
  dividend.
- Principal amounts and put/call rows are not used.
- `direct_parent`, appointment and influence assertions state no share. They
  appear under `unknown_links` and are never followed.
- Statements published after the as-of date are ignored.
- Owner-scoped and natural-person holders are `withheld`. They are counted
  without names unless person-research mode (`NOESIS_PERSON_RESEARCH`) is on.

**Linking holdings to issuers.** An ownership entity is linked to a Market
issuer in one of two ways: through the `links` argument, which is
caller-stated, or through an exact CIK or LEI that both records carry. Anything
else appears under `unlinked_entities`. An issuer with several dividend-paying
securities is `share-class-ambiguous` unless the security is named.

**Combining statements.** Several statements of one holder–subject link are
combined into the **hull** of their intervals. They are never summed. The rows
of one 13F manager's report period are summed as that report's position.

**The walk.** The walk has an explicit `max_depth`, from 1 to 10 with a default
of 3. Cycles are listed with their path, and `truncated` says whether the bound
cut the walk short. An unkeyed holder ends its path, because its own holders
are unknown. Estimates are per path and per currency. They are never summed
across paths, because a parent's 13D can restate a subsidiary's shares. They
are never netted or converted across currencies either, because no exchange
rate is cited. `by_currency` only indexes the estimates.

### The interval

The interval gives **bounds** under the stated assumptions. It is not a
confidence interval. The low bound is rounded down and the high bound up to
0.01, and the point is rounded half-even. A point is given only when every
input is exact. A band never becomes a point. The interval reflects three
things:

1. **Holding bands.** A stated band, such as PSC "more than 50 % and less than
   75 %", is carried through as `[50, 75] %`. An open upper bound becomes
   100 %.
2. **Timing.** The holding is read as stated in force at the dividend's
   holding date. When the statement's own date differs, the regime rule
   decides, and `interval_covers.timing` says whether timing is bounded:

   | Regime | Publication threshold | Rule when the dates differ |
   |---|---|---|
   | BaFin WpHG § 33 | 3 % | Threshold bracket. Without a later notification, the holding stays inside the bracket of the 3/5/10/15/20/25/30/50/75 % thresholds that contains the stated value. |
   | UK PSC / ROE | 25 % | The stated band bounds the holding, because a change of band must be registered. |
   | BODS | publisher's own | A stated range bounds the holding. An exact share does not bound timing. |
   | Schedule 13D | 5 % | ±1 percentage point (Rule 13d-2(a): a change of 1 % or more requires an amendment). Shares scale with the percentage. |
   | Schedule 13G | 5 % | Not bounded in v1. |
   | Form 13F | none | The hull of this position and the next report's position, both cited. Without a next report, timing is not bounded. |

   A statement whose validity is stated as open and whose statement date is on
   or after the holding date covers the date, so no widening is needed.
   Timing that is not bounded is accepted for at most
   `max_unbounded_gap_days` (default 92). Beyond that the statement is
   `stale-holding` and no estimate is made. When the regime's notification
   window (BaFin 10 days, PSC 28 days, 13D 4 days) has not closed by the as-of
   date, the estimate carries the flag `notification-window-open`.
3. **Threshold censoring.** A holding below its regime's publication threshold
   is **never estimated** (`below-publication-threshold`). For example, a
   13G/A exit filing at 4.2 % is not estimated. A bracket whose lower bound sits
   at the publication threshold is flagged in
   `interval_covers.threshold_censoring`. Holders below a threshold are absent
   from the record, which means they are unknown, not zero.

A split, reverse split or stock dividend between the holding date and the
dividend date stops an estimate (`share-count-changed`).

### Labels, honesty envelope and guard

Every answer and every estimate carries the following:

- `label: "derived"` and `reported: false`;
- the repo's honesty envelope (`src/analytics/honesty.py`): `n` (the number of
  cited input revisions), `method` (`noesis-dividend-flow-lookthrough@1.0.0`)
  and `assumptions` (ids whose texts are in `assumption_texts`);
- `method_id` and `method_version`;
- the cited `inputs`, each with store, record id, revision, revision id and
  record hash;
- the per-link statements, with regime, timing and censoring;
- the interval and `flags`.

The declared dividends are listed separately under `dividends`, labelled
`reported`. `guard()` refuses any answer whose estimates are not labelled this
way, or that carry a reported-figure field (`amount`, `value`,
`reported_amount`). The JSON schema refuses `reported: true`.

`reproduce()` re-reads every cited revision, refuses it if its record hash
changed, and recomputes each estimate with the recorded parameters.
`export_bundle()` writes a `noesis-evidence-bundle-v1`. In that bundle the
answer is the root and each cited revision is one evidence object, and the
bundle passes the offline verifier.

### Assumptions

| Id | Statement |
|---|---|
| `class-share` | A holding's share of the class is its share of the dividend on that class. A WpHG § 33 voting-rights percentage is read as a share of the dividend-bearing class, with no adjustment for non-voting or treasury shares. |
| `look-through` | A look-through estimate attributes an intermediate holder's estimated receipt to its owners. It is not cash the owner received; that depends on the intermediate's own distributions. |
| `holding-date` | A holding is read as stated in force at the holding date. Gaps are handled per regime, as described above. |
| `gross` | Amounts are gross, before withholding tax, fees and conversion, and stay in the declared currency. |
| `shares-outstanding` | A percentage is converted to shares with the nearest cited shares-outstanding fact. Issuance and buybacks between the two dates are not modelled. |
| `statement-hull` | The statements of one link are combined into their hull and never summed. |
| `no-share-count-change` | No share-count action lies between the holding date and the dividend date (this is checked). |

## Worked example: the synthetic three-level chain

The fixture is `tests/unit/dividend_flow_harness.py`. All names and figures in
it are fictional. The query is `issuer = Exampla Dividend Corp`, as of
2026-06-30, with `max_depth = 3`.

| Input | As stated |
|---|---|
| Dividend (corporate action) | USD 0.50 per share, ex-date 2026-03-10, record date 2026-03-11, confirmed |
| Northwind Holdings Ltd (B) in A | Schedule 13D, event date 2026-02-20: 12.5 % = 5,000,000 shares |
| Fiktiva Family Holdings Ltd (C) in B | PSC: more than 50 % and less than 75 % of shares, open, register snapshot 2026-04-30 |
| Fiktiva Trust Company Ltd (D) in C | PSC: 75 % to 100 % of shares, open, register snapshot 2026-04-30 |

The estimate is built link by link:

1. **B in A, a direct holding.** The 13D's date is 19 days before the record
   date, so the ±1 pp rule applies. The share becomes 11.5 % to 13.5 %, and
   the share count scales to 4,600,000 to 5,400,000 shares around the stated
   point of 5,000,000. Multiplied by USD 0.50, that gives USD **2,300,000.00**
   to **2,700,000.00**, with a point of 2,500,000.00. The estimate has n = 2:
   the dividend and the 13D.
2. **C in B, a band.** The open PSC statement of 2026-04-30 covers the record
   date, so no timing widening is needed. The fraction is 0.50 to 0.75, which
   gives USD **1,150,000.00** to **2,025,000.00**. There is no point, because
   the input is a band. n = 3.
3. **D in C, a band.** The fraction is 0.75 to 1.00, which gives USD
   **862,500.00** to **2,025,000.00**. There is no point. n = 4.

The same calculation for D, written out:

```text
low  = 0.50 × 5,000,000 × 11.5/12.5 × 0.50 × 0.75 =   862,500.00
high = 0.50 × 5,000,000 × 13.5/12.5 × 0.75 × 1.00 = 2,025,000.00
```

**When an input is a band, the interval widens.** Suppose C's stake in B were
an exact 60 % (an open BODS statement). Then the C estimate would be USD
1,380,000.00 to 1,620,000.00, with a point of 1,500,000.00. That interval is
240,000.00 wide, against 875,000.00 for the PSC band.

The other holders of A show the remaining rules:

- **The 13F manager.** Its 2025-12-31 position is 800,000 shares, and the next
  report puts it at 900,000 shares. The estimate is USD 400,000.00 to
  450,000.00, timing bounded, with both reports cited. For the June dividend
  there is no later report, so timing is `not-bounded`.
- **The 13G trust.** It states 6.0 % with no share count. The cited 40,000,000
  shares-outstanding fact gives USD 1,200,000.00, timing `not-bounded`. For the
  June dividend the gap is 116 days, more than 92, so the holding is
  `stale-holding` and not estimated.
- **The 13G/A exit filing.** At 4.2 % it is `below-publication-threshold` and
  is never estimated.
- **The rest.** The natural-person PSC of C is withheld without a name. The
  GLEIF consolidation parent of D is an unknown link. The announced Q3 dividend
  is excluded.

With `holder = D`, the walk also reaches Fiktiv Ertrag AG. Northwind holds
4.2 % of its voting rights under WpHG § 33, a statement 125 days old. The
threshold bracket gives 3 % to 5 % of the cited 10,000,000 shares. With EUR
1.20 per share and both PSC bands, D's estimate is EUR 135,000.00 to
450,000.00. It is listed under `EUR` beside the USD estimates and is never
added to them. The § 38 instruments statement is listed as not used.

## Not implemented

| Source | Status |
|---|---|
| Bundesanzeiger dividend resolutions | `not-implemented` (no permitted machine access; see the source audit) |
| `dei:EntityCommonStockSharesOutstanding` | `not-implemented` (the CompanyFacts path does not index the `dei` taxonomy) |
| Schedule 13G timing bracket | Not bounded in v1 |
| Shares stated without a percentage on an upper link | Unknown in v1 |
| Netting across currencies | Never done: no cited exchange rate |

The query uses offline synthetic fixtures only. No live dividend or holdings
sample has been evaluated.

```bash
python -m pytest -q tests/unit/domains/test_dividend_flows.py tests/unit/domains/test_dividend_flows_mcp.py
```
