# ADR-007: The culture bundle

Status: accepted with amendments, 2026-11-01 (CU01,
[#2815](https://github.com/Ikey168/Noesis/issues/2815), wave 3 tracker
[#2814](https://github.com/Ikey168/Noesis/issues/2814)). Extends
[ADR-005](ADR-005-domain-coverage-program.md), decision 6, which proposed the
`culture` bundle; nothing in ADR-004, ADR-005 or ADR-006 is superseded. Plan:
[domain coverage program](../../roadmaps/domain-coverage-program.md).

The issue suggested the file name `ADR-006-culture-bundle.md`. ADR-006 was
already taken by [source depth](ADR-006-source-depth.md), so this record is
ADR-007.

## Context

ADR-005 lists eight subdomains under Culture and leisure. Three are covered
offline:

| Subdomain | Provider | Bundle | Domain via |
| --- | --- | --- | --- |
| `cultural-heritage` | `science.cultural` | `science` (Science and knowledge) | provider override |
| `books-music-metadata` | `science.media-metadata` | `science` | provider override |
| `sports` | `sports.football`, `sports.forecasts`, `sports.olympics`, `sports.tennis` | `sports` (Culture and leisure) | bundle |

Five are wave 3 gaps, and the tracker names one proposed home for each:
`film-broadcast` (`culture.film-broadcast`, FB), `performing-arts-events`
(`culture.performing-arts`, PA), `games` (`culture.games`, GM),
`archives-genealogy` (`culture.archives`, AR) and `religion-belief`
(`culture.religion`, RB). Their delivery waits on this record; their source
audits ran in parallel and are referenced below.

The only bundle whose domain is Culture and leisure is `sports`. The two
non-sport providers in the domain sit in `science` and reach the domain through
overrides. ADR-004 names `science.media-metadata` as an example of a misplaced
provider and keeps it where it is because provider ids are preserved
identifiers.

## Questions and decisions

### 1. Scope against the existing providers

- **The existing providers stay where they are.** `science.cultural`,
  `science.media-metadata` and the four `sports.*` providers keep their bundles,
  ids and overrides (ADR-004 decision 6, ADR-005 "existing providers stay where
  they are"). Moving them would change their ids and needs its own migration
  decision; none is made here.
- **The `culture` bundle holds the five new providers only.** It does not
  re-export, wrap or require the `science` providers. It links to them by
  citation, the way `science.media-metadata` links to cultural objects: a link
  records both records' source, native id and revision and never merges them.
- **Subdomain boundaries.**
  - `cultural-heritage` keeps objects in collections (museum, library and
    archive objects with their rights), as `science.cultural` acquires them from
    DDB and Europeana. `archives-genealogy` takes **archival descriptions**:
    finding aids and the hierarchy of fonds, series and lower units, each with its
    holding institution and reference code. A DDB record that `science.cultural`
    already holds as an object is not acquired again as a description; the AR
    track links to it by DDB item id.
  - `books-music-metadata` keeps books, editions, recordings and their authority
    records. `film-broadcast` and `games` take audiovisual works, broadcasters'
    identity, and games. A Wikidata item that `science.media-metadata` already
    holds (for example a novel that a film adapts) is linked, not re-acquired.
  - `film-broadcast` keeps **broadcasters' identity** (name, country, launch,
    language, as stated). **Ownership and control of broadcasters stay with
    `media-outlets-ownership`** (`news.outlets`, MO01), which remains a gap. The
    FB track never reads ownership properties (P127, P749, P355, P1830) and never
    acquires MAVISE.
  - `religion-belief` keeps religious affiliation statistics and, should a
    source survive, registers of religious organisations. Population totals stay
    with `economics.demographics`; RB links to them by shared place code and
    citation, never as a computed share.

### 2. A new bundle, or an existing one

**A new bundle is needed.** ADR-005 decision 6 allows one only where the domain
has no bundle suited to the gap. The domain's bundles were checked:

- `sports` is a sports bundle: its manifest, scopes and composition describe
  competitions and results. Film, games, archives or religion statistics there
  would make its name and contract false.
- `science` is not a bundle of this domain. Placing the five providers there
  would need five domain overrides, repeating on purpose the misplacement that
  ADR-004 records as a problem.
- The other candidate neighbours (`news` for broadcasters, `society` for
  religion statistics) belong to other domains and would also need overrides.

`culture` (domain `culture-leisure`) is therefore accepted, with the five
provider ids the tracker proposes. **No provider id is moved or renamed.**
`culture.religion` stays in `culture` although its records are census series:
the subdomain `religion-belief` is listed under Culture and leisure, and moving
it to Society and population would change the closed subdomain list, which
needs its own decision (ADR-005 decision 5).

**Amendment: the bundle entry arrives with the first provider.** The issue
asked for the bundle entry in `packs/taxonomy.json` now. The gate refuses it:
`test_every_bundle_has_exactly_one_domain_or_is_infrastructure` fails when the
taxonomy names a bundle with no directory under `packs/`, and a bundle without a
provider descriptor has nothing to classify. The `society` bundle, the other
bundle ADR-005 proposed, was added the same way: its taxonomy entry, manifest
and first provider (`society.income`) landed in one change (c91e29e7). So:

- this record changes neither `packs/taxonomy.json` nor
  `tests/unit/composition/test_pack_taxonomy.py`;
- the first culture track to deliver adds `packs/culture/` (manifest and its
  provider descriptor), `"culture": {"domain": "culture-leisure"}` under
  `packs` and its provider under `providers` in one change, and removes its gap
  row in the same change; the gate test needs no edit, because
  `culture-leisure` is already a domain;
- a track that ends `not-implemented` adds nothing to the bundle.

### 3. Record shapes

No new shape. Each kind of record maps to an ADR-004 shape:

| Record | Shape | Tracks | Pattern to reuse |
| --- | --- | --- | --- |
| works (films, series, games, stage works), broadcasters, venues, ensembles, festivals, developers and publishers as organisations | `registry-records` | FB, PA, GM | the Wikidata record handling of `src/ingestion/media_metadata_sources.py` (revision `lastrevid`, redirects and missing items as history, rank kept) and reviewable identity |
| archival descriptions (fonds, series, lower units) with holding institution, reference code and parent | `registry-records` | AR | catalogue entries with revisions, as `science.cultural` keeps objects |
| release and first-performance dates, per platform and region | statements on a `registry-records` entry, not events | FB, PA, GM | each date with the qualifiers the source states |
| admissions, cultural participation, religious affiliation tables | `statistical-series` | FB (only if LUMIERE survives), PA, RB | release vintages as in `economic_vintages`; definitions, breaks and census bases as in `src/kb/demographics.py` |
| individual performances, screenings or broadcasts as dated occurrences | `events-notices` | PA, FB | only if a source survives; none does in the audits below |

A record that fits none of these needs a decision record before it is
implemented.

### 4. One shared Wikidata path

FB, PA and GM name Wikidata. They share one path:

- **Access.** The existing `wikidata` connector of `science.media-metadata`
  (`request_for`: `wbgetentities` by one declared QID per request,
  `props=info|labels|claims`, `maxlag=5`, 500 ms interval, `lastrevid` as
  revision, CC0 1.0, optional `NOESIS_MEDIA_METADATA_CONTACT` User-Agent
  contact), confirmed in code. No JSON dumps and no unbounded SPARQL, as that
  connector already excludes. Selections name QIDs; nothing is crawled from a
  class.
- **One parser, not three.** `parse_wikidata` keeps identifier properties,
  creators (P50, P175, P86), P629 and P577 only, and drops qualifiers. It is
  not changed, so the MM05 records of `science.media-metadata` stay as they are.
  The first culture track to deliver writes one **culture statement reader**:
  per provider, a declared allowlist of properties, each statement kept with its
  Wikidata statement id, rank, qualifiers and references. The other culture
  tracks add allowlists to it and write no parser of their own. The MO01
  amendment also calls for an ownership parser in `news.outlets`; reusing this
  reader with its own allowlist is recommended, but that decision belongs to the
  News track.
- **Labelling.** Every Wikidata record is "what Wikidata states", as in the
  [MO01 amendment](../../development/media-outlets-evidence/source-audit.md#amendment-further-candidates-2754):
  "Wikidata states, at revision N, that X", with the statement's references.
  It is never presented as a register or a publisher's own record, never
  preferred over a publisher's statement and never reconciled with one. A
  statement with no reference, or only a P143 "imported from Wikimedia project"
  reference, is marked `reference: wikimedia-import` or `references: []`. A
  statement without an end time is "no end time stated", never "current".
  Coverage backed only by Wikidata is reported as "Wikidata statements" wherever
  coverage is shown.
- **Depth.** Wikidata resolves to the `wikimedia` publisher in
  `packs/depth.json`. Each culture provider's Wikidata source counts once. A
  track whose only surviving source is Wikidata covers its gap at depth one and
  adds a thin row in its own change (ADR-006 decision 4).

### 5. Personal and special-category data

Rules for every culture provider:

- **Persons in Wikidata.** A person (P31 Q5, _verify_) reached as a creator,
  cast member, developer or performer is kept as QID, one label, the role and
  `kind: person` only, as `science.media-metadata` keeps creators. Nothing else
  is read from a human item: no date or place of birth or death, citizenship,
  residence, family relations, ethnic group, sexual orientation, political party,
  image, and **never religion (P140) or any other special-category property**.
  No track starts from a person or assembles a person's works from that person's
  item; works are reached from declared work QIDs.
- **Archives (AR): descriptions, not people.** Only finding-aid levels above the
  file are acquired in first coverage (fonds, sub-fonds, series, and their
  equivalents such as a NARA record group or series). File and item levels, and
  every person-level index (personal-name access points, EAC-CPF descriptions
  of persons and families, name indexes, census and civil-status databases), are
  excluded. Personal-name access points on an acquired unit are dropped at parse
  time. A fonds titled after a person (personal papers) keeps its title as the
  archive states it; no person record is created from it. No family trees, no
  person matching and no record linkage across archives. Admitting file-level
  descriptions later needs an amendment to the AR audit with a cut-off date
  checked against the protection periods of each archive's law (_verify_; the
  AR audit proposes 110 years after the unit's latest stated date as the
  starting point).
- **Religion (RB): published aggregates only.** Religion is special-category
  data (GDPR Art. 9). Only tables a publisher has released are acquired, each
  value exactly as published, with the publisher's disclosure control kept as it
  is: perturbed, rounded, suppressed or swapped cells stay so, a suppressed cell
  is a status and never a value, and totals are never recomputed from perturbed
  cells. No microdata, no person-level record, no estimate, imputation, small-area
  breakdown below the publisher's lowest published geography, or harmonisation of
  our own. Different question wordings (self-identified religion, legal
  membership of a religious body, belonging, upbringing) and different "no
  religion", "not stated" and "other" categories are different definitions and
  stay side by side; nothing maps them onto a common scheme.
- **Scopes.** Each provider reads under `knowledge:culture:<provider>:read`
  with namespace access (for example `knowledge:culture:archives:read`);
  writes and identity reviews need the matching `:write` and `:review` scopes.

## Outcomes for the five tracks

The source audits were written offline on 2026-11-01 (publisher hosts blocked
by the egress proxy). No source in them is `live`.

| Track | Audit | Sources kept (`unverified-live`) | Gated or not implemented | Effect on the gap |
| --- | --- | --- | --- | --- |
| FB01 | [film-broadcast](../../development/film-broadcast-evidence/source-audit.md) | Wikidata (statements) | LUMIERE, MAVISE, TMDB `not-implemented` | may open; depth one, thin row on delivery |
| PA01 | [performing-arts](../../development/performing-arts-evidence/source-audit.md) | Wikidata (statements); Eurostat cultural participation | Deutscher Bühnenverein theatre statistics `not-implemented`; no source for performances | may open; depth two |
| GM01 | [games](../../development/games-evidence/source-audit.md) | Wikidata (statements) | MobyGames API, IGDB `not-implemented` | may open; depth one, thin row on delivery |
| AR01 | [archives](../../development/archives-evidence/source-audit.md) | Archivportal-D through the existing DDB connector (descriptions only) | Archives Portal Europe, US National Archives Catalog API `gated-not-granted` | may open; depth one, thin row on delivery |
| RB01 | [religion](../../development/religion-evidence/source-audit.md) | ONS Census 2021 via Nomis; Zensus 2022 (precondition) | Pew, ARDA `not-implemented` | may open; depth two, one publisher per place |

## Consequences

- FB, PA, GM, AR and RB may open their per-track trackers on the conditions in
  their audits. Accepting the bundle covers nothing: every gap row stays until
  a fixture-tested provider is classified, and live coverage needs each track's
  live validation.
- The first delivering track creates `packs/culture/` and its taxonomy entries;
  later tracks add a provider descriptor to the same bundle.
- The culture statement reader is built once and reused. Changing what
  `parse_wikidata` keeps for `science.media-metadata` is out of scope.
- Three tracks would cover their gap with one publisher and add thin rows
  (ADR-006). Their second publishers are the gated or not-implemented sources
  their audits name.
- `media-outlets-ownership` is unaffected and stays a gap; FB does not touch
  ownership or MAVISE.
