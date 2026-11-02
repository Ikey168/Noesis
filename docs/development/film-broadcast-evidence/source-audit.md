# Film and broadcast: source-contract audit and bounded coverage (FB01)

Tracking: wave 3 tracker #2814 · audit issue #2820 · recorded 2026-11-01.
Bundle decision: [ADR-007](../../architecture/decisions/ADR-007-culture-bundle.md)
(CU01, #2815).

No per-track tracker or delivery issue exists yet. They open only because this
audit leaves one source: **Wikidata film, series and broadcaster statements,
read through the existing `wikidata` connector, kept as "what Wikidata states"
and never as a register** (`unverified-live`). The European Audiovisual
Observatory's LUMIERE and MAVISE databases and TMDB are `not-implemented`.

**Written offline.** The publisher hosts (`www.wikidata.org`,
`lumiere.obs.coe.int`, `mavise.obs.coe.int`, `www.obs.coe.int`,
`api.themoviedb.org`, `www.themoviedb.org`) were blocked by this runtime's
egress proxy (verified 2026-11-01), and nothing was fetched. Terms, endpoints,
Wikidata property and class identifiers and field names come from the
publishers' documentation as the author knows it. Every item marked _verify_ is
unchecked and must be checked before the first dated live run. The only facts
confirmed are those in this repository: what the `wikidata` connector requests
and parses (`src/ingestion/media_metadata_sources.py`) and the MO01 audit's
finding on MAVISE. No source here is `live`.

**The machine-readable copy does not exist yet.** The track's acquisition
issues add `PROVIDER_CONTRACTS`, `BOUNDED_COVERAGE`, `MINIMISATION` and
`LIVE_VERIFICATION` in a `src/ingestion/` module for `culture.film-broadcast`
(name chosen by the track), and they must match this audit.

Non-goals for every source: no ratings, review scores, box-office rankings,
popularity or recommendation signals (Wikidata P444 review score, P1258 Rotten
Tomatoes score and similar are never read); no images, posters, trailers or
other media (P18, P3383, P1651 and similar are never read); no plot summaries;
no ownership or control of broadcasters, which belongs to
`media-outlets-ownership` (MO01, a gap); no figures of our own.

## Access decisions

| Source (proposed source-pack id) | Publisher | Delivers | Access | Decision |
| --- | --- | --- | --- | --- |
| `wikidata-film-broadcast` | Wikimedia Foundation (Wikidata), reusing the `wikidata` connector | statements on declared film, television-series and broadcaster items, as Wikidata states them, with rank, qualifiers and references | `GET /w/api.php?action=wbgetentities&ids={QID}&props=info\|labels\|claims&format=json&maxlag=5`, one declared QID per request (the connector's `request_for`, confirmed in code) | `unverified-live` (statements only, never a register) |
| `eao-lumiere-admissions` | European Audiovisual Observatory (Council of Europe), LUMIERE | admissions per film, country and year for films released in European cinemas, with country of production (_verify_ coverage years and fields) | interactive web database; no documented API or bulk export known to the author (_verify_) | `not-implemented` |
| `eao-mavise-services` | European Audiovisual Observatory, MAVISE | television channels and on-demand services by country of establishment, licensing authority, genre and language, with the operating company | as audited in MO01: web database, no confirmed export or API, reuse terms unconfirmed (_verify_) | `not-implemented` (MO01 finding reused) |
| `tmdb-api` | TMDB (The Movie Database) | community-edited film, series, person and company records, release dates per country, certifications, images | REST API v3 with an API key or read access token from a registered account (_verify_) | `not-implemented` |

Reasons:

- **Wikidata.** The access path is in this repository: the `wikidata` connector
  fetches one item per request with all its claims under CC0 1.0, paced at
  500 ms with `maxlag=5`, with `lastrevid` as revision and redirects and missing
  items as history; it is `unverified-live` in
  `src/ingestion/media_metadata_sources.py`. It survives narrowly, as in the
  MO01 amendment: every record reads "Wikidata states, at revision N, that X",
  with the statement's references, and is never presented as a filmography,
  broadcaster register or official record. **Not confirmed in code:**
  `parse_wikidata` keeps identifiers, creators (P50, P175, P86), P629 and P577
  only, and drops qualifiers. Per ADR-007 the film properties below go through
  the shared culture statement reader, with its own allowlist; `parse_wikidata`
  and the MM05 records are not changed.
- **LUMIERE.** Admissions are the one figure in this subdomain a public body
  compiles across Europe, from national film agencies' returns. Not implemented
  because no documented machine access is known and the Observatory's reuse
  terms for extracts are not confirmed (_verify_ both). Scraping the query
  interface is excluded. Should an operator record, with page and date, a
  documented export and terms that allow storing per-film figures with
  attribution, the audit is amended; the records would be `statistical-series`
  (one series per film and country, yearly vintages), never a ranking.
  **Link-only:** a reviewer may cite a LUMIERE page on a film record.
- **MAVISE.** The MO01 audit (#2736, amendment #2754) already found no
  confirmed export or API and no confirmed reuse terms; nothing has changed
  offline, so that finding is reused and not re-audited. MAVISE also lists each
  service's operating company and group, which is ownership. **Keeping FB apart
  from MO01:** if MAVISE ever survives, it is admitted once, by the MO01 path
  ("Path to a surviving source" in that audit), and its operator and group
  fields belong to `news.outlets`. `culture.film-broadcast` would read only the
  service's identity fields (name, country of establishment, genre, language)
  and would link to the `news.outlets` record by MAVISE identifier.
- **TMDB.** The data is community-contributed under TMDB's own terms, not an
  open licence (_verify_). To the author's knowledge the API terms require
  attribution with TMDB's notice and logo, restrict commercial use to separate
  agreements, and limit how long API data may be cached or stored (_verify_ all
  three and any stated retention period). Noesis keeps every acquired revision
  for provenance and exports cited evidence bundles, so a retention limit or a
  bar on redistribution conflicts with the intended use. Much of what TMDB holds
  beyond Wikidata is out of scope anyway (images, ratings, popularity,
  recommendations). Not implemented; even with acceptable terms the source
  would remain `gated-not-granted` until an account and key are held. TMDB ids
  stated on Wikidata items (P4947 film, P4983 series, _verify_) are kept as
  identifier strings only.

### Further candidates not audited

National film agencies' statistics (for example the German FFA, the French CNC
and the BFI Statistical Yearbook) and national broadcasting regulators'
licence lists were not audited. Regulators' lists belong to MO01's track.
Adding a film-agency source needs an amendment to this audit and a source-pack
version bump.

## Per-source contract (as far as known)

| Source | Endpoints | Authentication and keys | Licence and redistribution | Rate limits | Revision model: updates, corrections, removals |
| --- | --- | --- | --- | --- | --- |
| Wikidata | the connector's `wbgetentities` request, one declared QID per request, optionally pinned with `revision` (confirmed in code). Classes for P31, all _verify_: Q11424 film, Q5398426 television series, Q1616075 television station, Q2001305 television channel. Work properties, all _verify_: P57 director, P58 screenwriter, P161 cast member, P162 producer, P272 production company, P449 original broadcaster, P495 country of origin, P364 original language, P577 publication date (qualifier P291 place of publication), P2047 duration, P144 based on, P179 part of the series, P1476 title. Broadcaster properties, all _verify_: P17 country, P571 inception, P576 dissolved, P407 language, P159 headquarters location. Identifier properties, all _verify_: P345 IMDb, P4947 and P4983 TMDB, P2704 EIDR, LUMIERE film id (property id _verify_). References: P248, P854, P813, P143 | none; Wikimedia User-Agent policy with the optional `NOESIS_MEDIA_METADATA_CONTACT` contact the connector already declares | CC0 1.0 (confirmed in the connector's contract); attribution "Wikidata" plus each statement's own references | the connector's 500 ms interval and `maxlag=5`; HTTP 429 or a maxlag error stops the run | `lastrevid` is the item revision. Each statement keeps its statement id, rank and qualifiers; a deprecated statement is kept with its rank and never shown as current. A statement absent from a later revision is `not-returned` at that revision. Redirects and deleted items follow the connector's history handling. A statement whose only reference is P143 is marked `reference: wikimedia-import` |
| LUMIERE | `https://lumiere.obs.coe.int/` (_verify_); no export path known | none known (_verify_) | Observatory / Council of Europe terms (_verify_); link-only | - | not acquired |
| MAVISE | see the MO01 audit | see the MO01 audit | see the MO01 audit | - | not acquired |
| TMDB | `https://api.themoviedb.org/3/...` (_verify_) | API key or bearer token from a TMDB account; if ever admitted, a required secret `NOESIS_TMDB_API_TOKEN` (name proposed) | TMDB API terms (_verify_): attribution, commercial-use and caching limits | _verify_ | not acquired |

**Release dates per country.** Wikidata models a release as a P577 statement,
often with a P291 place-of-publication qualifier (_verify_). Each statement is
kept as one dated statement with its qualifiers; a release date without P291 is
"place not stated", never the world premiere. Dates are kept with their
Wikidata precision (year, month or day); a year-precision date is never
expanded to a day.

**Unavailable-access fallback.** A Wikidata run that fails (HTTP error, 429,
maxlag, schema drift) is reported with its code and receipt; earlier revisions
stay current and nothing is marked removed. LUMIERE, MAVISE and TMDB queries
report `not-implemented`, never empty.

## Data minimisation decision

Film and series items name natural persons (directors, writers, cast,
producers). Following ADR-007, decision 5:

- **Stored:** works, series and broadcasters with the statements above, and
  for each person-valued statement the person's QID, one label, the role
  (`director`, `screenwriter`, `cast-member`, `producer`) and `kind: person`.
- **Excluded:** every property of a human item, even transiently in a
  fixture: dates and places of birth and death, citizenship, residence, family
  relations, awards, religion (P140), ethnic group, party membership and image.
  Cast character names are kept only as the P453 qualifier on the cast
  statement (_verify_ the qualifier) and never as a person attribute.
- **Traversal:** acquisition starts at declared work or broadcaster QIDs. It
  never starts from a person and never assembles a person's filmography; a
  person QID is never fetched in first coverage.
- **Retention:** revisions are retained for provenance. If Wikidata removes a
  person-valued statement, the later revision records `not-returned`.

## Record shape and reuse

Shape: **registry-records** (work and broadcaster entries with revisions and
reviewable identity). No new shape. Reuse: the `wikidata` connector's request,
pacing and revision handling; the culture statement reader that ADR-007
introduces; reviewable identity through `src/kb/entity_history.py` as the
media-metadata store does; citation-only links to `science.media-metadata`
records for source works (P144 based on) and, should `news.outlets` ever hold a
broadcaster, to that record by QID. LUMIERE, if ever admitted, would be
`statistical-series`.

## Bounded first coverage

| Source | Selection | Caps |
| --- | --- | --- |
| Wikidata | 10 declared film QIDs, 5 declared television-series QIDs and 5 declared broadcaster QIDs, chosen so that at least one film has releases in two countries, one series has a deprecated or redirected statement and one broadcaster has a dissolution date | 20 item requests and 300 statements per run; no person or company item is fetched |

Justification: one small set shows per-country release dates side by side,
the rank and redirect handling, and broadcaster identity kept apart from
ownership. Every further selection is a source-pack version bump.

## Live verification

| Source | Status | Checked | Evidence |
| --- | --- | --- | --- |
| Wikidata film and broadcaster statements | `unverified-live` | - | none for these properties; the `wikidata` connector itself is `unverified-live` |
| LUMIERE | `not-implemented` | - | no machine access or reuse terms confirmed |
| MAVISE | `not-implemented` | - | MO01 finding reused: export and terms unverified |
| TMDB | `not-implemented` | - | API terms on retention, redistribution and commercial use conflict with the intended use (_verify_) |

Fixtures written for the track are authored, not captured: fictional works,
broadcasters and persons, item QIDs in the `Q9999xxxxx` range the media-metadata
fixtures already use (`tests/fixtures/source_packs/media-wikidata.json`), dates
in 2094-2097. Class QIDs such as Q5 stay real, as there.

## Conclusion

- **Survives, narrowly:** Wikidata statements, `unverified-live`, reported as
  "Wikidata statements, not a register". A per-track tracker may open. Its
  first issue is a bounded live check that at least 15 of the 20 declared items
  carry referenced statements for the listed properties; if fewer do, the track
  stops and `film-broadcast` stays a gap.
- **Depth:** one publisher (Wikimedia). A delivering change adds a thin row
  (ADR-006). LUMIERE is the natural second publisher once its access and terms
  are confirmed.
- **MO01:** unchanged. FB never reads ownership properties or MAVISE operator
  fields; `media-outlets-ownership` stays a gap.
