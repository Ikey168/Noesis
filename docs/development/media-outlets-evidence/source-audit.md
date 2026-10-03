# Media outlets and ownership: source-contract audit and bounded coverage (MO01)

Tracking: wave 2 tracker #2736 · recorded 2026-10-03.

No per-track tracker or delivery issue exists yet. They are opened once an
audit names a surviving source. **This audit names none yet**: no candidate
has a documented machine-readable access path the author can confirm, so every
source is `not-implemented` and the `media-outlets-ownership` subdomain stays
a gap until a live check or a revised audit finds one (see "Path to a
surviving source").

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

Further candidates, not audited here (for a revised audit): national
regulators' licence registers (Ofcom broadcast licences, the media
authorities' TV station database, _verify_ machine access); the media-ownership
disclosures and national databases required by Article 6 of the European
Media Freedom Act (Regulation (EU) 2024/1083, applicable from 8 August 2025,
_verify_ what Member States publish); and Wikidata ownership statements (CC0,
machine-readable through the existing `wikidata` connector of
`src/ingestion/media_metadata_sources.py`, but crowd-sourced: usable only as
"what Wikidata states" with its references, never as a register).

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

Any fixtures written for a surviving source will be authored, not captured:
fictional outlets, companies and persons, statement dates in 2094-2097 and
update stamps in 2098-2099, so nothing can be mistaken for a published record.
