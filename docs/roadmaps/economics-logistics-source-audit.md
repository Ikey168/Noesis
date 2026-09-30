# Shipping and logistics: source audit, freight-index licence decisions and bounded coverage (SL01)

Tracking: #2229 · delivery issue #2527 · recorded 2026-09-30.

This audit sets out, per source, what the Economics bundle's optional
`logistics` feature may acquire, how, and on what terms. It was written without
network access. Endpoints, parameters, column names and terms come from the
providers' published documentation as the author knows it. **Every item marked
_verify_ must be checked against the live documentation, the live terms and a
real response before the first dated live run (SL13, #2553). No provider is
`live` until that run exists.** The machine-readable copy of these decisions is
`PROVIDER_CONTRACTS`, `FREIGHT_INDEX_DECISIONS`, `LIVE_VERIFICATION` and
`BOUNDED_COVERAGE` in `src/ingestion/logistics_sources.py`; the MCP tool
`logistics_source_contracts` returns them, and every source entry of the
separate source pack `config/source_packs/economic-logistics.json`
(`economic-shipping-and-logistics` 1.0.0) carries its
`logistics.live_verification` status.

The sources live in their own economics-domain source pack rather than in
`config/source_packs/economic.json`, so that the Economics bundle's pin on
`economic-statistics-and-filings` is not changed by this feature; the bundle pins
both packs.

Non-goals for every source: values are stored as each publisher released them.
Series of different sources for a similar concept are shown side by side and
never merged; nothing forecasts freight rates, derives, rebases, chains or
interpolates an index, infers a port-to-port route from port totals or derives a
trade-per-throughput ratio. Commercial freight indices are not redistributed
without a licence.

## Access decisions

| Source | Delivers | Decision (`LIVE_VERIFICATION`) | Reason |
| --- | --- | --- | --- |
| UN/LOCODE (service.unece.org) | code-list releases: ports and other locations | `unverified-live` | Public release files without authentication. File names (`loc{yy}{n}csv.zip`), the CSV parts, column order and ISO-8859-1 encoding are _verify_ |
| UNCTADstat (unctadstat-api.unctad.org) | port calls and time in port, container port throughput, liner shipping connectivity (country and port), merchant fleet by flag | `unverified-live` | Bulk downloads need no key but are documented as 7z archives (_verify_), which the runtime does not unpack (no 7z dependency); CSV and zip bodies are read, a 7z body is refused with `unsupported_archive`, and the operator imports the extracted CSV with `src.kb.logistics_series.operator_import` (evidence origin `operator`). The registered data API (client id and secret) is not used; no SDMX endpoint of UNCTADstat could be confirmed, so the SDMX connector is not used (_verify_) |
| Eurostat maritime transport (ec.europa.eu) | goods and passengers by main port, vessel traffic, port-to-partner-port flows | `unverified-live` | Anonymous dissemination API through the existing `EurostatConnector` (`dataset_url` for datasets keyed by `rep_mar` instead of `geo`, `parse_cells` so that no dimension is collapsed). Dataset codes and the `rep_mar`/`par_mar` code lists are _verify_ |
| BLS PPI deep sea freight (api.bls.gov) | the producer price index for deep sea freight transportation (NAICS 483111), monthly | `unverified-live` | Public API v2 without a key (a registration key would travel in the request URL or body, so none is used). The series id `PCU483111483111` and the base period are _verify_ |

## Per-source contract

| Source | Endpoint and format | Identifiers | Licence and attribution | Rate limits | Releases and revisions |
| --- | --- | --- | --- | --- | --- |
| UN/LOCODE | `GET https://service.unece.org/trade/locode/loc{yy}{n}csv.zip`, a zip of headerless CSV parts (change, country, location, name, name without diacritics, subdivision, function, status, date, IATA, coordinates, remarks) (_verify_) | UN/LOCODE = ISO 3166-1 alpha-2 country + 3-character location; function position 1 = port | UNECE terms: free reuse with attribution to UNECE (_verify_ wording) | not documented; one download per declared release | about twice a year (`2024-1`, `2024-2`). Each release is a version declared by the operator (version and publication date). A changed entry adds a revision; change indicator `X` marks an entry for removal (kept, flagged); a code of a covered country missing from a later release is marked `removed`, never deleted. Coordinates (`5333N 00958E`) are kept as published and projected through `geospatial` only when published |
| UNCTADstat | `GET https://unctadstat-api.unctad.org/bulkdownload/{report}/{file}` (_verify_ path and archive type), CSV with declared column names per report | economies as UN M49 codes (276, 528); UNCTAD port identifiers with the report's UN/LOCODE column where stated; report codes `US.PortCalls`, `US.PLSCI`, `US.ContPortThroughput`, `US.MerchantFleet` (_verify_) | UNCTADstat terms: reuse with attribution to UNCTAD (_verify_) | not documented; one download per declared report, bounded by `max_pages` and `max_results` | a report's last-update date (declared by the operator) dates a release; a release that changes values is a new vintage, an unchanged release adds nothing; methodology changes stated in the report notes are recorded as series breaks (declared per document with the source text) |
| Eurostat maritime | `GET https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/{dataset}?format=JSON&rep_mar=..&par_mar=..&unit=..&time=..` (JSON-stat 2.0) | `rep_mar` reporting-port codes and `par_mar` partner ports kept as published (mapping to UN/LOCODE is SL07's reviewable identity); `geo` country codes (Eurostat GEO: ISO alpha-2 except `EL`, `UK`); datasets `mar_mg_aa_pwhd`, `mar_mg_aa_cwh`, `mar_go_am_de` (_verify_) | Eurostat reuse policy (Commission Decision 2011/833/EU), attribution required | no published per-user limit (_verify_); a request is bounded by its categories and a 2 000-cell ceiling | quarterly and annual releases revise earlier periods; the cube's `updated` stamp dates a release; status flags (`p` provisional, `c` confidential) are kept verbatim; a confidential cell carries no value; a cell absent from the cube is never zero-filled |
| BLS PPI | `GET https://api.bls.gov/publicAPI/v2/timeseries/data/PCU483111483111?startyear&endyear` (JSON) | BLS series id | US federal statistics, public domain; cite BLS | without registration about 25 queries a day and 10 years per query (_verify_) | values are preliminary (footnote `P`) for four months and then revised; the declared PPI release date dates a release, else the retrieval time (labelled); a revised value is a new vintage; `M13` annual averages are kept apart, never mixed into the monthly series |

## Freight-index licence decisions

Only indices with an `in-scope` decision are acquired; their licence and
attribution are stored on the series and on every observation. Excluded indices
appear in the coverage report (`logistics_freight_indices`, `logistics_readiness`)
as "excluded by licence decision" with the reason below.

| Index | Publisher | Decision | Reason |
| --- | --- | --- | --- |
| PPI deep sea freight transportation (NAICS 483111) | U.S. Bureau of Labor Statistics | **in scope** | US federal statistics are public domain; redistributable with citation |
| Baltic Dry Index (and other Baltic Exchange indices) | The Baltic Exchange | excluded | commercial index; data licence and subscription required for use and redistribution |
| Drewry World Container Index | Drewry | excluded | the weekly headline is on the web, but no licence grants storage or redistribution |
| Freightos Baltic Index (FBX) | Freightos / Baltic Exchange | excluded | commercial terms restrict redistribution |
| Shanghai Containerized Freight Index | Shanghai Shipping Exchange | excluded | no redistribution licence confirmed |
| Xeneta Shipping Index | Xeneta | excluded | subscription product without a redistribution licence |

## Bounded coverage (selected)

The first coverage is small enough to be reviewed figure by figure in the live
check (SL13):

| Provider | Ports | Countries | Series | Periods | Caps |
| --- | --- | --- | --- | --- | --- |
| UN/LOCODE | port-function entries of the declared countries | DE, NL | - | the two most recent releases | 5 000 entries |
| UNCTADstat | Hamburg (DEHAM), Bremerhaven (DEBRV), Rotterdam (NLRTM), Wilhelmshaven | Germany 276, Netherlands 528 | port calls, port LSCI, container port throughput, merchant fleet by flag | two reference years (quarters for the port LSCI) | 50 series per report, 4 reports |
| Eurostat maritime | Hamburg and Bremerhaven as reporting ports, Rotterdam as partner port | DE, NL | goods handled by port (`mar_mg_aa_pwhd`), by country (`mar_mg_aa_cwh`), Hamburg to Rotterdam (`mar_go_am_de`) | two reference years | 50 series per cube, 2 000 cells |
| BLS PPI | - | US | `PCU483111483111` | the months of one reference year | 1 series |

Justification: Hamburg and Rotterdam appear in all three statistical sources, so
their identity can be checked across an embedded UN/LOCODE (UNCTAD), a published
code list (Eurostat) and a name candidate (Bremerhaven); Germany and the
Netherlands are the reporters of the Comext trade-flow fixtures, so the
trade-flow join is exercised on shared codes. The offline fixtures use these
selections with fictional values dated 2097-2099.

## Gap against existing components

- The Eurostat connector had no URL builder for datasets without a `geo`
  dimension; `EurostatConnector.dataset_url` was added (repeated parameters, no
  network access). `parse_cells` from the trade-flows work is reused unchanged.
- The Economics series storage (`register_series`, `dataset_*`,
  `economic_vintages`) keys a series by one geography string and has no place for
  a published partner port, a UN/LOCODE release, a freight-index licence decision,
  status flags or footnotes. Those live in the `logistics_*` mapping tables beside
  it (`src/kb/logistics_series.py`, `src/kb/logistics_ports.py`); every number is
  registered once in the shared storage with its release and retrieval clocks.
- The platform review inbox (`src/kb/review_targets.py`) has fixed target kinds;
  port matches use the proposed/accepted/rejected/reverted state machine of the
  trade identity instead of adding a kind.
