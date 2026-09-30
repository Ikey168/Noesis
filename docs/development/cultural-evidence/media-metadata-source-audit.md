# Media metadata source audit (books, music and authorities)

Planning item MM01 ([#2476](https://github.com/Ikey168/Noesis/issues/2476)) of
[#2225](https://github.com/Ikey168/Noesis/issues/2225). It extends Cultural
Collections ([#1732](https://github.com/Ikey168/Noesis/issues/1732)). There is
no new pack. The sources run as `media-metadata` connector sources of the
scientific source pack (`config/source_packs/scientific.json`), next to the
DDB and Europeana sources.

This document records one decision per source: access method, licence, rate
limit, User-Agent requirement and revision semantics. It also records the
bounded coverage and what is excluded. The machine-readable copy is
`PROVIDER_CONTRACTS`, `EXCLUDED_SOURCES`, `EXCLUDED_RECORD_CLASSES`,
`LIVE_VERIFICATION` and `BOUNDED_COVERAGE` in
`src/ingestion/media_metadata_sources.py`. The
`media_metadata_source_contracts` MCP tool returns that copy.

Every source is `unverified-live` until a dated live run
([#2513](https://github.com/Ikey168/Noesis/issues/2513)). Request paths and
field names were written from public documentation. The fixtures under
`tests/fixtures/source_packs/media-*.json` are synthetic. They are not
captures.

## Scope and exclusions

The records are works, editions (manifestations), recordings, releases,
creators and authority links. Every record keeps its source, native ID,
revision and as-of time. The following are out of scope:

- full text, audio, video, cover images and any other media content;
- popularity data (ratings, reading-log counts, listen counts, sitelink counts used as popularity);
- rights clearance determinations (catalogue rights notes are never interpreted);
- bulk mirrors of any provider.

## Per-source decisions

| Source | Access | Licence | Rate limit and User-Agent | Revision marker | Decision |
| --- | --- | --- | --- | --- | --- |
| Open Library API | `https://openlibrary.org/{works,books,authors}/{OLID}.json`, `/isbn/{ISBN}.json` | The catalogue data is published for reuse (developers licensing page; the operator confirms the terms before redistribution). Covers and scanned books are governed separately and are excluded. | An identifying `User-Agent` with app name and contact is required. At most 1 request per second. | `revision` (integer, one per edit) plus `last_modified`. Earlier revisions can be read with `?v=N`. `/type/redirect` (with `location`) and `/type/delete` are kept as history. | acquire |
| Open Library dumps | Monthly dumps on archive.org (`ol_dump_{works,editions,authors}_YYYY-MM-DD.txt.gz`, TSV: type, key, revision, last_modified, JSON). | as above | Not fetched by the runtime. | as the API | acquire named slices only |
| MusicBrainz | Web service `https://musicbrainz.org/ws/2/{recording,release,work,artist}/{MBID}?fmt=json&inc=...` | Core data is CC0. Supplementary data (annotations, tags, genres, ratings, edit history, user data) is CC BY-NC-SA and is excluded. Cover Art Archive images are excluded. | 1 request per second on average per IP. HTTP 503 means throttled. A meaningful `User-Agent` (`app/version (contact)`) is required. | WS/2 has no edit ID per entity. The revision marker is the SHA-256 of the stored CC0 core payload (`mb-core-digest`), ordered by acquisition. A merged MBID answers with the surviving entity, and the request is kept as a redirect. | acquire (CC0 core only) |
| Wikidata | `https://www.wikidata.org/w/api.php?action=wbgetentities&ids={QID}&props=info\|labels\|claims&format=json` | CC0 (structured data) | Wikimedia User-Agent policy. Requests are serial, and `maxlag` is honoured. Bulk JSON dumps are never processed. | `lastrevid` (the revision ID) plus `modified`. Redirects (`redirects.from/to`) and missing items are history. Deprecated statements keep their rank. | acquire by QID |
| Deutsche Nationalbibliothek | SRU `https://services.dnb.de/sru/authorities` (GND) and `https://services.dnb.de/sru/dnb` (catalogue), `recordSchema=MARC21-xml`. No access token is needed for SRU (verify). | CC0 1.0 for DNB metadata. Digitised tables of contents, cover images and full texts are excluded. | Serial requests with an identifying `User-Agent`. No published quota. | MARC 005 (date and time of latest transaction). Leader/05 `d` means deleted. 682 with a `(DE-588)` `$0` means redirected (Umlenkung, verify). | acquire (authorities by GND ID, catalogue by IDN or ISBN) |
| Library of Congress | `https://id.loc.gov/authorities/names/{LCCN}.marcxml.xml` (LCNAF) and `https://lccn.loc.gov/{LCCN}/marcxml` (catalogue) | Library of Congress bibliographic and authority metadata has no known copyright restrictions (US government work; verify for records contributed by others). | Serial, polite requests with an identifying `User-Agent`. At most 1 request per second. | MARC 005. Leader/05 `d` or `s` means deleted or replaced. 682 and 010 `$z` identify the replacing heading. | acquire (LCNAF by LCCN, catalogue by LCCN) |

Identifier types used by these sources are ISBN-10/13 (checksum-verified; an
ISBN-10 meets its ISBN-13 form), ISRC, ISWC, MBID, Wikidata QID, GND ID, DNB
IDN, LCCN/LCNAF and OLID. VIAF and ISNI identifiers are kept only as
identifier strings that a provider published.

## Bounded coverage

- **Identifier classes.** Selections name explicit OLIDs, ISBNs, MBIDs with
  their entity type, QIDs (optionally a pinned revision ID), GND IDs, DNB IDNs
  and LCCNs. There is no free-text search against any provider. Title and
  creator search runs only over records already acquired.
- **Selections.** 1 to 50 selectors per source, and one provider request per
  selector. DNB SRU answers at most 5 records per request.
- **Record classes.**
  - Open Library: works, editions, authors.
  - MusicBrainz: recordings, releases, works, artists.
  - Wikidata: items reached from a named QID.
  - DNB: GND person, corporate-body and work authorities, and catalogue records.
  - Library of Congress: LCNAF names and catalogue records.
- **Open Library dump slices.** The full dump is never downloaded or mirrored.
  An operator cuts a named slice from one pinned dump file. The slice is
  filtered to an explicit key list of at most 500 lines. The operator imports
  it with `MediaMetadataStore.import_dump_slice`, which records the dump name
  and date, the slice SHA-256, the requested keys and the keys that were
  found. A slice line whose key was not requested is refused.
- **Fixture coverage (synthetic).**
  - A book work with two editions (Open Library, DNB, Library of Congress).
  - The book's author (Open Library, Wikidata, GND, LCNAF), including a deprecated GND record redirected to its successor.
  - A recording with an ISRC and its work, release and artist (MusicBrainz, Wikidata), including one MBID that was merged into another.

## Excluded record classes

- cover images (Open Library covers, Cover Art Archive), audio and video;
- full text, scanned books, excerpts and descriptions (`ocaid`, `excerpts`, `description` and `first_sentence` are dropped);
- MusicBrainz supplementary data: annotations, tags, genres, ratings, user tags and ratings, edit history;
- popularity data (ratings, reading-log counts and similar signals);
- catalogue rights notes as rights determinations (MARC 506/540 are not stored);
- identifier schemes outside the audited list (for example commercial store IDs published by Open Library).

## Excluded sources

| Source | Reason |
| --- | --- |
| Wikidata JSON dumps and unbounded SPARQL | No bulk processing. Lookups go by named QID. Bounded SPARQL is not needed for this coverage. |
| OCLC WorldCat / VIAF API | WorldCat is licensed. VIAF identifiers are kept only as published identifier strings. |
| DNB bulk data shop dumps and Culturegraph | These are bulk mirrors. SRU per identifier covers the bounded scope. |
| Library of Congress Z39.50 / legacy SRU (lx2.loc.gov) and Classification Web | Z39.50 and the legacy SRU are superseded by id.loc.gov and lccn.loc.gov. Classification Web is a subscription product. |
| Discogs | Non-core data under a mixed licence. It is outside the selected providers. |

## Revision semantics (summary)

Revisions are immutable and keyed by the provider's revision marker. A later
acquisition of the same marker adds nothing. If the same marker arrives with
different content, the store records a conflict instead of overwriting.

- An older revision that arrives late becomes history, and the current
  revision does not change.
- MusicBrainz revisions are ordered by acquisition, because the web service
  exposes no edit marker.
- Redirects, merges, deletions and deprecations become revisions of the old
  record (`status` is `redirected`, `deleted` or `deprecated`). They are also
  recorded as `redirect` decisions in the entity history store.
