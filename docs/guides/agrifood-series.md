# Agriculture and Food Systems guide

Tracking: #2213. From a crop or commodity and a place to the published
production, yield, area, producer and market prices and food balances of each
source, with release vintages, flags and as-of time. Source access decisions:
[source audit](../roadmaps/agrifood-source-audit.md).

## What the pack does and does not do

- **Does**: acquire a bounded, explicit selection from FAOSTAT (QCL, PP, FBS),
  USDA NASS Quick Stats, USDA FAS PSD, Eurostat agriculture (through the SDMX
  connector) and the EU Agri-food data portal; keep every figure with its unit,
  reference period (calendar year, marketing year or week, never converted),
  value text, flag code and label **verbatim** and its release vintage; answer
  per source, side by side, as of a date; list revisions; align commodity codes
  through reviewable crosswalks and places through geospatial place
  resolution; link trade flows, climate/weather records and RASFF food alerts
  by explicit citation; monitor releases, revisions and linked alerts.
- **Does not**: forecast or project yields, production or prices; compute
  food-security indicators; blend, sum or average sources or differently
  defined commodities; impute withheld or missing values; infer causal or
  correlational links (for example weather to yield). NASS forecasts, Eurostat
  `f` values and USDA PSD projections are returned labelled as the publisher's.

## Journey

1. **Acquire** - `acquire_agrifood_sources(namespace, run_key)` runs the
   `agrifood` source pack (`config/source_packs/agrifood.json`) through the
   source-pack runtime with licence acceptance, budgets and receipts. NASS and
   PSD need the `NOESIS_NASS_API_KEY` / `NOESIS_FAS_API_KEY` secrets; no key is
   ever stored in a URL, receipt or record. A selection the provider has no
   data for is a `no_data` outcome.
2. **Align** - `register_agrifood_places` registers the bounded places as
   geospatial places carrying every source code (FAO area, ISO 3166-1, US FIPS,
   PSD country, Eurostat geo, portal member state).
   `propose_agrifood_crosswalks` offers `equivalent` candidates between codes
   whose published labels state the same exact name ("Maize (corn)" and
   "CORN"); `propose_agrifood_crosswalk_manual` records a reviewer's
   `broader`/`narrower` mapping (or a mapping to an HS/CN/CPC heading that trade
   records cite). Nothing counts until `review_agrifood_crosswalk` accepts it;
   rejections and `revert_agrifood_crosswalk` are kept in the history.
   `list_unmapped_agrifood_codes` shows unmapped and partial codes.
3. **Ask** - `agrifood_series_as_of(namespace, commodity, place, as_of)`
   returns every matching series per source with its commodity mapping (query,
   equivalent, broader or narrower - the latter two never aggregated), unit,
   period type, and per period the figure of the latest vintage released at or
   before the as-of time, its flag, estimate type, release and citation;
   figures revised later are marked. Withheld (`(D)`, Eurostat `c`) and missing
   values are returned as such. A commodity and place without series is
   `none_on_record`. `agrifood_revision_history(series_id, period)` lists every
   vintage with its release date and the differences between vintages.
4. **Link** - `link_agrifood_citations` links RASFF notices that name a
   commodity explicitly, climate/weather records that cite a place code and a
   period, and trade-flow records that cite an acquired code (or one an accepted
   equivalent crosswalk reaches) with a place code; without a composed
   Economics trade provider the trade result is `provider_unavailable`.
5. **Monitor** - `create_agrifood_monitor(commodity, place, thresholds)` is a
   knowledge subscription; `run_agrifood_monitor` evaluates at a committed
   complete `agrifood` run and delivers `release`, `revision` (old and new value
   and flag with both citations) and `linked_alert` events once. Thresholds
   (`min_abs_change`, `min_pct_change`) are the user's; the pack sets none.

## Records and storage

`noesis-agrifood-record-v1` (`contracts/schemas/jsonschema/noesis-agrifood-record-v1.json`)
defines commodity, release, observation and food-balance statements.
`src/kb/agrifood_store.py` keeps series, vintages and values (a different
figure under an unchanged release is refused as `vintage_conflict`) and
mirrors numeric values into the Economics `ObservationStore`
(`dataset_series` / `dataset_observations`) at each vintage's release time.

## Evidence

The offline acceptance test
`tests/unit/domains/test_agrifood_acceptance.py::test_commodity_and_place_to_cited_series_with_vintages_and_flags`
replays authored fixtures in each provider's documented response shape with
sockets blocked. Every provider stays `unverified-live` until the live
validation (#2370) records a dated run; offline and live evidence are reported
separately by `agrifood_bundle_status`.
