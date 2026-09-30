# Domain coverage program

Status: planned, 2026-09-30. Decision:
[ADR-005](../architecture/decisions/ADR-005-domain-coverage-program.md).
Taxonomy: [`packs/taxonomy.json`](../../packs/taxonomy.json).

The nine domains of [ADR-004](../architecture/decisions/ADR-004-pack-taxonomy.md)
are divided into 89 subdomains. A subdomain is **covered** when at least one
classified provider names it. Today 56 are covered and 33 are gaps. This
program fills every gap, one track per subdomain.

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

Two domains have no bundle suited to their gaps, so two new bundles are
proposed: `society` (Society and population) and `culture` (Culture and
leisure). Every other gap becomes a provider in an existing bundle. Existing
providers stay where they are; their ids are preserved.

## Waves

- **Wave 1:** strong links to existing packs, and open sources with known
  machine access.
- **Wave 2:** useful, with some access or modelling work.
- **Wave 3:** restrictive terms, registration-gated data or narrow demand.

## Gap table

| Subdomain | Domain | Proposed home | Candidate sources (unaudited) | Links to | Wave |
| --- | --- | --- | --- | --- | --- |
| `government-transparency` | Governance and law | `political.transparency` | Bundestag DIP (questions, printed papers); UK Parliament written questions API; FragDenStaat | legislation, public finance | 2 |
| `defence-security` | Governance and law | `political.defence` | SIPRI military expenditure and arms transfers (terms restrict redistribution); UN Register of Conventional Arms; NATO defence expenditure reports | sanctions, humanitarian | 3 |
| `income-poverty-inequality` | Society and population | `society.income` (new bundle) | World Bank Poverty and Inequality Platform; Eurostat EU-SILC; OECD Income Distribution Database | demographics, labour | 1 |
| `social-protection` | Society and population | `society.social-protection` (new bundle) | Eurostat ESSPROS; OECD SOCX; ILO social protection data | public finance, demographics | 2 |
| `public-opinion-wellbeing` | Society and population | `society.public-opinion` (new bundle) | Eurobarometer via GESIS; European Social Survey (registration); OECD How's Life | elections polls | 3 |
| `civil-society` | Society and population | `society.civil-society` (new bundle) | IRS exempt-organisation data and Form 990 filings; Charity Commission for England and Wales register; 360Giving | funding, lobbying, ownership | 2 |
| `extractives-natural-resources` | Economy and markets | `economics.extractives` | EITI summary data; USGS Mineral Commodity Summaries; BGS World Mineral Statistics | ownership, energy, public finance | 1 |
| `tourism-hospitality` | Economy and markets | `economics.tourism` | Eurostat tourism statistics; UN Tourism statistics | geospatial, labour | 2 |
| `industry-business` | Economy and markets | `economics.business` | Eurostat short-term business statistics and business demography; US Census County Business Patterns | trade, labour | 2 |
| `oceans-marine` | Earth and environment | `environment.marine` | NOAA ERDDAP; Argo; Copernicus Marine (registration); WDPA marine areas (non-commercial terms) | fisheries, climate | 2 |
| `land-soils-geology` | Earth and environment | `environment.land` | CORINE Land Cover; FAO Forest Resources Assessment; ESDAC soil data; national geological surveys | agrifood, hazards, biodiversity | 2 |
| `waste-circular-economy` | Earth and environment | `environment.waste` | Eurostat waste statistics; EEA Industrial Reporting (E-PRTR successor); OECD waste statistics | chemicals, products | 2 |
| `life-sciences-reference` | Science and knowledge | `science.life-sciences` | UniProt; NCBI Gene and Taxonomy; RCSB PDB; ChEMBL | chemicals, clinical, biodiversity | 1 |
| `research-entities-data` | Science and knowledge | `science.research-entities` | ORCID public API; ROR; DataCite; OpenAIRE Graph; CORDIS | literature, funding | 1 |
| `physical-sciences-reference` | Science and knowledge | `science.physical-reference` | NIST CODATA constants; NIST Atomic Spectra Database; IAEA nuclear data; Particle Data Group | materials, chemicals | 3 |
| `medical-devices` | Health | `clinical.devices` | openFDA device endpoints (510(k), PMA, MAUDE, recalls); GUDID; EUDAMED public modules | products safety, engineering safety | 1 |
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
| `social-platforms` | Information and investigation | `osint.platform-transparency` | EU DSA Transparency Database; Meta Ad Library API; Google Ads Transparency Center; Lumen (research access) | OSINT, news, elections | 1 |
| `media-outlets-ownership` | Information and investigation | `news.outlets` | Media Ownership Monitor; MAVISE; KEK media database | news, ownership | 2 |

Wave 1 has 10 tracks, wave 2 has 14 and wave 3 has 9.
