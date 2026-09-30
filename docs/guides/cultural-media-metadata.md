# Cultural media metadata: books, music and authority records

This guide covers works, editions (manifestations), recordings, releases,
creators and authority links from five providers:

- Open Library;
- MusicBrainz (CC0 core data only);
- Wikidata;
- the Deutsche Nationalbibliothek (GND and catalogue);
- the Library of Congress (LCNAF and catalogue).

The records serve as the identity backbone for
[Cultural Collections](cultural-collections.md) and as reviewable candidates
for News entity resolution. There is no separate pack. The sources are part of
the scientific source pack (`config/source_packs/scientific.json`, version
1.2.0). The provider is `science.media-metadata` in the Science pack, and the
feature is optional and off by default. Tracking:
[#2225](https://github.com/Ikey168/Noesis/issues/2225).

Access, licence, rate-limit and revision decisions are recorded in the
[source audit](../development/cultural-evidence/media-metadata-source-audit.md).
Out of scope: full text, audio, cover images, popularity rankings and rights
clearance determinations.

Demonstration question: *"Which work, editions and authority records does
this title, author or identifier refer to, and what did each authority say on
a given date?"*

## Enable

The Science bundle has two optional features, both default off:

- `media-metadata`: the records, identity, cultural links, answers and
  monitors. It binds `science.cultural`, `platform.entity-identity`,
  `platform.subscriptions` and `platform.source-runtime`.
- `media-metadata-news`: offers authority records as reviewable candidates
  for News canonical entities, and binds `news.core`.

Select a feature through the composition, for example with
`coordinator.select("science", version, features=["media-metadata"])`.
`media_metadata_status` reports each feature's selection and the
`LIVE_VERIFICATION` state of every source.

## Sources and selection

| Source ID | Provider | Selectors (explicit, 1-50) |
| --- | --- | --- |
| `openlibrary-media` | Open Library API | OLID (work, edition, author) or ISBN |
| `musicbrainz-media` | MusicBrainz WS/2 | entity type (recording, release, work, artist) and MBID |
| `wikidata-media` | Wikidata `wbgetentities` | QID, optionally a pinned revision ID |
| `dnb-gnd-media` | DNB SRU `authorities` | GND ID |
| `dnb-catalogue-media` | DNB SRU `dnb` | IDN or ISBN |
| `loc-lcnaf-media` | id.loc.gov | LCNAF LCCN |
| `loc-catalogue-media` | lccn.loc.gov | LCCN |

Run the sources with operation `media`. They go through
`src/ingestion/source_pack_runtime.py`, with licence acceptance, budgets,
receipts and checkpoints.

- **Pacing.** Each provider's minimum interval (MusicBrainz 1.1 s, Open
  Library and LoC 1 s, Wikidata and DNB 0.5 s) paces live requests.
- **User-Agent.** The optional `NOESIS_MEDIA_METADATA_CONTACT` secret adds the
  contact that the Open Library, MusicBrainz and Wikimedia User-Agent rules
  ask for.
- **Open Library dump slices.** Import them with
  `MediaMetadataStore.import_dump_slice`. A slice is cut from one named
  monthly dump, filtered to at most 500 requested keys. The full dump is never
  mirrored.

## Records and revisions

There is one record per source and native ID. Each revision is immutable and
keyed by the provider's revision marker:

| Provider | Revision marker |
| --- | --- |
| Open Library | `revision` |
| Wikidata | revision ID (`lastrevid`) |
| DNB, Library of Congress | MARC 005 |
| MusicBrainz | digest of the CC0 core payload (WS/2 exposes no edit marker) |

- **Late and repeated revisions.** A late, older revision becomes history. A
  replay adds nothing.
- **Conflicts.** The same marker with different content is recorded as a
  conflict and is never overwritten.
- **Levels.** Works and editions, and recordings and releases, are separate
  record types. A record's level is fixed at its first revision.
- **Identifiers.** Identifiers are typed and keep their published form.
  ISBN-10 and ISBN-13 checksums are checked, and the two forms of the same
  ISBN meet. ISRC, ISWC, MBID, QID, GND, IDN, LCCN/LCNAF, OLID, VIAF and ISNI
  are also supported.
- **Redirects.** Provider redirects, MusicBrainz merges, deletions and
  deprecations are revisions of the old record. Redirects and merges are also
  entity-history `redirect` decisions.

## Identity and links

`propose_media_identity_matches` works in four steps:

- **Explicit identifier statements** become matches that cite the asserting
  revision and property. Examples: Wikidata P648/P227/P244/P434/P435, Open
  Library identifiers and remote IDs, MusicBrainz URL relations, MARC 024/010.
- **Shared identifiers** (ISBN, ISRC, ISWC, barcode) match across sources.
  When one source claims an identifier for two records (an ISBN claimed by two
  works), the claim is a **conflict** and waits for review.
- **Title, creator and date similarity** only proposes scored candidates.
- **Creators** are proposed against News `canonical_entities`. That table is
  only read and never re-labelled.

Reviews (`review_media_identity_match`) are entity-history decisions, and
`revert_media_identity_match` undoes them. Records are never merged.

`link_media_cultural_objects` links a cultural object only through a creator
authority ID or a provider relation (`same_as`) in the object record. Keyword
overlap only creates a candidate. `link_media_news_entities` turns creator
candidates into news-entity link candidates. Every link cites the revisions on
both sides.

## Answers

- `resolve_media_identifier` looks up an identifier exactly. An optional ISO
  `as_of` date is supported. The answer contains:
  - the identity;
  - its authority records, each with source, revision marker, revision date,
    retrieval time and licence;
  - the revision history known at the date, and a count of later revisions;
  - the matches used and the open candidates;
  - redirect chains.
- Unknown identifiers, invalid checksums and ambiguous identities are reported
  as such.
- `search_media_titles` returns ranked candidates for a title, a creator or
  both. There is one candidate per identity, each with its title and name
  evidence, and ties are flagged as ambiguous. The tool never asserts a single
  answer and never ranks by popularity.
- `media_authority_history` lists every revision of one record.
- `export_media_evidence_bundle` returns the answer as a
  `noesis-evidence-bundle-v1`.

## Monitoring

`create_media_metadata_monitor` watches records or identifiers as a knowledge
subscription. `run_media_metadata_monitor` and `poll_media_metadata_monitor`
deliver dated events that cite both revisions:

- `authority_revision`;
- `redirect`;
- `merge`;
- `deprecation`;
- `identifier_assertion`.

`MediaMetadataMonitor.refresh` re-acquires a source's pinned selection within
its budgets and pace. It writes a receipt and stops at the first rate-limit
answer.

## Evidence

- **Offline.** `tests/unit/domains/test_media_metadata_acceptance.py` replays
  the synthetic fixtures (`tests/fixtures/source_packs/media-*.json`) with
  sockets blocked. A book title and creator, and a music recording, resolve to
  cited authority records with revisions and identity matches. Negative cases:
  - an ambiguous title;
  - a conflicting ISBN;
  - a deprecated (redirected) GND authority;
  - an unknown identifier.
- **Live.** No live run has been made, and every source is `unverified-live`.
  Live validation and the cited demo are tracked separately in
  [#2513](https://github.com/Ikey168/Noesis/issues/2513).
