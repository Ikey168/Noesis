# Society: wealth distribution

The Society bundle (`packs/society/`) gains the provider `society.wealth`
(issue #2882, taxonomy subdomain `income-poverty-inequality`) behind three
optional features, one per source: `wid`, `dwa` and `dfa` (all default off).
It answers: *given a country and a date, what wealth distribution figures did
the World Inequality Database, the ECB and the Federal Reserve publish, under
which wealth concept, group, population unit, method and release, as of that
date?*

It quotes what each source released. It never nowcasts, fills or interpolates
a period a source did not publish, blends WID, DWA, DFA, HFCS and PHF figures into one
series, re-harmonises wealth concepts, population units or percentile groups,
computes a top share, Pareto fit, ratio or average of its own, corrects a
publisher's model-based estimate or makes a forecast.

## Sources

Five sources in the `society-wealth-distribution` source pack (1.0.0,
`config/source_packs/society-wealth-distribution.json`), connector
`wealth-distribution`, all `unverified-live` until a dated live run. Access,
terms, the revision model, the minimisation decision and the bounded coverage
are in the [source audit](../development/wealth-distribution-evidence/source-audit.md).
It was written without access to the publishers, so items marked _verify_
(hosts, paths, response shapes, dimension and column names, licences) were not
checked live.

| Source | Publisher | Bounded first coverage | Access |
| --- | --- | --- | --- |
| `wid-wealth-distribution` | World Inequality Database (WID API, JSON) | Germany, France, United States; top 1%, 0.1% and 0.01% net personal wealth shares; national and private wealth-to-income ratios; 1 document, 40 series | key from the secret reference `NOESIS_WID_API_KEY`, sent in a header, never stored |
| `ecb-distributional-wealth-accounts` | ECB Distributional Wealth Accounts (ECB Data Portal, `format=csvdata`) | Germany, France; household net wealth shares of the top 10% and bottom 50%, median net wealth; quarterly; 1 document, 20 series | no key |
| `fed-distributional-financial-accounts` | Federal Reserve Distributional Financial Accounts (one member of the DFA archive) | United States; net worth shares of the top 0.1%, 99-99.9, 90-99, 50-90 and bottom 50%; quarterly; 1 document, 20 series | no key |
| `ecb-hfcs-statistical-tables` | ECB HFCS Statistical Tables (PDF, per wave and data version) | Germany, Spain, France, Italy; median and mean net wealth, top 10% share; one table per survey wave; 1 PDF, at most 6 tables and 80 cells | no key |
| `bundesbank-phf-aggregates` | Bundesbank household wealth survey data annex (PDF) | Germany; mean and median net wealth, top 10% share, one column per survey wave; 1 PDF, at most 6 tables and 40 cells | no key |

The HFCS and PHF figures exist only as PDF tables. They are read at the
declared table anchor, row labels and printed columns only; each value keeps
its printed token, page, table and quoted line, and a cell that cannot be read
exactly fails the document instead of being guessed (see WD02 in the source
audit). Reading them needs PyMuPDF (the `optional-sources` extra); without it
the source reports `source_unavailable`. Their frequency is `irregular` (survey
waves). HFCS, PHF and SCF microdata are excluded.

## Records and vintages

`noesis-wealth-distribution-record-v2` (`src/kb/wealth_distribution_records.py`,
`src/kb/wealth_distribution_store.py`). A **series** is keyed by source,
dataset, indicator (`wealth_share`, `wealth_level`, `wealth_average`,
`wealth_median`, `wealth_income_ratio`), wealth concept, wealth group, unit,
population or unit of analysis, method, place (ISO 3166-1 alpha-2) and
frequency, each with the code as published. The definition and the method
statement the source states are definition revisions; a changed method
statement (a new WID DINA method, for example) is a new revision.

Each value keeps its text as published (WID shares stay fractions of 1, DWA
and DFA shares stay percent), its cell status (`reported`, `confidential`,
`not_published`), the publication status the source states (`estimated`,
`provisional`, `extrapolated`) and its flags verbatim (`OBS_STATUS`,
`OBS_CONF`, WID's extrapolation ranges, a PDF cell's footnote flag). WID, DWA and
DFA are model-based, HFCS and PHF are the surveys' own estimates, and every row
says which (`model_based`). Each changed release is an appended
**vintage** dated by the declared release (a WID update, a DWA release, a Z.1
release), else the retrieval time labelled `retrieval_time`; a series a
complete later release no longer states gets a `removed_by_source` vintage.
Values also live in the Economics series storage (domain `society`).

## The journey and the tools

1. Acquire through the shared source-pack tools (pack
   `society-wealth-distribution`). A failed document (HTTP error, redirect to
   another host, schema drift, a missing archive member, an over-budget
   response) fails that source's run with its code and a receipt; earlier
   vintages stay current and `wealth_readiness` reports the source `stale`.
2. `wealth_indicator_for_place` returns each selected source's values for a
   place (`{"scheme": "iso3166-1-alpha2", "code": "DE"}`) as released by the
   date, optionally filtered by indicator and groups as published. Rows are
   never combined. Every pair of rows from different sources states
   `different_indicator`, the notes that relate them (a source-stated scope
   note, a reviewer-recorded note) or `comparability_unknown`.
3. `wealth_series_history` lists every vintage with new, revised and dropped
   periods, model estimates the source later replaced, method and definition
   changes and removals, and per release pair the notes that apply, else
   `comparability_unknown`.
4. `record_wealth_comparability` proposes a cited note for a series or a pair;
   `review_wealth_comparability` accepts, rejects or reverts it as another
   principal; `wealth_comparability_notes` lists them.
5. `wealth_profile` gathers a place's figures; `export_wealth_profile` returns
   a `noesis-evidence-bundle-v1` citing source, record revision and as-of time
   per item.

Every answer declares the exclusions and is checked against the minimisation
decision before it leaves. Reads need `knowledge:wealth:read` and namespace
access; writes `knowledge:wealth:write`; reviews `knowledge:wealth:review`.

## What is not live

Nothing here has been run against the publishers: their hosts did not resolve
from the cloud runtime. Offline evidence is
`tests/unit/domains/test_wealth_distribution_*.py`, the acceptance journey
`test_wealth_distribution_acceptance.py` and
`tests/unit/composition/test_society_wealth_composition.py`, on authored
fixtures with synthetic values (reference periods 2093-2098, releases in
2098-2099). Offline coverage is not live coverage.
