# Political elections: source-contract audit and provider coverage (L01)

Tracking: #1908 · delivery issue #1918 · recorded 2026-09-27.

This audit fixes, per source, what the Political pack's optional `elections`
feature may acquire, how, and on what terms. It was written without network
access: endpoints, file layouts, identifiers and terms are recorded from the
providers' published documentation as known to the author. **Every item
marked _verify_ must be checked against the live page and a published file
before the first dated live run (L13, #2016), and no provider is `live` until
that run exists.** The machine-readable copy of these decisions is
`PROVIDER_CONTRACTS` in `src/ingestion/election_sources.py`; the MCP tool
`election_source_contracts` returns it.

Non-goals apply to every source: figures are what the issuing authority
published. Preliminary and certified results stay separate vintages; a poll is
never a result; no seat or outcome prediction, no aggregation of polls into a
single "true" number and no causal claim from news framing.

## Access decisions

| Source | Delivers | Decision | Reason |
| --- | --- | --- | --- |
| Bundeswahlleiterin open data (`kerg2.csv` Leitformat) | results, constituency geometry reference | `unverified-live` | parser follows the long-format result file (area, group, vote, count, published percentage); the header comment that states *vorläufig* / *endgültig* and the *Stand* line are _verify_; whether the preliminary file is replaced in place by the final one is _verify_ |
| Berlin Landeswahlleitung (wahlen-berlin.de) | results, constituency geometry reference | `unverified-live` | parser follows a per-area, per-vote table with one column per party and an `Ergebnisstand` column; the column names, delimiter and file URL are _verify_ |
| daten.berlin.de | catalogue records (mirror of the Landeswahlleitung files, FIS-Broker geometry) | `not-implemented` | used for discovery and licence metadata only; no second copy of the Berlin results is acquired; Berlin geometry comes from the Geospatial pack's collections |
| UK Electoral Commission electoral data | results | `unverified-live` | parser follows a candidate-level CSV with ONS constituency codes; the column names and per-election file URL are _verify_; the file states no publication date, so the HTTP `Last-Modified` header is used and a file without one is refused |
| MIT Election Data and Science Lab (Harvard Dataverse) | results (compiled from certified returns) | `unverified-live` | parser follows the `countypres` layout (FIPS, vote mode, dataset version); Dataverse file access redirects to a storage host, which the runtime's same-host redirect policy refuses: a live run needs a direct, same-host file URL (_verify_) |
| ParlGov | party and cabinet reference data | `not-implemented` | not needed for results; party identity uses reviewable decisions; CC BY-SA share-alike obligations (licence version _verify_) need a decision before any derived records are stored |
| wahlrecht.de poll listings | poll listing (aggregator) | `not-implemented` | no redistribution licence stated (_verify_ in writing); polls are imported from their publishers' own releases instead |
| Poll publishers' own releases | poll readings | `unverified-live` | imported per release through the existing polls connector with a column map; terms recorded per series; a series whose terms are not confirmed is stored link-only (publisher, method, fieldwork dates, sample size, URL; no figures) |

**Unavailable-access fallback.** When a file cannot be fetched (HTTP error,
redirect to another host, budget exceeded, schema drift, no stated vintage or
publication date), the run records the failure class and no release is
written. Earlier releases stay authoritative for their dates: an as-of answer
after the last acquired release reports the vintage then in force. A release is
all-or-nothing: a partial file is never stored.

**Live evidence and notifications.** Releases acquired through the runtime's
HTTPS transport are `live` evidence, fixture replays `fixture` evidence.
Monitors withhold live vintages of a provider whose decision is still
`unverified-live` until a dated live run moves it to `verified-live`.

## Per-source contract

| Source | Access | Auth / limits | Identifiers | Vintages | Jurisdiction rules | Corrections and recounts | Cadence |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Bundeswahlleiterin | CSV per election under `bundeswahlleiterin.de/bundestagswahlen/<year>/ergebnisse/opendata/` (_verify_) | none; undocumented rate limits, one bounded download per run | Wahlkreis number 1-299 per boundary vintage, Land number, party short names as published (no stable party code) | preliminary on election night, final (*endgültig*) after the Bundeswahlausschuss | mixed-member proportional: Erststimme (constituency candidate, plurality) and Zweitstimme (Land list); list and direct mandates published separately | recounts ordered by district returning officers enter the final result; later corrections go through the Bundestag's scrutiny procedure and appear as a republished file | per election |
| Berlin Landeswahlleitung | CSV/XLSX per election (_verify_) | none | Wahlkreis number within the Land, Bezirk number 01-12 | *vorläufig*, then *endgültig*; a repeat election (Wiederholungswahl) is a separate election, never a correction | Abgeordnetenhaus: Erststimme per Wahlkreis, Zweitstimme for Land or Bezirk lists | republished tables | per election |
| UK Electoral Commission | CSV/XLSX per election (_verify_) | none | ONS constituency code (E14/W07/S14/N06), party register name and abbreviation | results as declared by returning officers (certified); recounts happen before the declaration | first-past-the-post, single-member constituencies | a corrected file is a new publication (stored as a `corrected` vintage) | per election |
| MIT Election Lab | CSV dataset releases on Harvard Dataverse | none; Dataverse API limits (_verify_) | five-digit county FIPS, state postal code, dataset version (YYYYMMDD) | compiled from state-certified returns; each dataset version is a source revision | counties (and equivalents) as reporting units; certification authority per state; vote modes as published, never summed | a changed figure in a new dataset version is a `corrected` vintage | irregular |
| Poll publishers | publisher release file or table | per publisher | publisher, commissioning client, fieldwork window, question | a new release may repeat earlier readings; a changed figure for the same fieldwork window is a new reading revision | not applicable (a poll is never a result) | publisher corrections as new reading revisions | per publication |

## Terms

| Source | Terms as recorded | Retention |
| --- | --- | --- |
| Bundeswahlleiterin | Datenlizenz Deutschland - Namensnennung 2.0 with source attribution (_verify_ on the imprint) | figures and file digest |
| Berlin Landeswahlleitung / daten.berlin.de | CC BY 3.0 DE for daten.berlin.de datasets (_verify_ per dataset) | figures and file digest |
| UK Electoral Commission | Open Government Licence v3.0 (_verify_ on the copyright notice) | figures and file digest |
| MIT Election Lab | dataset licence on Dataverse (CC0 is typical for MEDSL datasets; _verify_ per dataset version) with the requested citation | figures and file digest; the citation is kept with the release |
| ParlGov | CC BY-SA (_verify_ version) | none (not implemented) |
| wahlrecht.de | no licence stated (_verify_ in writing) | none (not implemented) |
| Poll publishers | recorded per series at import (`redistribution`: `allowed`, `link-only` or `unknown`) | figures only when `allowed`; otherwise link-only metadata |

## Constituency geometry references

Result files carry no geometry. Each source manifest names the provider's
geometry reference per unit scheme (provider, layer, vintage, CRS, URL); the
constituency record keeps it, and geometry is stored only through the
Geospatial feature store as its own collection per boundary vintage (L07).

| Unit scheme | Geometry reference (as recorded) |
| --- | --- |
| `de-bt-wahlkreis` | Bundeswahlleiterin Wahlkreis shapefile per election, ETRS89 / UTM 32N (EPSG:25832; _verify_ per vintage) |
| `de-be-wahlkreis` | Amt für Statistik Berlin-Brandenburg Wahlkreis layer on FIS-Broker, EPSG:25833 (_verify_ layer id) |
| `gb-ons-pcon` | ONS Westminster Parliamentary Constituencies (July 2024) boundaries, EPSG:27700 |
| `us-fips-county` | US Census TIGER/Line county boundaries by vintage, EPSG:4269 |

## Pinned fixtures

Every source-pack entry replays an authored file in the documented shape
(fictional areas, parties and candidates; dated 2099) through the real adapter;
the fixture digest and expected output hash are pinned in
`config/source_packs/political.json` (`official-political-records` 1.2.0).
Fixtures are never evidence of live coverage.
