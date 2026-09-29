# Political elections guide

Tracking: #1908. The optional `elections` feature of the Political bundle
(default off, independent of `lobbying`) takes a contest - one election in one
reporting unit for one ballot - to a cited results-and-polls dossier: result
vintages as each authority published them (preliminary, certified, recount,
corrected) and the figures that changed between them, candidates and lists
with reviewable party identity, poll series with publisher, method, fieldwork
dates and sample sizes kept apart from results, constituency boundaries in the
Geospatial store as of a date, user-registered forecasts resolved only against
the certified vintage, and news evidence. Nothing here predicts seats or
outcomes, aggregates polls into one number or reads news framing as a cause.

## Pieces

| Piece | Where |
| --- | --- |
| Source audit and access decisions (four `unverified-live` result providers; ParlGov, wahlrecht.de and the daten.berlin.de catalogue `not-implemented`) | `docs/roadmaps/political-elections-source-audit.md`, `PROVIDER_CONTRACTS` in `src/ingestion/election_sources.py` |
| Result acquisition (`election-results` connector) | `de-bundeswahlleiterin-btw-results`, `de-berlin-agh-results`, `gb-electoral-commission-ge-results`, `us-mit-election-lab-countypres` in `config/source_packs/political.json` (1.2.0) |
| Record owner (`noesis-election-record-v1`) | `src/kb/elections.py` (releases, elections, constituencies, contests, candidate-or-list records, append-only result vintages) |
| Poll series | `src/kb/elections_polls.py` over the existing polls connector (`poll_source.py`, `poll_to_series`, `ObservationStore`) |
| Identity and assertions | `src/kb/elections_identity.py` (the ownership identity state machine; entity identity decisions; dated succession and redistricting assertions) |
| Boundaries and places | `src/kb/elections_geo.py` (Geospatial projector and feature store; place resolution review) |
| Forecasts | `src/kb/elections_forecasts.py` (the binary forecast ledger; certified-only resolution) |
| News evidence | `src/kb/elections_news.py` (`document_actors`, `document_frames`, sentiment) |
| Monitors | `src/kb/elections_monitoring.py` (knowledge subscriptions) |
| Dossier | `src/kb/elections_queries.py` |
| MCP tools | `tools/knowledge_engine_mcp/elections.py` |
| Provider and feature | `packs/political/providers/political.elections.json`, `packs/political/composition.json` (`elections`, profile `political.elections-review`) |

## Journey

1. Enable the feature with a coordinator selection (`features=["elections"]`
   for the `political` bundle); the bundle works unchanged without it.
2. Run the result sources through the source-pack runtime. Each run records one
   release; an unchanged file adds nothing, a contest whose figures are new
   gets a vintage of the release's kind, and a certified figure that later
   changes becomes a `corrected` vintage. The vintage in force on a date is
   chosen by the authority's publication date, so an older file that arrives
   late lands as history.
3. `import_election_poll_release` imports a publisher's own release; figures
   are stored only when the publisher's terms allow it (`redistribution:
   allowed`), otherwise the readings are link-only metadata.
4. `propose_election_identity_matches` proposes party, candidate, poll-option
   and constituency candidates within one country; review or revert with the
   `…_election_identity_match` tools. `record_election_assertion` records a
   dated, cited successor, merger, renaming or redistricting assertion;
   conflicting assertions stay side by side.
5. `project_election_boundaries` projects one boundary vintage (its validity
   window, the provider's CRS) through the Geospatial projector, or
   `register_election_boundary_collection` uses an existing collection in
   place. `election_results_at_place` answers which constituency contained a
   point on a date and returns its results; `link_election_constituency_place`
   records a reviewable place resolution.
6. `register_election_forecast` stores your forecast (your probability) in the
   ledger with a certified-only rule; `propose_forecast_resolution` cites the
   certified vintage or stays unresolved; `resolve_binary_forecast` is
   unchanged; `score_election_forecasts` scores with the certified publication
   time as cutoff.
7. `refresh_election_news_links` links articles by explicit mentions (shared
   words are keyword candidates); `election_news_evidence` shows frames and
   sentiment beside results and polls for the same period.
8. `create_election_monitor` watches a contest, constituency, poll series or
   an election's polls; runs at committed watermarks deliver new vintages
   (certified ones with the changed figures, each side cited), recounts,
   corrections and poll readings.
9. `election_contest_dossier` assembles the answer, listing unknowns (no
   certified vintage yet, unmatched names, unresolved places).

Every answer is scoped by namespace and `knowledge:political:elections:read`;
identity views also need `knowledge:ownership:read`, and the dossier's
optional places, forecasts and news sections check the Geospatial, forecast
and news scopes when requested.

## Evidence

Offline acceptance runs on authored fixtures (`tests/fixtures/elections`,
fictional areas, parties and candidates) in
`tests/unit/domains/test_elections_acceptance.py`; every release it records is
marked `fixture` evidence and poll imports `operator-import`. No provider has a
dated live run yet; live validation and a cited demo are tracked separately
(#2016).
