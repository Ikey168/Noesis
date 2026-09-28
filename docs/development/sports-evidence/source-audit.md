# Sports source audit and bounded v1 coverage (SP01, #2136)

Status: reviewed offline on 2026-09-28 for the Sports pack (#2135). No provider was contacted from the build
environment: every statement below about a live endpoint, rate limit, file layout or licence text comes from the
providers' published documentation as known without network access and is marked **(verify)** where it must be
checked against the live terms before the dated live run (SP13, #2148) is accepted. The machine-readable form of
these decisions is `PROVIDER_CONTRACTS` and `LIVE_VERIFICATION` in `src/ingestion/sports_sources.py`; the runnable
source declarations are `config/source_packs/sports.json` (pack `sports-records` 1.0.0).

Offline evidence (authored, fictional fixtures under `tests/fixtures/sports` and
`tests/fixtures/source_packs/sports-*.json`) and live evidence are reported separately. Live evidence belongs in
this directory (`docs/development/sports-evidence/`) and does not exist yet.

## Non-goals applied to every source

- No odds, betting markets, tips, value bets or Noesis-produced predictions. Bookmaker and odds sources are
  `not implemented`; the `odds` block of a football-data.org match is dropped at parse time.
- No medical, injury, biometric or tracking data. Sackmann's height, handedness and age columns and every
  nationality field are never read into a record; the record contract has no field for them.
- A person appears only as a published participant: name as published, the source's id, the team or appearance,
  and a date of birth only where the source itself publishes it (used solely as identity evidence).
- No scraping of sites whose terms forbid it and no mirroring of commercial event data.

## Decisions

| Source | Decision | Access and auth | Licence and attribution | Stable ids | Corrections and fixture moves |
| --- | --- | --- | --- | --- | --- |
| football-data.org v4 | implement | REST JSON, `X-Auth-Token` header from the credential store (`NOESIS_FOOTBALL_DATA_API_KEY`), never committed; free tier 10 requests/minute and a fixed competition list **(verify)** | football-data.org terms: free tier for non-commercial use with attribution "Football data provided by the Football-Data.org API" **(verify wording and redistribution)** | match id, team id, competition code, season start year | status, `utcDate` and score change in place; `lastUpdated` states when **(verify it moves on every change)**; postponement is `POSTPONED` then a new `utcDate` with `SCHEDULED`/`TIMED` **(verify)**; `AWARDED` for an awarded result **(verify)** |
| StatsBomb open data | implement (matches and lineups only) | raw JSON at a pinned commit of `statsbomb/open-data`: `data/competitions.json`, `data/matches/{competition}/{season}.json`, `data/lineups/{match}.json` | StatsBomb Public Data User Agreement: attribution to StatsBomb (logo on published work) **(verify the agreement text and any commercial restriction)** | competition, season, match, team and player ids | edits appear in later commits (`last_updated` per match); a newly pinned commit is a new release |
| openfootball (football.json) | implement | JSON exports at a pinned commit of `openfootball/football.json` (`{season}/{league}.json`); Football.TXT not parsed in v1 | public domain dedication **(verify per repository)** | league code, season, round, team names as published (no numeric ids) | edits in later commits; the commit hash is the release id; a moved date keeps the fixture key (season, round, both teams) and becomes a schedule revision |
| Jeff Sackmann `tennis_atp` / `tennis_wta` | implement, licence-gated (`sports-tennis` feature, default off; the tracker names it `sports_tennis`, but composition feature ids are hyphenated) | CSV `atp_matches_YYYY.csv` / `wta_matches_YYYY.csv` at a pinned commit | **CC BY-NC-SA 4.0** (verify the licence file covers every file used): attribution, non-commercial only, derived records shared alike | tourney id + match number, per-repository player id | rows are edited in later commits without a change log; each pinned commit is a release |
| olympics.com / IOC result pages | link-only | HTML pages, no documented open API **(verify)** | terms restrict automated extraction and reuse **(verify)** | Games, sport and event slugs | cited as the official publication behind transcribed rows |
| Olympic results (operator transcription) | implement | a CSV in the documented `olympic-results-csv` layout, imported per publication (`import_sports_olympic_results`); every row cites the official publication | facts with a citation to the official publication **(verify the cited publication's reuse terms)** | Games, sport, event, phase, participant as published, NOC | a disqualification or medal reallocation is a later publication naming the deciding body (IOC, CAS, the federation) and citing its decision, stored as a result revision |
| Olympedia | not implemented | - | no open data licence stated **(verify)** | - | - |
| Official transfer announcements (governing bodies, clubs) | implement (operator-recorded) | `record_sports_transfer` citing the announcement URL | per announcement **(verify)**; a fee only when the announcement states it | the announcing body's own reference where given | a later announcement is a new revision |
| Wikidata P54 memberships | not implemented in v1 | - | CC0 **(verify)** | QIDs | memberships are not transfers; QIDs a source states are used only as identity cross-references |
| Commercial transfer databases (e.g. Transfermarkt) | not implemented | - | terms forbid extraction and redistribution **(verify)**; commercial data | - | excluded: commercial data and scraping are non-goals; market values are not published records |
| Bookmakers and odds aggregators | not implemented | - | - | - | excluded by the non-goals |

### CC BY-NC-SA 4.0 (Sackmann archives)

What the licence permits here, and how it is enforced:

- **Non-commercial use only.** The archives are acquired only when the operator accepts the source licence in the
  source-pack runtime and selects the `sports-tennis` optional feature (default off). `export_sports_records`
  refuses a `commercial` purpose for any set containing a non-commercial record.
- **Attribution.** Every derived record carries `source.licence` (`CC-BY-NC-SA-4.0`, the licence URL and the
  attribution text "Tennis databases, files, and algorithms by Jeff Sackmann / Tennis Abstract, licensed under
  CC BY-NC-SA 4.0") and `source.attribution`; every export repeats them.
- **Share-alike.** Derived records keep the licence and the `share_alike` flag; an export that would relicense them
  under different terms is refused.
- If an operator cannot meet these obligations the source stays disabled; the decision would then be
  `not implemented`.

### StatsBomb user agreement

Recorded as the source's `license` (`statsbomb-public-data-user-agreement`) and attribution "Data provided by
StatsBomb (StatsBomb open data)" on every derived record. Event-level data (`data/events`, `data/three-sixty`)
is out of v1 scope; managers, referees and player nationality are dropped at parse time.

## Transfers

Openly published transfers come from governing-body and club announcements and are recorded one by one with the
announcement's citation (`transfer_assertion`). A transfer reported only by news stays a cited news claim (SP09)
and is never a transfer record. Commercial transfer databases are excluded for the reasons above.

## Bounded v1 coverage

| Source | Bound |
| --- | --- |
| football-data.org | Premier League (PL) and Bundesliga (BL1), seasons 2024 and 2025: at most 2 x 380 + 2 x 306 matches, 4 tables, one request per source per run within the free-tier limit (the shipped manifest declares PL 2025; the other bounds are further sources of the same shape) |
| StatsBomb open data | one competition-season, its matches file and at most 40 lineup files per pinned commit |
| openfootball | `en.1` and `de.1` for two seasons: at most 1,372 matches |
| Sackmann | ATP and WTA tour-level matches for one year each: at most about 6,000 rows |
| Olympic results | one Games, at most 50 event phases |
| Transfers | operator-recorded announcements only |

The shipped manifest pins every repository source to a placeholder commit (`000…0`): an operator pins a verified
commit and its date before a live run **(verify)**. The endpoint always reads the pinned commit, never a branch.
