# Archives and historical records: source-contract audit and bounded coverage (AR01)

Tracking: wave 3 tracker #2814 · audit issue #2823 · recorded 2026-11-01.
Bundle decision: [ADR-007](../../architecture/decisions/ADR-007-culture-bundle.md)
(CU01, #2815).

No per-track tracker or delivery issue exists yet. This audit leaves one
source: **Archivportal-D archival descriptions, read through the existing
Deutsche Digitale Bibliothek (DDB) connector and its key, at finding-aid levels
above the file only** (`unverified-live`). Archives Portal Europe and the US
National Archives Catalog API are `gated-not-granted`: each needs a key issued
by the publisher that this deployment does not hold. Their terms look
compatible with the intended use (_verify_), so they are the track's second and
third publishers once keys are granted.

**Written offline.** The publisher hosts (`www.archivesportaleurope.net`,
`api.archivesportaleurope.net`, `www.archivportal-d.de`,
`api.deutsche-digitale-bibliothek.de`, `catalog.archives.gov`) were blocked by
this runtime's egress proxy (verified 2026-11-01), and nothing was fetched.
Endpoints, field names, terms and the legal protection periods cited below come
from the publishers' documentation and the statutes as the author knows them.
Every item marked _verify_ is unchecked. The only facts confirmed are those in
this repository: the DDB connector, its CC0 metadata terms as recorded there and
its `NOESIS_DDB_API_KEY` secret (`src/ingestion/cultural_sources.py`,
`config/source_packs/scientific.json`). No source here is `live`.

**The machine-readable copy does not exist yet.** The track's acquisition
issues add `PROVIDER_CONTRACTS`, `BOUNDED_COVERAGE`, `MINIMISATION` and
`LIVE_VERIFICATION` for `culture.archives` and must match this audit.

Non-goals for every source: no family trees; no person matching, person
identity or record linkage across archives; no digitised images, scans,
transcriptions or full text; no person-level indexes or genealogical
databases; no access or rights determinations beyond what the archive states.

## Access decisions

| Source (proposed source-pack id) | Publisher | Delivers | Access | Decision |
| --- | --- | --- | --- | --- |
| `archivportal-d-descriptions` | Deutsche Digitale Bibliothek (Archivportal-D is its archive portal, operated with the Landesarchiv Baden-Württemberg, _verify_) | archival descriptions from German archives: fonds (Bestand), series (Gliederung) and lower units, with holding institution, reference code (Signatur), title, dates and parent, as contributed in EAD(-DDB) | the DDB API with the existing `NOESIS_DDB_API_KEY`: the search index restricted to the archive sector (_verify_ the sector filter value) and item and hierarchy endpoints for parents and children (_verify_ paths, believed `/items/{id}` with parent and children views) | `unverified-live` (descriptions above the file level only) |
| `archives-portal-europe` | Archives Portal Europe Foundation (APEF) | finding aids (EAD), records-creator descriptions (EAC-CPF) and institution descriptions (EAG) from archives across Europe | API with a key issued on registration and request to APEF (_verify_ the process and whether it is self-service) | `gated-not-granted` |
| `nara-catalog` | US National Archives and Records Administration (NARA) | catalog descriptions: record groups, collections, series, file units and items, with NARA identifiers (NAID), dates, creators and access and use restrictions; also user contributions (tags, transcriptions, comments) | Catalog API v2 (`catalog.archives.gov/api/v2`, _verify_) with an `x-api-key` key requested from NARA (_verify_: by email request to the catalog team) | `gated-not-granted` |

Reasons:

- **Archivportal-D.** The access path, its authentication and the CC0 terms for
  DDB metadata are already recorded in this repository for the
  `ddb-berlin-photographs` source of `science.cultural`. The track reuses the
  connector and key and adds archival-description handling; it does not change
  `science.cultural`. A DDB object that `science.cultural` holds is linked by
  DDB item id, never acquired twice (ADR-007, decision 1). Each record names its
  providing institution (DDB provider id); the reference code and the
  hierarchy are believed present for archive records (_verify_ field names).
  Per-record licence: DDB metadata is CC0 (repository contract; _verify_ for
  archive-sector records, which some archives may contribute under a different
  statement); a record whose metadata licence is stated as anything else is
  stored link-only.
- **Archives Portal Europe.** The most complete European finding-aid
  aggregator, in standard formats (EAD, EAC-CPF, EAG). To the author's
  knowledge its descriptive metadata is made available for reuse under CC0 by
  the content-provider agreement (_verify_), so the terms are not the blocker:
  the key is. Recorded `gated-not-granted` until an operator holds a key, then
  `unverified-live`. EAC-CPF descriptions of persons and families are excluded
  outright (see minimisation); EAC-CPF of corporate bodies may be read.
- **US National Archives Catalog.** NARA's own descriptions are US federal
  government works and so in the public domain in the US (_verify_ that the API
  terms add no condition). The described records may carry access or use
  restrictions, which are stored as NARA states them and never interpreted.
  User contributions (tags, transcriptions, comments) are third-party content and
  are excluded. Recorded `gated-not-granted` until a key is held (_verify_ the
  key requirement and the rate limits, believed to be a monthly quota). NARA's
  person-level databases (for example the Access to Archival Databases
  enlistment records and the released census schedules) are excluded under the
  minimisation decision regardless of access.

## Per-source contract (as far as known)

| Source | Endpoints | Authentication and keys | Licence and redistribution | Rate limits | Revision model: updates, corrections, removals |
| --- | --- | --- | --- | --- | --- |
| Archivportal-D (DDB) | `https://api.deutsche-digitale-bibliothek.de/2/search/index/search/select` (confirmed in the connector) with an archive-sector filter, and item and hierarchy endpoints (_verify_ paths) | `NOESIS_DDB_API_KEY` (existing required secret; DDB account, operator step) | DDB metadata CC0 1.0 (repository contract; _verify_ per archive-sector record); attribution to the holding institution and DDB | no published quota (_verify_); serial requests within the connector's budgets | DDB records change as archives re-deliver finding aids (cadence _verify_). A changed description is a new revision keyed by DDB item id; a re-delivered finding aid may assign new ids (_verify_), so the reference code plus holding institution is kept as a stated identifier and a reviewable identity proposal links old and new ids, never merging them. A unit no longer returned is `not-returned`, never "destroyed" or "deaccessioned" |
| Archives Portal Europe | APE API (_verify_ base URL and operations); EAD 2002 and EAC-CPF responses (_verify_) | API key from APEF; required secret `NOESIS_APE_API_KEY` (name proposed) | CC0 for descriptive metadata under the provider agreement (_verify_); attribution to the holding institution and APE | _verify_ | finding aids are republished by archives; each republication a new revision keyed by APE identifier and EAD id (_verify_ stability); not acquired until granted |
| NARA Catalog | `https://catalog.archives.gov/api/v2/...` (_verify_) | `x-api-key`; required secret `NOESIS_NARA_CATALOG_API_KEY` (name proposed) | public domain for NARA-authored descriptions (_verify_); user contributions excluded | quota per key (_verify_) | descriptions are edited in place; the revision marker is the record's stated modification date if present, else the SHA-256 of the stored payload ordered by acquisition (as the MusicBrainz source does); not acquired until granted |

**Holding institution, reference code and licence per record.** Answering the
issue: every stored description carries (1) its holding institution as the
source names it, with the source's institution id (DDB provider id, EAG
repository code or ISIL, NARA unit), (2) its reference code exactly as stated
(Signatur, `unitid`, NAID and record group number), never normalised, (3) its
parent unit, and (4) its metadata licence as stated per record. A record lacking
(1) or (2) is refused at write time and counted in the run receipt; a record
lacking (4) is stored link-only.

**Unavailable-access fallback.** A failed run is reported with its code and
receipt; earlier revisions stay current and nothing is marked removed. Queries
for APE and NARA report `gated-not-granted`, never empty.

## Data minimisation decision

Archival descriptions can name private and possibly living people, especially
at file and item level (personnel files, case files, civil-status and census
records). Policy `archives-minimisation-v1`, following ADR-007, decision 5, is
enforced in the parser and again at write time:

- **The bound: description levels above the file.** First coverage acquires
  only fonds, sub-fonds, series and sub-series (EAD `@level` `fonds`,
  `subfonds`, `series`, `subseries`, `collection`, `recordgrp`; NARA record
  groups, collections and series; _verify_ the DDB level vocabulary). File
  (`file`) and item (`item`) levels are not requested and are refused if
  returned.
- **No person-level indexes.** Not acquired: personal-name and family-name
  access points (EAD `persname` and `famname` in `controlaccess`, which are
  dropped at parse time), EAC-CPF descriptions of persons and families, name
  indexes, civil-status registers, census schedules, enlistment and passenger
  databases, and any other genealogical index.
- **Titles and scope notes as stated.** A unit's title and scope note are
  stored as the archive published them. A personal-papers fonds titled after a
  person (Nachlass) keeps its title; no person record or identity is created from
  it. A scope note longer than a declared cap (proposed 2,000 characters) is
  truncated with a marker, because long notes at series level sometimes list
  named individuals.
- **The cut-off for any later file-level coverage.** Not part of first
  coverage. An amendment may admit file-level descriptions only when the unit's
  latest stated date lies at least **110 years** before the acquisition year, a
  starting point chosen to exceed the protection periods the author knows
  (_verify_ each): the German Federal Archives Act's period for person-related
  records (10 years after death, 100 years after birth if the death date is
  unknown, 90 years after creation if neither is known, BArchG § 11), the
  corresponding Land archive laws, and NARA's 72-year rule for census records.
  Personal-name access points stay excluded even then.
- **Never matched or linked:** names in titles are never extracted, matched to
  persons, or linked across archives; no family tree is assembled.
- **Retention:** revisions are retained for provenance. If an archive withdraws
  or restricts a description, the next acquisition records `not-returned` and an
  audited minimisation revision can remove earlier text on request.
- **Who may query:** `knowledge:culture:archives:read` with namespace access;
  writes need `knowledge:culture:archives:write`, identity reviews
  `knowledge:culture:archives:review`.

## Record shape and reuse

Shape: **registry-records** (archival descriptions as catalogue entries with a
parent, revisions and reviewable identity). No new shape. Reuse: the DDB
connector, key and budgets of `src/ingestion/cultural_sources.py`; revision and
identity handling through `src/kb/entity_history.py`; citation-only links to
`science.cultural` objects by DDB item id and to the Legal bundle's records
where a fonds is a court's or ministry's record and the Legal record cites it
(link by stated identifier only).

## Bounded first coverage

| Source | Selection | Caps |
| --- | --- | --- |
| Archivportal-D | the top three description levels of 3 declared fonds held by 2 declared Berlin archives (for example the Landesarchiv Berlin, _verify_ that it delivers to Archivportal-D), and of 1 declared fonds whose latest date precedes 1900 | 4 fonds, 3 levels, 150 descriptions, 50 requests per run |
| Archives Portal Europe (only once granted) | the same declared fonds where APE also holds them, plus 1 fonds from a declared non-German archive | 2 finding aids, 100 descriptions |
| NARA Catalog (only once granted) | 1 declared record group and its series | 1 record group, 50 series |

Justification: one German city and a handful of fonds show the hierarchy,
reference codes and per-record licences; the pre-1900 fonds tests that the
level bound holds even where the protection periods would not. The same fonds
in APE and Archivportal-D show two sources' descriptions side by side without
reconciling them. Every further fonds or archive is a source-pack version bump.

## Live verification

| Source | Status | Checked | Evidence |
| --- | --- | --- | --- |
| Archivportal-D via DDB | `unverified-live` | - | none for archive-sector descriptions; the DDB connector itself is `unverified-live` |
| Archives Portal Europe | `gated-not-granted` | - | no APE API key held |
| NARA Catalog API | `gated-not-granted` | - | no NARA API key held |

Fixtures are authored, not captured: fictional archives, fonds and reference
codes, no real or fictional person names in any title or note, dates in
2094-2097 for revisions and historical dates for the described units.

## Conclusion

- **Survives:** Archivportal-D descriptions above the file level, through the
  existing DDB connector, `unverified-live`. A per-track tracker may open; its
  first issue is a bounded live check that the DDB API returns archive-sector
  records with holding institution, reference code and parent for the declared
  fonds.
- **Gated:** Archives Portal Europe and the NARA Catalog API, pending keys.
- **Personal data:** the bound is description levels above the file, no
  person-level index, and a 110-year cut-off (_verify_) as the starting point
  for any later file-level amendment.
- **Depth:** one publisher (DDB) until a key is granted; a delivering change adds
  a thin row (ADR-006). Note that DDB also backs `science.cultural`, which does
  not change this count: depth is per subdomain.
