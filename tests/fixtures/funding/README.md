# Funding & Grants provider fixtures

**Authored fixtures, not live captures.** Every file here was written by hand to
mirror the page/response *shapes* that the adapters in
`src/ingestion/funding_providers.py` target. Names, identifiers, dates and
amounts are illustrative (EU topic identifiers carry a `FIXTURE` marker) and are
**not** statements about any currently open call. They exist to exercise the
parsers, normalization, eligibility, ranking, workspace, drafting and monitoring
code paths offline and deterministically.

Live coverage is validated separately by `scripts/funding_live_check.py`, whose
output records the provider, timestamp and failures apart from this offline
evidence.

| File | Provider shape | Used for |
| --- | --- | --- |
| `nlnet_propose.html` | NLnet `/propose/` page | current fund rounds, deadline with CEST, amount range, licence rule |
| `nlnet_propose_amended.html` | same page after an amendment | deadline shift + changed licence text |
| `nlnet_propose_broken.html` | page without the fund selector | partial fetch → stale, not closed |
| `nlnet_commonsfund.html` | NLnet fund page | programme record, programme budget vs award size |
| `eu_search_page1.json`, `eu_search_page2.json` | F&T portal search results | pagination, open/forthcoming/closed, two-stage, tender split |
| `eu_topic_open.json`, `eu_topic_open_amended.json` | F&T `topicDetails` | authoritative conditions, consortium rule, deadline amendment |
| `fdb_exist_gruendungsstipendium.html` | Förderdatenbank programme page | directory entry → EXIST reference, grant |
| `fdb_startgeld_loan.html` | Förderdatenbank programme page | loan instrument |
| `fdb_results.html` | Förderdatenbank result list | duplicate listings, stale link |
| `exist_gruendungsstipendium.html` | EXIST programme page | stipend per person-month, affiliation/team/not-founded rules, rolling |
| `exist_forschungstransfer.html` | EXIST programme variant | undated deadlines stay unknown |
