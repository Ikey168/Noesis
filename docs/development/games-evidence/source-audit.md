# Games: source-contract audit and bounded coverage (GM01)

Tracking: wave 3 tracker #2814 · audit issue #2822 · recorded 2026-11-01.
Bundle decision: [ADR-007](../../architecture/decisions/ADR-007-culture-bundle.md)
(CU01, #2815).

No per-track tracker or delivery issue exists yet. This audit leaves one
source: **Wikidata statements on games, developers, publishers and platforms,
through the existing `wikidata` connector, kept as "what Wikidata states" and
never as a register** (`unverified-live`). The MobyGames API and IGDB are
`not-implemented`: neither is confirmed to allow local retention and display of
its data, which Noesis needs for every acquired revision.

**Written offline.** The publisher hosts (`www.wikidata.org`,
`api.mobygames.com`, `www.mobygames.com`, `api.igdb.com`, `www.igdb.com`,
`dev.twitch.tv`) were blocked by this runtime's egress proxy (verified
2026-11-01), and nothing was fetched. Terms, endpoints, Wikidata property and
class identifiers come from the publishers' documentation as the author knows
it. Every item marked _verify_ is unchecked. The only facts confirmed are those
in this repository: the `wikidata` connector in
`src/ingestion/media_metadata_sources.py`. No source here is `live`.

**The machine-readable copy does not exist yet.** The track's acquisition
issues add `PROVIDER_CONTRACTS`, `BOUNDED_COVERAGE`, `MINIMISATION` and
`LIVE_VERIFICATION` for `culture.games` and must match this audit.

Non-goals for every source: no ratings, review scores or aggregated scores
(Wikidata P444 is never read); no cover art, screenshots or other images (P18
and similar never read); no sales, player-count or revenue figures or estimates
(P2664 units sold and P5436 number of users, _verify_ ids, are never read); no
popularity rankings. Age-rating classifications (PEGI, ESRB, USK) are left out
of first coverage; adding them needs an amendment.

## Access decisions

| Source (proposed source-pack id) | Publisher | Delivers | Access | Decision |
| --- | --- | --- | --- | --- |
| `wikidata-games` | Wikimedia Foundation (Wikidata), reusing the `wikidata` connector | statements on declared game items and on the developer, publisher and platform items they name, as Wikidata states them | `wbgetentities`, one declared or reached QID per request (confirmed in code) | `unverified-live` (statements only, never a register) |
| `mobygames-api` | MobyGames (owned by Atari since 2022, _verify_) | games, platforms, release dates per platform and country, companies, credits (people), screenshots, cover art and user ratings | REST API with a personal API key from a MobyGames account; the author believes key access has been tied to a paid subscription tier since the ownership change (_verify_) | `not-implemented` |
| `igdb-api` | IGDB (owned by Twitch, Amazon) | games, platforms, release dates per platform and region, companies, age ratings, covers, ratings | API v4 (`api.igdb.com/v4`, Apicalypse query language) with a Twitch developer application's client id and an OAuth app access token (_verify_) | `not-implemented` |

Reasons:

- **Wikidata.** Same access path and narrow survival as in the
  [FB01 audit](../film-broadcast-evidence/source-audit.md): CC0 1.0, one item per
  request, through the ADR-007 culture statement reader, every record
  "Wikidata states, at revision N, that X" with references. WikiProject Video
  games keeps game items in some depth, including per-platform and per-region
  release dates (_verify_ how consistently).
- **MobyGames.** Its data is contributed by the community under MobyGames'
  own terms, not an open licence (_verify_). To the author's knowledge the API
  terms are written for live lookups in an application: they limit caching,
  forbid building a competing database and do not grant redistribution of
  extracts (_verify_ all three), and access needs a key that may now require a
  paid tier (_verify_). Answering the issue: **only live lookup is believed
  allowed, not local retention and display**. Noesis keeps every acquired
  revision for provenance and exports cited evidence bundles, so the terms do
  not allow the intended use. Not implemented; MobyGames ids on Wikidata items
  (P1933, _verify_) are kept as identifier strings, and a reviewer may cite a
  MobyGames page link-only.
- **IGDB.** Access requires a Twitch developer account and acceptance of the
  Twitch Developer Services Agreement (_verify_). The author recalls that IGDB
  allows commercial use free of charge with attribution (_verify_), but cannot
  confirm offline whether that agreement permits storing API responses
  indefinitely and republishing extracts outside the application, or requires
  deletion on request or after a period (_verify_). Answering the issue: **local
  retention and display are unconfirmed, so the source is treated as live lookup
  only**. Not implemented. If an operator records, with page and date, terms that
  allow retaining and showing the fields below with attribution, the audit is
  amended and the source becomes `gated-not-granted` until a Twitch application
  is registered, then `unverified-live`. IGDB ids on Wikidata items (P5794,
  _verify_) are kept as identifier strings.

### Further candidates not audited

BoardGameGeek (tabletop; its XML API terms are believed to restrict commercial
use, _verify_), national game archives and age-rating bodies' registers (USK,
PEGI, ESRB) were not audited. Adding one needs an amendment to this audit.

## Per-source contract (as far as known)

| Source | Endpoints | Authentication and keys | Licence and redistribution | Rate limits | Revision model: updates, corrections, removals |
| --- | --- | --- | --- | --- | --- |
| Wikidata | the connector's `wbgetentities` request. Classes for P31, all _verify_: Q7889 video game, Q131436 board game, Q7058673 video game series; platform items Q8076 video game console and Q16338 personal computer reached only by P400 from a declared game. Properties, all _verify_: P178 developer, P123 publisher, P400 platform, P577 publication date (qualifiers P400 platform and P291 place of publication), P136 genre, P404 game mode, P437 distribution format, P408 software engine, P179 part of the series, P495 country of origin, P571 inception and P17 country on developer and publisher items. Identifier properties, all _verify_: P1933 MobyGames game id, P5794 IGDB game id, P2725 GOG application id, P1733 Steam application id. References P248, P854, P813, P143 | none; Wikimedia User-Agent policy with the optional `NOESIS_MEDIA_METADATA_CONTACT` contact | CC0 1.0 (confirmed in the connector's contract); attribution "Wikidata" plus each statement's references | 500 ms interval and `maxlag=5`; HTTP 429 or maxlag stops the run | as in FB01: `lastrevid`, statement ids, rank and qualifiers; deprecated never current; absent later is `not-returned`; P143-only references marked `reference: wikimedia-import` |
| MobyGames | `https://api.mobygames.com/v1/...` (_verify_) | API key; if ever admitted, a required secret `NOESIS_MOBYGAMES_API_KEY` (name proposed) | MobyGames API terms (_verify_); believed live-lookup only | believed low per-hour limits (_verify_) | not acquired |
| IGDB | `https://api.igdb.com/v4/{endpoint}` (_verify_) | Twitch client id and app access token; if ever admitted, required secrets `NOESIS_IGDB_CLIENT_ID` and `NOESIS_IGDB_CLIENT_SECRET` (names proposed) | Twitch Developer Services Agreement and IGDB terms (_verify_ retention and redistribution) | believed 4 requests per second (_verify_) | not acquired |

**Release dates per platform and region.** Answering the issue: each release
is one P577 statement on the game item, kept with exactly the qualifiers
Wikidata states, P400 (platform) and P291 (place of publication, a country or
region item). The key of a release statement is (game QID, statement id); its
platform and region are qualifier values, never inferred. A release statement
without P400 is "platform not stated" and one without P291 is "region not
stated"; neither is treated as worldwide or as the first release. Two releases
on the same date for different platforms stay two statements. Dates keep their
Wikidata precision and calendar; a year-precision date is never expanded. Early
access, ports and remasters are separate items or statements only where
Wikidata makes them so; the track never merges them.

**Unavailable-access fallback.** A Wikidata run that fails is reported with its
code and receipt; earlier revisions stay current. MobyGames and IGDB queries
report `not-implemented`, never empty.

## Data minimisation decision

Games name natural persons only through credits (P57 director, P86 composer,
P50 author and similar, _verify_) and through developers who are individuals.
Per ADR-007, decision 5:

- **Stored:** games, series, developers and publishers as organisations,
  platforms, and for a person-valued statement the QID, one label, the role and
  `kind: person`.
- **Excluded:** every property of a human item (birth and death data,
  citizenship, residence, family, religion P140, image). Credits other than
  director, designer (P287, _verify_) and composer are not read in first
  coverage.
- **Traversal:** from declared game QIDs to the developer, publisher and
  platform items they name, one level only. A developer that is a human item is
  not fetched; its QID and the statement's label suffice.

## Record shape and reuse

Shape: **registry-records**. No new shape; release dates are statements on the
game record, not `events-notices`. Reuse: the `wikidata` connector and the
ADR-007 culture statement reader; reviewable identity through
`src/kb/entity_history.py`; citation-only links to `science.media-metadata`
records (a game based on a book, P144) and to `culture.film-broadcast` records
(adaptations), by QID.

## Bounded first coverage

| Source | Selection | Caps |
| --- | --- | --- |
| Wikidata | 10 declared video-game QIDs and 2 declared board-game QIDs, chosen so that at least one game has releases on three platforms in two regions and one has a deprecated release date; plus the developer, publisher and platform items they name | 40 item requests and 400 statements per run |

Justification: a small set shows per-platform and per-region release dates side
by side without a second publisher. Every further selection is a source-pack
version bump.

## Live verification

| Source | Status | Checked | Evidence |
| --- | --- | --- | --- |
| Wikidata game statements | `unverified-live` | - | none for these properties; the connector itself is `unverified-live` |
| MobyGames API | `not-implemented` | - | terms believed to allow live lookup only; access possibly paid (_verify_) |
| IGDB | `not-implemented` | - | retention and redistribution under the Twitch agreement unconfirmed (_verify_) |

Fixtures are authored, not captured: fictional games, companies and platforms
with QIDs in the `Q9999xxxxx` range and release dates in 2094-2097.

## Conclusion

- **Survives, narrowly:** Wikidata statements, `unverified-live`, reported as
  "Wikidata statements, not a register". A per-track tracker may open; its first
  issue is a bounded live check that at least 8 of the 12 declared games carry
  referenced P577 statements with P400 qualifiers. If fewer do, the track stops
  and `games` stays a gap.
- **Depth:** one publisher (Wikimedia); a delivering change adds a thin row
  (ADR-006). IGDB is the most likely second publisher, once its terms are
  confirmed to allow retention.
