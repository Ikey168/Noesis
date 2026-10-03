# Domain coverage program

Status: wave 1 covered offline, 2026-10-01; wave 2 source audits recorded,
2026-10-03 ([#2736](https://github.com/Ikey168/Noesis/issues/2736)), and the
wave 2 industry-business (IB) and tourism (TO) tracks covered offline, not
live; wave 3 planned. Decision:
[ADR-005](../architecture/decisions/ADR-005-domain-coverage-program.md).
Taxonomy: [`packs/taxonomy.json`](../../packs/taxonomy.json).

The nine domains of [ADR-004](../architecture/decisions/ADR-004-pack-taxonomy.md)
are divided into 89 subdomains. A subdomain is **covered** when at least one
classified provider names it. The program started with 56 covered and 33
gaps. Wave 1 and the wave 2 industry-business and tourism tracks are now
covered offline, so 68 are covered and 21 are gaps. This program fills every
gap, one track per subdomain.

## How coverage is counted

- **Offline coverage.** A fixture-tested provider names the subdomain. This is
  what `tests/unit/composition/test_pack_taxonomy.py` computes, and it must
  match the gap table below.
- **Live coverage.** The subdomain's provider has passed bounded live
  acceptance. This is tracked by the per-track "Validate live coverage"
  issues and is reported separately. Offline coverage is never reported as
  live coverage.

## Track rules

Each track follows the existing domain-track pattern:
1. **Source audit.** Name the providers, and record access, licence and
   redistribution terms before any code, as in the `*-source-audit.md`
   documents. The sources below are candidates only; none has been audited.
2. **Provider.** Add a descriptor, store, ingestion and MCP tools in the home
   bundle, fixture-tested offline. Reuse an existing record shape; a new
   shape needs a decision record first.
3. **Taxonomy.** Classify the provider in `packs/taxonomy.json` and remove its
   row from the gap table in the same change. The gate test fails until both
   agree.
4. **Live validation.** Open a "Validate live coverage" issue for the track.

Two domains had no bundle suited to their gaps, so two new bundles were
proposed: `society` (Society and population), created by wave 1, and `culture`
(Culture and leisure), still proposed. Every other gap becomes a provider in an existing bundle. Existing
providers stay where they are; their ids are preserved.

## Waves

- **Wave 1:** strong links to existing packs, and open sources with known
  machine access.
- **Wave 2:** useful, with some access or modelling work.
- **Wave 3:** restrictive terms, registration-gated data or narrow demand.

## Wave 2 source audits

Tracker: [#2736](https://github.com/Ikey168/Noesis/issues/2736). Each track's
audit is `docs/development/<dir>-evidence/source-audit.md`. All fourteen were
written without network access (publisher hosts blocked by the egress proxy,
2026-10-03), so unchecked terms are marked _verify_ and no source is live.
Per-track trackers and delivery issues are opened only for tracks whose audit
leaves at least one source.

| Code | Subdomain | Audit dir | Sources kept (`unverified-live`) | Not implemented or gated |
| --- | --- | --- | --- | --- |
| GT01 | government transparency | `government-transparency` | Bundestag DIP; UK written questions; FragDenStaat (metadata only) | none at the source level |
| SS01 | social protection | `social-protection` | Eurostat ESSPROS; OECD SOCX; ILOSTAT coverage | ILO dashboards (no machine access) |
| CV01 | civil society | `civil-society` | IRS EO BMF; IRS Form 990 e-file (needs range reads); Charity Commission API; 360Giving | none at the source level |
| TO01 | tourism | `tourism` | Eurostat occupancy and capacity | UN Tourism (no machine access, terms unclear) |
| IB01 | business | `business-statistics` | Eurostat STS and business demography; US Census CBP | none |
| OM01 | marine | `marine` | ERDDAP OISST; Argo via ERDDAP; EEA Natura 2000 marine sites | Copernicus Marine (gated); WDPA (terms); Argo GDAC/Argovis (first coverage) |
| LN01 | land and soils | `land-soils` | CORINE (EEA vector service); BKG CLC5; FAO FRA; BGR overview maps | CLMS API (gated); ESDAC (terms); BGS (first coverage) |
| WC01 | waste | `waste` | Eurostat waste and circular-economy; EEA waste transfers; OECD municipal waste | none |
| CD01 | mortality | `mortality` | WHO Mortality Database; WHO GHO; Eurostat causes of death; UN WPP files | UN Data Portal API (gated); IHME GBD (terms) |
| AH01 | animal health | `animal-health` | EFSA Knowledge Junction records | WAHIS (no API); EMPRES-i (gated); EFSA dashboards |
| II01 | internet infrastructure | `internet-infrastructure` | RIPEstat; PeeringDB; RDAP; crt.sh; CT log list | direct CT logs (unbounded); CAIDA (terms) |
| AI01 | AI models | `ai-models` | Hugging Face Hub metadata; OpenML; Epoch AI | none |
| CY01 | cyber incidents | `cyber-incidents` | SEC 8-K Item 1.05; Washington AG list (conditional) | HHS OCR portal (no API) |
| MO01 | media outlets | `media-outlets` | **none** | Media Ownership Monitor (no machine access, terms); MAVISE and KEK (until an export and terms are confirmed) |

IB01 is covered offline: `economics.business` (track
[#2738](https://github.com/Ikey168/Noesis/issues/2738)) holds fixture-tested
Eurostat STS, Eurostat business demography and Census CBP sources, so its gap
row is gone. Its sources stay `unverified-live`; live coverage waits for the
track's "Validate live coverage" run (IB13).

TO01 is covered offline: `economics.tourism` (track
[#2739](https://github.com/Ikey168/Noesis/issues/2739)) holds fixture-tested
Eurostat tourism occupancy and capacity sources, so its gap row is gone. Its
sources stay `unverified-live` and UN Tourism stays `not-implemented`; live
coverage waits for the track's "Validate live coverage" run (TO12).

MO01 leaves no source, so `media-outlets-ownership` stays a gap until an
amended audit finds one. AH01 rests on one source whose records are
themselves _verify_; if they do not exist, `animal-health` stays a gap too.

## Gap table

| Subdomain | Domain | Proposed home | Candidate sources (unaudited) | Links to | Wave |
| --- | --- | --- | --- | --- | --- |
| `government-transparency` | Governance and law | `political.transparency` | Bundestag DIP (questions, printed papers); UK Parliament written questions API; FragDenStaat | legislation, public finance | 2 |
| `defence-security` | Governance and law | `political.defence` | SIPRI military expenditure and arms transfers (terms restrict redistribution); UN Register of Conventional Arms; NATO defence expenditure reports | sanctions, humanitarian | 3 |
| `social-protection` | Society and population | `society.social-protection` (existing `society` bundle) | Eurostat ESSPROS; OECD SOCX; ILO social protection data | public finance, demographics | 2 |
| `public-opinion-wellbeing` | Society and population | `society.public-opinion` (existing `society` bundle) | Eurobarometer via GESIS; European Social Survey (registration); OECD How's Life | elections polls | 3 |
| `civil-society` | Society and population | `society.civil-society` (existing `society` bundle) | IRS exempt-organisation data and Form 990 filings; Charity Commission for England and Wales register; 360Giving | funding, lobbying, ownership | 2 |
| `oceans-marine` | Earth and environment | `environment.marine` | NOAA ERDDAP; Argo; Copernicus Marine (registration); WDPA marine areas (non-commercial terms) | fisheries, climate | 2 |
| `land-soils-geology` | Earth and environment | `environment.land` | CORINE Land Cover; FAO Forest Resources Assessment; ESDAC soil data; national geological surveys | agrifood, hazards, biodiversity | 2 |
| `waste-circular-economy` | Earth and environment | `environment.waste` | Eurostat waste statistics; EEA Industrial Reporting (E-PRTR successor); OECD waste statistics | chemicals, products | 2 |
| `physical-sciences-reference` | Science and knowledge | `science.physical-reference` | NIST CODATA constants; NIST Atomic Spectra Database; IAEA nuclear data; Particle Data Group | materials, chemicals | 3 |
| `mortality-health-outcomes` | Health | `clinical.mortality` | WHO Mortality Database; Eurostat causes of death; UN World Population Prospects; IHME GBD (non-commercial terms) | surveillance, demographics | 2 |
| `animal-health` | Health | `clinical.animal-health` | WOAH WAHIS; EFSA data; FAO EMPRES-i | surveillance, agrifood | 2 |
| `internet-infrastructure` | Technology | `technology.internet-infrastructure` | RIPEstat; PeeringDB; RDAP; certificate transparency logs; CAIDA datasets (acceptable-use policy) | OSINT, vulnerabilities | 2 |
| `telecommunications-spectrum` | Technology | `technology.telecommunications` | FCC ULS and broadband data; Bundesnetzagentur data; ITU DataHub (terms) | infrastructure, competition | 3 |
| `ai-models-datasets` | Technology | `technology.ai-models` | Hugging Face Hub metadata; OpenML; Epoch AI datasets | OSS ecosystems, literature | 2 |
| `cyber-incidents` | Technology | `technology.cyber-incidents` | SEC 8-K Item 1.05 disclosures; HHS OCR breach portal; state breach-notification registers | vulnerabilities, market filings | 2 |
| `film-broadcast` | Culture and leisure | `culture.film-broadcast` (new bundle) | Wikidata; European Audiovisual Observatory LUMIERE and MAVISE; TMDB (API terms) | media metadata | 3 |
| `performing-arts-events` | Culture and leisure | `culture.performing-arts` (new bundle) | Wikidata; Eurostat cultural participation statistics | cultural heritage | 3 |
| `games` | Culture and leisure | `culture.games` (new bundle) | Wikidata; MobyGames API; IGDB (platform terms) | media metadata | 3 |
| `archives-genealogy` | Culture and leisure | `culture.archives` (new bundle) | Archives Portal Europe; Archivportal-D; US National Archives catalog API | cultural heritage, legal | 3 |
| `religion-belief` | Culture and leisure | `culture.religion` (new bundle) | Census religion tables; Pew Research religious composition data; ARDA | demographics | 3 |
| `media-outlets-ownership` | Information and investigation | `news.outlets` | Media Ownership Monitor; MAVISE; KEK media database | news, ownership | 2 |

Wave 1 had 10 tracks; all ten now have fixture-tested providers, so their rows
are gone. Their live coverage is still open, in each track's "Validate live
coverage" issue. Wave 2 has 14 tracks; the industry-business and tourism
tracks are covered offline (not live), so their rows are gone and 12 wave 2
rows remain. Wave 3 has 9.
