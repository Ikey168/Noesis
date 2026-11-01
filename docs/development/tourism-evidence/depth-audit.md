# Tourism and hospitality: second-publisher depth audit (DT01)

Tracking: [#2826](https://github.com/Ikey168/Noesis/issues/2826) DT01 (wave 4
tracker [#2825](https://github.com/Ikey168/Noesis/issues/2825)) · decision
[ADR-006](../../architecture/decisions/ADR-006-source-depth.md) · plan
[source depth program](../../roadmaps/source-depth-program.md) · recorded
2026-11-01.

`tourism-hospitality` is thin: its only provider, `economics.tourism`, reads one
publisher, Eurostat (`eurostat-tourism-occupancy`, `eurostat-tourism-capacity`
in `economic-statistics-and-filings` 1.8.0; see [source-audit.md](source-audit.md)).
This audit names the candidate second publishers and records, per candidate,
endpoint, dataset codes, access, licence, redistribution terms, origin chain and
fit with the provider's record shape, before any code.

**It was written without network access.** The publisher hosts
(`www-genesis.destatis.de`, `sdmx.oecd.org`, `www.oecd.org`,
`www.statistik-berlin-brandenburg.de`, `www.regionalstatistik.de`) are blocked by
this runtime's egress proxy (verified 2026-11-01). Terms, endpoints, table codes,
dimension names and rate limits come from the publishers' documentation as the
author knows it and from what other Noesis source packs already record for the
same publishers. Every item marked _verify_ must be checked against the live
pages, the live terms and a real response before the first dated live run. None
of the sources below is live; none counts toward live depth (ADR-006,
decision 6).

## Summary

| Candidate | Publisher (registry id in `packs/depth.json`) | Adds a publisher | Adds an independent origin | Record shape fit | Decision |
| --- | --- | --- | --- | --- | --- |
| Destatis GENESIS-Online, monthly accommodation survey | Statistisches Bundesamt (`destatis`, already registered) | yes | no: Destatis compiles the German figures it transmits to Eurostat | fits `noesis-tourism-statistics-record-v2` with a declared geography scheme and a GENESIS symbol mapping | **recommended** for `economics.tourism` |
| OECD tourism statistics (OECD Data Explorer, SDMX) | OECD (`oecd`, already registered) | yes | no: national administrations report to the OECD, and for EU members part of the data comes through Eurostat (_verify_) | partial: national, mostly annual, concepts differ from Eurostat's accommodation statistics | `not-implemented` (this wave) |
| Amt für Statistik Berlin-Brandenburg, Berlin accommodation statistics | Amt für Statistik Berlin-Brandenburg (`statistik-bb`, already registered) | yes | no new origin against Eurostat, but it is the office that collects the Berlin returns, i.e. the head of the chain | fits for Berlin totals; district series need a geography the record does not yet key | `not-implemented` (this wave): no stable machine interface established |

All three publishers are already in the `packs/depth.json` registry, so the
recommended source needs no new registry entry: its `publisher` string
(`Statistisches Bundesamt (Destatis), GENESIS-Online`) resolves to `destatis`
through the existing `match` prefix, and only the `economics.tourism` source list
changes.

## Origin chain (ADR-006, decision 5)

The German monthly accommodation survey (Monatserhebung im Tourismus, legal
basis the Beherbergungsstatistikgesetz, _verify_ citation) is a decentralised
federal statistic:

1. **Statistical offices of the Länder** collect the returns of establishments
   in their Land. For Berlin this is the Amt für Statistik Berlin-Brandenburg.
2. **Destatis** compiles the federal results and publishes them in GENESIS-Online.
3. **Eurostat** receives the national results under Regulation (EU) No 692/2011
   (_verify_ amendments) and publishes them as `tour_occ_*` and `tour_cap_*`.
4. **OECD** collects national tourism statistics by questionnaire from national
   administrations; for EU members part of the accommodation data is taken from
   Eurostat (_verify_ per table).

Consequences:

- Every candidate is a publisher of the same underlying returns. Each one raises
  depth; none adds an independent origin for a German or Berlin figure. A
  Destatis figure that equals the Eurostat figure for the same month is the same
  observation published twice, never a corroboration. Record-level independence
  stays with the
  [Evidence Independence Graph](../../../contracts/noesis-evidence-independence-v1.md),
  which must see these as one origin chain.
- What a second publisher does add is visible publication differences:
  revision timing (Destatis revises provisional months on its own calendar and
  Eurostat republishes later), coverage thresholds stated in each publisher's
  own words, regional and district detail that Eurostat does not publish, and
  definition notes. These are the disagreements the provider must keep visible.
- Once `tourism-hospitality` reaches depth two through Destatis, the program's
  depth-two watchlist must say that independence is doubtful (shared origin).

## Per-candidate contract

### Destatis GENESIS-Online: monthly accommodation survey

| Item | Record |
| --- | --- |
| Endpoint | GENESIS-Online web service, `https://www-genesis.destatis.de/genesisWS/rest/2020/data/tablefile` (ffcsv), the same REST path the `demographics` connector already uses for `destatis-genesis-population-migration` and `destatis-genesis-bautaetigkeit` (_verify_ that the 2020 interface is still current; Destatis has announced interface changes before) |
| Dataset codes | statistic 45412 (Monatserhebung im Tourismus): tables `45412-0001` (arrivals and overnight stays, Germany, months), `45412-0010`-range (by Land, months), `45412-0021`-range (by type of establishment), and capacity tables in 45412 or 45413 (establishments and bed places offered) (_verify_ every table code, the dimension codes `DLAND`, `WZ08`/`BETRIEBSART`, `WOHNSITZ` and the value columns) |
| Access | registered account; token in `NOESIS_DESTATIS_GENESIS_TOKEN` (existing variable, already listed in `docs/guides/connector-api-keys.md`); never a value in the repository |
| Licence | Datenlizenz Deutschland - Namensnennung - Version 2.0 (`dl-de/by-2-0`), as the existing GENESIS sources record (_verify_ for these tables) |
| Redistribution | permitted with attribution ("Statistisches Bundesamt (Destatis), GENESIS-Online, <table>, <Stand>"), including in exported evidence bundles (_verify_) |
| Rate limits | no published quota known to the author (_verify_); the source budget (pages, bytes) bounds every run |
| Revision model | each table carries an update stamp (`Stand`/"Updated"); provisional months are revised in later releases; each changed release is a new vintage, as for Eurostat |
| Symbols | GENESIS quality and special symbols (`-` nothing, `.` secret, `x` not meaningful, `/` not reliable, `...` not yet available, `p` provisional, `r` revised; _verify_ the list) are kept verbatim per value; `.` is a status (`confidential`), never a value |

Fit with `noesis-tourism-statistics-record-v2`
(`src/kb/tourism_records.py`, `src/ingestion/tourism_sources.py`):

- **Fits without a new shape.** The record types (`release`, `series`,
  `definition`, `vintage`, `observation`, `comparability_note`) and the
  concepts (`nights_spent`, `arrivals`, `establishments`, `bed_places`) cover the
  GENESIS tables. The release clock basis `declared_release` or
  `provider_last_update` takes the table's `Stand`. Values continue into the
  Economics series storage through `register_series`.
- **Series key.** Residence of guest maps from the GENESIS residence attribute
  (Inland/Ausland, _verify_) to the existing `c_resid` roles; accommodation type
  maps WZ 2008 classes 55.1, 55.2, 55.3 to the existing `I551`, `I552`, `I553`
  codes only through a declared, cited mapping (WZ 2008 is the national version
  of NACE Rev. 2). The dataset part of the key is the GENESIS table code, so a
  Destatis series is never the same series as a Eurostat series.
- **Geography.** GENESIS keys Länder by AGS (`11` Berlin), not NUTS. The record
  keys geography "with its NUTS version"; the source must declare the
  geography scheme (`ags`, as `destatis-genesis-population-migration` does) and
  link to NUTS places only through a reviewed published correspondence
  (`src/kb/tourism_identity.py`), code before name, nothing merged.
- **Code to extend, not shape.** `tourism_sources.py` today hard-codes the
  Eurostat providers (`PROVIDERS`, `PROVIDER_HOSTS`, `DATASETS`, `FLAG_MEANINGS`,
  the `nuts_versions` check). The delivery adds a Destatis entry to each, a
  GENESIS symbol map, and keeps `NEVER_SENTENCE`: no blending of Destatis and
  Eurostat values.
- **Bounded first coverage (proposal).** Germany and Berlin (`DLAND` `11`),
  monthly arrivals and nights spent by residence, all accommodation types,
  at most 36 months from a declared start; Berlin establishments and bed places.
  This mirrors the Eurostat bounded coverage so the two publications of the same
  month can be shown side by side with their own vintages.

### OECD tourism statistics

| Item | Record |
| --- | --- |
| Endpoint | OECD Data Explorer SDMX REST, `https://sdmx.oecd.org/public/rest/data/{agency},{dataflow},{version}/{key}` (SDMX-CSV), the path `oecd-labour-statistics`, `oecd-socx` and `oecd-idd-income-distribution` already use; tourism dataflows of the OECD Tourism Statistics database (inbound, domestic, outbound tourism; enterprises and employment; _verify_ agency `OECD.CFE.*` and every dataflow id) |
| Access | none (no key) (_verify_) |
| Licence | CC BY 4.0 for OECD data since 2024 under the OECD terms and conditions (_verify_; the existing OECD sources record "attribution-required (verify)") |
| Redistribution | with attribution (_verify_); third-party data inside OECD tables may carry their own terms (_verify_) |
| Rate limits | the OECD API states a per-hour request limit (_verify_ the figure) |
| Origin chain | national administrations (Destatis for Germany) and, for EU members, partly Eurostat (_verify_) |

Fit: partial. The OECD tables are national and mostly annual. Their concepts
(international arrivals, overnight visitors, trips) follow the International
Recommendations for Tourism Statistics and do not map one to one to Eurostat's
accommodation-establishment concepts; nights spent in accommodation exist in some
tables (_verify_). Berlin is not covered.

Decision: **`not-implemented` this wave.** It adds a publisher but no origin, no
Berlin figure and a concept mismatch that would put non-comparable numbers next
to each other. It may be reconsidered for national demand-side series in a later
amendment.

### Amt für Statistik Berlin-Brandenburg (AfS)

| Item | Record |
| --- | --- |
| Endpoint | Statistical reports "Beherbergung im Land Berlin" (series G IV 1, monthly, _verify_ series code) as XLSX/PDF downloads under `https://www.statistik-berlin-brandenburg.de/` (_verify_ the file URLs and whether they are stable); the joint Regionaldatenbank Deutschland (`https://www.regionalstatistik.de/genesis/`, GENESIS-based, run by the statistical offices of the Federation and the Länder, _verify_ table `45412-*` regional tables and its own web-service terms) |
| Access | file downloads need no account (_verify_); the Regionaldatenbank web service needs an account (_verify_) |
| Licence | CC BY 3.0 DE, as `statistik-bb-bezirke` and `statistik-bb-bautaetigkeit` record ("operator must confirm") (_verify_ for the tourism reports); Regionaldatenbank: `dl-de/by-2-0` (_verify_) |
| Redistribution | with attribution (_verify_) |
| Origin chain | AfS collects the Berlin returns: it is the head of the chain that ends at Eurostat. It adds a publisher but no independent origin; it does add Berlin district (Bezirk) detail that Eurostat does not publish (_verify_) |

Fit: Berlin totals fit the record shape like the Destatis Land series. District
series would need a Berlin district geography in the series key (the
Geospatial bundle already holds the 12 district boundaries, so a link by
published district code is possible), which is a key extension to decide in the
delivery issue, not a new shape.

Decision: **`not-implemented` this wave.** No stable, documented machine
interface was established offline: the reports are files at release-specific
URLs (_verify_), and the Regionaldatenbank is a second publisher string (the
statistical offices jointly) whose registry binding would need a judgement.
Destatis delivers the same Berlin Land figures through an interface Noesis already
uses. AfS can join later as an amendment for district detail.

## Recommendation

1. **Implement Destatis GENESIS (monthly accommodation survey, statistic 45412)
   in `economics.tourism`** with the existing
   `noesis-tourism-statistics-record-v2` shape, reusing the GENESIS web-service
   path and the `NOESIS_DESTATIS_GENESIS_TOKEN` credential. Proposed source id
   `destatis-genesis-tourism`, publisher string
   `Statistisches Bundesamt (Destatis), GENESIS-Online` (resolves to the
   existing `destatis` registry entry). Fixtures are authored, with fictional
   reference periods as in the TO01 deviation note.
2. In the same delivery change: add `destatis-genesis-tourism` to
   `providers.economics.tourism.sources` in `packs/depth.json`, remove the
   `tourism-hospitality` row from the thin table, and add it to the depth-two
   watchlist with the note that Destatis compiles the German figures Eurostat
   publishes (shared origin, independence doubtful).
3. OECD tourism statistics and AfS Berlin accommodation statistics:
   `not-implemented` for this wave, with the reasons above.

Depth after the recommended delivery: offline 2 (Eurostat, Destatis), live 0
until the track's "Validate live coverage" run. Independent origins for a German
or Berlin figure: still 1.

## Live verification

| Candidate | Status | Checked | Evidence |
| --- | --- | --- | --- |
| Destatis GENESIS 45412 | `unverified-live` (proposed) | - | none; publisher host blocked 2026-11-01 |
| OECD tourism statistics | `not-implemented` | - | concept mismatch and shared origin; terms not re-checked (_verify_) |
| AfS Berlin accommodation | `not-implemented` | - | no stable machine interface established offline (_verify_) |
