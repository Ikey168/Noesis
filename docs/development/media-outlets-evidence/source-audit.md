# Media outlets and ownership: source-contract audit and bounded coverage (MO01)

Tracking: wave 2 tracker #2736 · recorded 2026-10-03 · amended 2026-10-03
(MO01 amendment, #2754).

No per-track tracker or delivery issue exists yet. They are opened once an
audit names a surviving source. The original MO01 audit named none. **The
amendment names one narrow survivor: Wikidata ownership statements, read
through the existing `wikidata` connector, kept as "what Wikidata states"
with its references and never as a register** (`unverified-live`). No
register-grade source survives: Ofcom's licence lists and the German media
authorities' TV station database stay `not-implemented` until a file or
export and reuse terms are confirmed, and no national database under Article
6 of the European Media Freedom Act is confirmed to the author. See
"Amendment: further candidates (#2754)" for the reasoning and "Conclusion of
the amendment" for what may open.

**The amendment was written offline, like the original audit.** No publisher
host (`www.ofcom.org.uk`, `www.die-medienanstalten.de`, the Member State
regulators' sites, `www.wikidata.org`) was reachable from the authoring
runtime, and nothing was fetched. It rests on the publishers' documentation as
the author knows it and on this repository's own `wikidata` connector. Every
item marked _verify_ is unchecked.

This audit sets out, per candidate, what the News pack's proposed
`news.outlets` provider (subdomain `media-outlets-ownership`, ADR-005) could
acquire, how, on what terms, and how the personal data of owners who are
natural persons would be minimised. **It was written without network access:
the publishers' hosts (`www.mom-gmr.org`, `mavise.obs.coe.int`,
`www.kek-online.de`) were blocked by this runtime's egress proxy (HTTP 403 on
CONNECT, verified 2026-10-03), so terms, interfaces and field names were not
re-verified live.** Every item marked _verify_ must be checked against the live
pages and terms before any acquisition issue is opened.

**The machine-readable copy does not exist yet.** If a source survives,
`PROVIDER_CONTRACTS`, `BOUNDED_COVERAGE`, `MINIMISATION` and
`LIVE_VERIFICATION` will be added in `src/ingestion/media_outlets_sources.py`
by the track's acquisition issues and must match this audit. Until then the
decisions below exist only here.

Non-goals for every source: no media bias, political leaning, reliability or
quality ratings; no pluralism, concentration or market-power scores (no
audience-share sums, HHI or risk indicators of our own); no "independent" or
"state-controlled" label beyond what a source states; nothing feeds the
outlet scores or clusters of `src/argument_mining/outlet_scorer.py` and
`src/argument_mining/outlet_clustering.py`. Ownership is kept as each source
states it; conflicting statements from different sources are separate
records and are never reconciled.

## Access decisions

| Source (proposed source-pack id) | Publisher | Delivers | Access | Decision |
| --- | --- | --- | --- | --- |
| `media-ownership-monitor` | Reporters Without Borders and partners, Media Ownership Monitor (MOM) | per country, the leading outlets by audience, their owners (companies and persons), shares, and researched context including owners' political affiliations and family ties, as of the country's research year | HTML country sites; no documented API or export (_verify_) | `not-implemented` |
| `mavise-audiovisual` | European Audiovisual Observatory (Council of Europe), MAVISE | television channels and on-demand services by country of establishment, licensing authority, genre and language, with the operating company and its group | web database; an export or API is not documented to the author's knowledge (_verify_) | `not-implemented` (conditional, see below) |
| `kek-mediendatenbank` | Kommission zur Ermittlung der Konzentration im Medienbereich (KEK) | German nationwide private broadcasters, the licence holders' shareholding structures down to ultimate shareholders, and related press and online holdings | web database pages and published decisions (Beschlüsse, PDF); no documented API or export (_verify_) | `not-implemented` (conditional, see below) |

Reasons:

- **MOM.** No machine-readable interface is known; scraping the country
  sites is excluded. The research is a dated snapshot per country, not a
  maintained register. The pages record natural persons' political
  affiliations and family relations, which are special-category or
  profiling data this project does not store. The licence is _verify_ (the
  author believes it may be non-commercial). **Link-only:** a reviewer may
  cite a MOM country page as a reference on an outlet record; nothing is
  acquired from it.
- **MAVISE.** The data is the most register-like candidate, covering EU and
  other Observatory member states including the UK, and largely compiled from
  national regulators. Not implemented because neither a documented export or
  API nor reuse terms that allow storing extracts are confirmed (_verify_ both;
  some functions may need a registered account, _verify_).
- **KEK.** An official commission of the German media authorities, so its
  shareholding statements are authoritative for licensed broadcasters. Not
  implemented because no documented export or API is known and the reuse terms
  of the database pages are not stated to the author's knowledge (_verify_).
  Its decisions are official documents and may be cited link-only by URL and
  decision number (Aktenzeichen, _verify_ the scheme).

The further candidates the original audit listed but did not audit
(national regulators' licence registers, EMFA Article 6 disclosures and
national databases, Wikidata ownership statements) are audited in
"Amendment: further candidates (#2754)" below.

## Path to a surviving source

MAVISE or KEK becomes `unverified-live` only when an operator records, with
the page and date, (1) a documented export or API that returns one declared
outlet or company per request and (2) terms that allow storing the fields
below with attribution and showing them in cited answers. The audit is then
revised with the contract below filled in, and the acquisition issues are
opened. Without both, no scraper is written and the gap row stays.

## Per-source contract (as far as known)

| Source | Endpoints | Authentication and keys | Licence and redistribution | Rate limits | Revision model: updates, corrections, removals |
| --- | --- | --- | --- | --- | --- |
| MOM | country sites under `mom-gmr.org` (_verify_ host and paths) | none | _verify_; link-only | n/a | research year per country; not acquired |
| MAVISE | `https://mavise.obs.coe.int/` (_verify_ any export or API path) | none expected; if an account is required, a required secret `NOESIS_MAVISE_ACCOUNT_TOKEN` would be declared and the source recorded `gated-not-granted` until granted | Council of Europe / Observatory terms (_verify_); attribution "European Audiovisual Observatory, MAVISE database" (_verify_ wording) | _verify_ | entries change as regulators report (cadence _verify_); a changed channel or company is a new revision keyed by the MAVISE identifier (_verify_ that stable ids exist); a channel no longer listed is a `not-returned` observation, never a stated closure |
| KEK | `https://www.kek-online.de/` database pages (_verify_ paths) and decision PDFs | none | KEK terms (_verify_); attribution to KEK | _verify_ | shareholding structures are updated after notified changes and KEK decisions (_verify_); a changed structure is a new revision citing the decision that states it, when one is stated |

**Unavailable-access fallback.** While every source is `not-implemented`,
the provider (if scaffolded) reports the subdomain as uncovered with the
reason; it never answers an outlet or ownership query as empty.

## Data minimisation decision

Owners and controllers of outlets include natural persons who are not public
office holders. Policy `media-outlets-minimisation-v1`, to be enforced in the
parser and again at write time, follows the person and ownership-assertion
pattern of `src/kb/ownership_records.py`:

- **Stored:** outlets (name, type, country of establishment, licensing
  authority and licence reference, language, genre, operating company), legal
  entities with their stated identifiers, and ownership or control assertions
  as stated (holder, subject, kind, share as an exact decimal or a band with
  explicit bounds, statement date). A natural-person holder is stored as name,
  the source's own key and `kind: person` only.
- **Excluded:** for natural persons, addresses, dates of birth, nationality,
  residence, family relations, political affiliation or party membership,
  biographies and photographs; MOM's researched context and risk indicators in
  full.
- **Never matched:** natural-person holders are excluded from identity
  proposals. Companies are proposed to Corporate Ownership legal entities by
  stated identifiers through `src/kb/ownership_identity.py`; outlets are
  proposed to news sources through `src/kb/source_identity.py` alias
  decisions, and an accepted match may add an `ownership` relationship
  revision there that cites the registry record. Nothing is merged.
- **Retention:** revisions are retained for provenance; historical ownership
  is the subject. If a source withdraws a person's data, the next acquisition
  records that and an audited minimisation revision removes the person's name
  from earlier revisions.
- **Who may query:** `knowledge:news:outlets:read` with namespace access;
  natural-person holders' names additionally need
  `knowledge:news:outlets:persons:read` (others see `kind: person` and the
  stated share); writes need `knowledge:news:outlets:write`, reviews
  `knowledge:news:outlets:review`.

## Record shape and reuse

Shape from `packs/taxonomy.json`: **registry-records** (outlet and company
entries and ownership assertions with revisions and reviewable identity). No
new shape. Reuse: `src/kb/ownership_records.py` record kinds,
`src/kb/ownership_identity.py` and `src/kb/entity_history.py` for identity,
`src/kb/source_identity.py` for the link to news sources (`news.core`), and the
corporate chain above an outlet's operator from the existing ownership
providers (`src/ingestion/ownership_providers.py`), not re-acquired.

## Bounded first coverage (only if a source survives)

| Source | Selection | Caps |
| --- | --- | --- |
| KEK | the licence holders of one declared German broadcasting group and their shareholding chain | 10 broadcasters, 3 shareholding levels, 50 assertions |
| MAVISE | the channels of the same declared German group, and of one declared UK group | 50 channels, 20 companies |

Justification: one German group seen by both KEK and MAVISE shows two
sources' statements side by side without reconciling them; one UK group
shows a second jurisdiction and links to Companies House records. Every
further group or country is a source-pack version bump.

## Live verification

| Source | Status | Checked | Evidence |
| --- | --- | --- | --- |
| MOM | `not-implemented` | - | none; link-only |
| MAVISE | `not-implemented` | - | none; export and terms unverified |
| KEK | `not-implemented` | - | none; export and terms unverified |
| Ofcom licence lists (#2754) | `not-implemented` | - | none; file path, format and terms unverified |
| Medienanstalten TV station database (#2754) | `not-implemented` | - | none; export and terms unverified |
| EMFA Art. 6 national databases (#2754) | `not-implemented` | - | none; no Member State database confirmed |
| Wikidata ownership statements (#2754) | `unverified-live` | - | none for ownership properties; the `wikidata` connector itself is `unverified-live` in `src/ingestion/media_metadata_sources.py` |

Any fixtures written for a surviving source will be authored, not captured:
fictional outlets, companies and persons, statement dates in 2094-2097 and
update stamps in 2098-2099, so nothing can be mistaken for a published record.

## Amendment: further candidates (#2754)

Recorded 2026-10-03 (MO01 amendment). **Written offline:** no host named
below was reached and nothing was fetched; terms, file paths, field names,
Wikidata property and class identifiers and each Member State's database are
from the author's knowledge of the publishers' documentation and are marked
_verify_ where unchecked. The only facts confirmed are those in this
repository: what the `wikidata` connector requests and parses
(`src/ingestion/media_metadata_sources.py`). No source here is `live`.

The non-goals, the policy `media-outlets-minimisation-v1`, the record shape
and the reuse set out above apply unchanged; the decisions below only add to
them.

### Access decisions

| Source (proposed source-pack id) | Publisher | Delivers | Access | Decision |
| --- | --- | --- | --- | --- |
| `ofcom-broadcast-licences` | Ofcom (UK communications regulator) | lists of licensed TV services (Television Licensable Content Services, digital television programme and additional services, local TV) and radio services, each with the licence number, service name and licensee (_verify_ columns) | downloadable lists on `www.ofcom.org.uk`, believed to be spreadsheets republished periodically (_verify_ path, format and cadence); no API known (_verify_) | `not-implemented` (conditional, see below) |
| `medienanstalten-tv-senderdatenbank` | die medienanstalten (the joint body of the German state media authorities) | licensed private TV channels with licence holder, licensing state media authority and channel type; nationwide channels link to KEK's shareholding pages (_verify_ all) | web database pages on `www.die-medienanstalten.de` (_verify_ path); no documented API or export (_verify_) | `not-implemented` (conditional, see below) |
| `emfa-art6-<member-state>` | national regulatory authorities or other bodies designated under Article 6 of the European Media Freedom Act (Regulation (EU) 2024/1083) | ownership disclosures of news and current-affairs media service providers: legal name, direct and indirect owners, beneficial owners, and state advertising figures (_verify_ the list in Art. 6(1)) | either each provider's own website (Art. 6(1)) or a national database where a Member State has built one (Art. 6(2), _verify_ whether mandatory); no common format or interface | `not-implemented` |
| `wikidata-media-ownership` | Wikimedia Foundation (Wikidata), reusing the `wikidata` connector | ownership and control statements on outlet items, as Wikidata states them, with rank, qualifiers and references | `GET /w/api.php?action=wbgetentities&ids={QID}&props=info\|labels\|claims&format=json&maxlag=5`, one declared QID per request (the connector's `request_for`, confirmed in code) | `unverified-live` (statements only, never a register) |

Reasons:

- **Ofcom licence lists.** The most register-like candidate: an official
  regulator's list of licence holders, and UK public-sector information is
  usually reusable under the Open Government Licence v3.0 (_verify_ that
  Ofcom's website terms cover these files and state no exception). It is not
  an ownership register: it names the licensee only, not its shareholders
  (_verify_ that no shareholder column exists), so it gives the first link
  (service to licence holder) and the chain above comes from Companies House
  through the existing ownership providers, as the audit already decided.
  Not implemented because the file path, format, columns, cadence and terms
  are all unverified offline. It becomes `unverified-live` on the same two
  conditions as MAVISE and KEK ("Path to a surviving source"): a documented
  file or export, and terms that allow storing and showing the fields. A
  periodically republished spreadsheet satisfies (1) if it is reachable by a
  stable link; scraping Ofcom's licence search pages is excluded. The UK is
  outside the EU, so the EMFA does not apply to Ofcom.
- **German media authorities' TV station database.** Authoritative for who
  holds the licence and which state authority granted it; shareholdings of
  nationwide broadcasters are KEK's, already audited above. Not implemented
  for the same reason as KEK: no documented export or API and no stated
  reuse terms known to the author (_verify_ both). Pages may be cited
  link-only. The medienanstalten's media-diversity monitor (opinion-power
  shares) is excluded outright: it is a concentration measure, a stated
  non-goal.
- **EMFA Article 6.** The Regulation applies from 8 August 2025 (_verify_
  for Article 6 specifically). Disclosures under Art. 6(1) sit on each
  provider's own website, in no common format: acquiring them would mean
  crawling outlet sites, which is excluded; a reviewer may cite a provider's
  disclosure page link-only. National databases under Art. 6(2) would be the
  register-grade source, but the author cannot confirm, offline, which
  Member States have built one, what it holds, or whether it offers an
  export. The candidates the author believes exist, all _verify_, are listed
  in the table below; none is accepted. Beneficial-owner names in these
  disclosures are personal data whose general public availability the Court
  of Justice limited for AML registers (joined cases C-37/20 and C-601/20,
  22 November 2022); the EMFA's sector rule makes media beneficial owners
  public, but re-publishing them in a third-party knowledge base needs its
  own legal check (_verify_) before any acquisition.
- **Wikidata.** The access path exists and is documented in this
  repository: the `wikidata` connector already fetches one item per request
  with all its claims, under CC0 1.0, paced at 500 ms with `maxlag=5`, with
  `lastrevid` as revision, redirects and missing items as history, and the
  source is `unverified-live` there. It therefore meets both conditions of
  "Path to a surviving source" on facts confirmed in code. It survives only
  narrowly: Wikidata is crowd-sourced, so every record says "Wikidata states,
  at revision N, that X is owned by Y" with the statement's references, and
  is never presented as a register, never preferred over a regulator's
  statement and never reconciled with one. **Not confirmed in code:**
  `parse_wikidata` today keeps identifier properties, creators (P50, P175,
  P86), P629 and P577 only; it drops every other property and all
  qualifiers, and keeps references only on identifier claims. Ownership
  statements therefore need a separate parser in the track (the MM05 records
  of the media-metadata pack are not changed); no code is written here.

| Member State (believed database, all _verify_) | Body (_verify_) | What the author believes it holds | Machine access | Decision |
| --- | --- | --- | --- | --- |
| Portugal | ERC, transparency portal (Lei n.º 78/2015) | ownership, management and financing of registered media entities | search pages; no export known | `not-implemented` |
| Italy | AGCOM, Registro degli Operatori di Comunicazione (ROC) | registered communication operators and their ownership disclosures | limited public search; no export known | `not-implemented` |
| Spain | register of audiovisual media service providers under Ley 13/2022 (keeper _verify_: ministry or CNMC) | providers and ownership | search pages; no export known | `not-implemented` |
| Croatia | Agency for Electronic Media (AEM) | ownership structures of electronic media providers | pages and documents; no export known | `not-implemented` |
| Lithuania | information system of public information producers and disseminators (keeper _verify_) | producers, disseminators and their participants | search pages; no export known | `not-implemented` |
| Germany | KEK and the medienanstalten (designation under Art. 6 _verify_) | see KEK and the TV station database above | see above | see above |

This list is the author's recollection, not a survey: Member States not
listed may publish a database and listed ones may not. A revised audit with
network access enumerates the designated bodies (the Commission or the
European Board for Media Services may publish the list, _verify_).

### Per-source contract (as far as known)

| Source | Endpoints | Authentication and keys | Licence and redistribution | Rate limits | Revision model: updates, corrections, removals |
| --- | --- | --- | --- | --- | --- |
| Ofcom licence lists | file links on `www.ofcom.org.uk` (_verify_ paths, format, columns) | none | Open Government Licence v3.0 (_verify_); attribution "Contains Ofcom data" or as Ofcom states (_verify_ wording) | none published (_verify_); one file per run | each republished file is a dated snapshot (_verify_ that it carries a date); revisions are derived by comparing snapshots keyed by licence number; a licence missing from a later snapshot is a `not-returned` observation, never a stated revocation, unless an Ofcom decision states one and is cited |
| Medienanstalten TV station database | pages on `www.die-medienanstalten.de` (_verify_ path); no export known | none | not stated to the author (_verify_); link-only until stated | _verify_ | licence changes after state-authority decisions (_verify_); a changed holder is a new revision citing the decision when one is stated |
| EMFA Art. 6 national databases | per Member State (table above, all _verify_) | none expected; any account requirement makes the source `gated-not-granted` | per Member State (_verify_); beneficial-owner names need the legal check above | _verify_ | per Member State; disclosures are updated by providers (_verify_ cadence); a withdrawn disclosure is `not-returned`, never a stated divestment |
| Wikidata ownership statements | the connector's `wbgetentities` request, one declared QID per request, optionally pinned with `revision` (confirmed in code). Properties, all _verify_: P127 owned by, P749 parent organisation, P355 has subsidiary (read only on declared outlet or operator items), P137 operator; qualifiers P1107 proportion, P580 start time, P582 end time, P585 point in time; references P248 stated in, P854 reference URL, P813 retrieved, P143 imported from Wikimedia project. Outlet classes for P31, all _verify_: Q11032 newspaper, Q2001305 television channel, Q1616075 television station, Q14350 radio station | none; Wikimedia User-Agent policy with the optional `NOESIS_MEDIA_METADATA_CONTACT` contact the connector already declares | CC0 1.0 (confirmed in the connector's contract); attribution "Wikidata" plus each statement's own references | the connector's 500 ms interval and `maxlag=5`; HTTP 429 or a maxlag error stops the run | `lastrevid` is the item revision. Each statement keeps its Wikidata statement id, rank (`preferred`, `normal`, `deprecated`) and qualifiers as stated; a deprecated statement is kept with its rank and never shown as current. Currency is never inferred: a statement without P582 is "no end time stated", not "current owner". A statement absent from a later revision is `not-returned` at that revision, never a stated divestment. A redirect or deleted item follows the connector's existing history handling. A statement whose only reference is P143 is marked `reference: wikimedia-import`, not sourced |

**Unavailable-access fallback.** As above: a source that is not
implemented, or a Wikidata run that fails (HTTP error, 429, maxlag, schema
drift), is reported with its code and receipt; no outlet or ownership query
is answered as empty, and earlier revisions stay current.

### Data minimisation decision (additions to `media-outlets-minimisation-v1`)

Ownership chains name natural persons in all four candidates (Wikidata
items for owners, Ofcom licences held by individuals, EMFA beneficial
owners). The existing policy applies; in addition:

- **Wikidata, traversal only upward.** Acquisition starts at a declared
  outlet item and follows ownership properties upward to the holders. It
  never starts from a person or follows a person's holdings (P1830 "owner of"
  on a human item is not read), so no person's portfolio is assembled.
- **Wikidata, holder items.** A holder item is fetched once to learn whether
  it is a human (P31 Q5, _verify_) or an organisation. For a human item
  everything except the QID, one label, `kind: person` and P31 is discarded
  at parse time: no date of birth (P569), place of birth (P19), citizenship
  (P27), residence (P551), party membership (P102), family relations (P22,
  P25, P26, P40) or image (P18) is stored, even transiently in a fixture.
  An organisation item keeps its label, P31 and stated company identifiers
  (for example LEI or a national company number, _verify_ property ids) for
  identity proposals through `src/kb/ownership_identity.py`.
- **Wikidata, unreferenced statements about persons.** A person-valued
  ownership statement with no reference, or only a P143 Wikimedia-import
  reference, is not stored; the run counts it in its receipt. Unreferenced
  statements between organisations are stored with `references: []`.
- **Ofcom.** Licensee names are stored as stated with the licence number.
  Ofcom's lists do not mark whether a licensee is a person (_verify_); a
  licensee without a company number or a company-form suffix is treated as a
  possible person (name behind `knowledge:news:outlets:persons:read`). Any
  address or contact column is dropped at parse time.
- **EMFA disclosures.** If a database is ever accepted, beneficial owners
  are stored as name and `kind: person` only, behind the persons scope;
  dates of birth, nationality, residence and the nature or extent of
  interest beyond the stated share band are excluded.

### Bounded first coverage

| Source | Selection | Caps |
| --- | --- | --- |
| Wikidata ownership statements | the outlet items of the same declared German group and declared UK group as the KEK and MAVISE rows above, plus their holders upward | 10 declared outlet QIDs, 3 levels upward, 40 item requests and 150 statements per run |
| Ofcom licence lists (only if it survives) | one dated snapshot of the TV licence list, restricted to the licences of the declared UK group | 1 file, 20 licences |
| Medienanstalten (only if it survives) | the channels of the declared German group | 10 channels |
| EMFA Art. 6 (only if a database survives) | one Member State's database, one declared group | 20 providers |

Justification: the same two groups across every source let Wikidata's
statements sit beside the regulators' and KEK's without reconciling them,
which is the comparison the track exists to show; one UK group links Ofcom
licensees to Companies House through the existing ownership providers.
Every further group, country or Member State is a source-pack version bump.

### Conclusion of the amendment

- **Survives, narrowly:** Wikidata ownership statements through the existing
  `wikidata` connector, `unverified-live`, as "what Wikidata states" with
  references, never as a register. This is enough for a per-track tracker
  to open if the project chooses to, with two conditions recorded here: its
  first issue is a bounded live check that the declared outlets actually
  carry referenced ownership statements (if fewer than half of the ten do,
  the track stops and the subdomain stays a gap), and the track reports its
  coverage as "Wikidata statements, not a register" wherever coverage is
  shown.
- **Does not survive yet:** Ofcom licence lists (the most promising
  register-grade candidate: one operator check of the file link, its columns
  and Ofcom's reuse terms decides it), the German media authorities' TV
  station database, and every EMFA Article 6 national database.
- **Gap:** `media-outlets-ownership` remains a gap in the taxonomy until a
  tracker's live validation passes; no register-grade ownership source is
  available.
