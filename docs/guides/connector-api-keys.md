# Connector API keys

Every Noesis connector that reads an API key, token, account or contact
string, whether it is free to obtain, and a free alternative where it is not.

**Scope.** Noesis has far more connectors than this page lists: 243 source
entries in 38 source packs (`config/source_packs/*.json`), of which 155 need
no credentials, plus about 60 domain provider modules
(`src/ingestion/*_sources.py`, `*_providers.py`) and the code-level connectors
under `src/ingestion/connectors/`. Only the ones that need a credential appear
here; everything else works without a key. Source-pack sources
declare their secret as `auth.secret_ref` in `config/source_packs/*.json`
(`required-secret` = the source is skipped without it, `optional-secret` =
higher rate limits or extra data); code-level connectors read the variable
named in the second column directly.

**Needs API key**: **Required** = the connector does nothing without it;
**Optional** = works without it, with lower rate limits or less data.

**Free / not free** was checked against provider documentation and, where no
official page was found, secondary sources, in October 2026, for a
non-commercial user. Entries starting with **Unconfirmed** could not be
verified; limits and prices change, so check the provider before relying on a
figure. Sources are listed at the end.

> A connector whose required key is missing is skipped with
> `PermanentFetchError(... needs <VAR>; skipping ...)` and its harvest yields
> zero records. Record such a source as *not searched* in any search receipt,
> never as *zero hits*.

## Scholarly and research

| Connector | Needs API key | Free / not free | Suggested alternative if not free |
|---|---|---|---|
| `scopus` (PR #2834) | Required: `ELSEVIER_API_KEY` | Free key for non-commercial use, but without an institutional entitlement only the STANDARD view (no abstracts, first author only, 25 records per request; 20,000 searches per week). Full access needs a Scopus subscription. | Abstracts by DOI from `openalex` / `crossref` / `semantic_scholar` |
| `core` | Required: `CORE_API_KEY` | Free key (registration), for non-commercial academic use per secondary sources; official limits not published | — |
| `semantic_scholar` | Optional: `SEMANTIC_SCHOLAR_API_KEY` | Free key on request (introductory 1 request/s dedicated); keyless requests share one throttled pool | — |
| `pubmed`, clinical providers, `ncbi-gene-lifesci`, `ncbi-taxonomy-lifesci` | Optional: `NCBI_API_KEY` / `NOESIS_NCBI_API_KEY` | Free; 10 requests/s with a key, 3 without | — |
| `openalex-works` | Optional: `NOESIS_OPENALEX_API_KEY` | Freemium: a free key gives $1 of usage per day (≈ 1,000 searches or 10,000 list calls; single lookups free); keyless $0.10/day; above that prepaid pay-as-you-go or annual plans (from $5,000/year) | Stay within the free daily budget; spread load to `crossref` (free) |
| OpenCitations (knowledge engine) | Optional: `NOESIS_OPENCITATIONS_TOKEN` | Free token (email only); recommended, not required | — |
| `research-entities-orcid` | Required: `NOESIS_ORCID_PUBLIC_TOKEN` | Free Public API credentials (read-public token, client-credentials flow), for non-commercial use | — |
| `epo-ops-patents` | Required: `NOESIS_EPO_OPS_CREDENTIALS` | Free up to 4 GB per week (registration); above that a yearly subscription (2025: €2,800) | Stay under the weekly cap; EPO raw-data products for bulk |
| `huggingface-hub`, media diarizer | Optional: `NOESIS_HF_TOKEN` / `HUGGINGFACE_HUB_TOKEN` | Free account token; limits per 5-minute window, higher on paid tiers | — |
| Zotero sync | Required: `NOESIS_ZOTERO_<NAME>` (e.g. `NOESIS_ZOTERO_MAIN`) | Unconfirmed (not re-checked): free key from a Zotero account | — |

## Economics, statistics, energy, agriculture

| Connector | Needs API key | Free / not free | Suggested alternative if not free |
|---|---|---|---|
| `bls-public-data-api` | Required: `NOESIS_BLS_KEY` | Free (registration); v2: 500 queries/day, 50 series per query | — |
| `un-comtrade-trade-flows` | Required: `NOESIS_COMTRADE_KEY` | Free registered key (≈ 500 calls/day, up to 100,000 records per call per rOpenSci docs); premium from $2,000/year | Free key within its limits; Eurostat Comext for EU trade |
| `fred-series`, dataset connector `fred` | Required: `NOESIS_FRED_API_KEY` / `FRED_API_KEY` | Free (account); about 120 requests/min | — |
| `destatis-genesis-*` | Required: `NOESIS_DESTATIS_GENESIS_TOKEN` | Free; API needs a registered account (token from the account page) | — |
| `iom-dtm-idps` | Required: `NOESIS_IOM_DTM_KEY` | Unconfirmed: subscription key from the IOM DTM API portal; no fee found | — |
| `energy-eia-*` (4 sources) | Required: `NOESIS_EIA_API_KEY` | Free (registration) | — |
| `energy-ember-*` (3 sources) | Required: `NOESIS_EMBER_API_KEY` | Free key per secondary source; data CC BY 4.0 | Ember CSV downloads (free, no key) |
| `energy-entsoe-de-lu` | Required: `NOESIS_ENTSOE_SECURITY_TOKEN` | Unconfirmed: register, then request API access by email; no fee found | — |
| `fas-psd-balances` | Required: `NOESIS_FAS_API_KEY` | Unconfirmed: PSD data are public and free; key sign-up not confirmed | PSD Online downloads (free) |
| `nass-quickstats-crops` | Required: `NOESIS_NASS_API_KEY` | Unconfirmed: key from Quick Stats; no fee found | — |
| `us-census-cbp` | Optional: `NOESIS_CENSUS_API_KEY` | Free; 500 calls/IP/day without a key | — |
| `development_finance` (IATI Datastore) | Required: `NOESIS_IATI_DATASTORE_KEY` | Free subscription key: Exploratory tier 5 calls/min and 100 calls/week without approval; Full Access tier after IATI Secretariat approval | IATI Registry bulk XML (free) |
| `environment_providers` OpenAQ | Required: `NOESIS_OPENAQ_API_KEY` | Free key; full rate limit with a key (about 60/min, 2,000/hour per secondary sources), much lower without | — |
| `water_sources` USGS Water Data | Optional: `NOESIS_USGS_WATER_API_KEY` | Free api.data.gov key; lower anonymous limit without | — |
| `environment_providers` Copernicus Atmosphere Data Store (not implemented) | Account + personal API key | Free ECMWF account; per-dataset licence acceptance | — |
| market connector `fmp` | Required: `FMP_API_KEY` | Freemium: free plan 250 calls/day, non-commercial; paid plans from about $22/month | SEC EDGAR XBRL company facts (`sec-edgar`, free) for fundamentals |

## Politics, law, government, companies

| Connector | Needs API key | Free / not free | Suggested alternative if not free |
|---|---|---|---|
| `de-bundestag-dip` | Required: `NOESIS_BUNDESTAG_API_KEY` | Free; the currently valid key is published on the DIP help page | — |
| `eu-eurlex-regulatory` | Required: `NOESIS_EURLEX_API_KEY` | Free after registration and approval; daily call cap set at registration | Cellar REST API for documents |
| `us-congress-gov-bills`, `us-congress-gov-house-votes` | Required: `NOESIS_CONGRESS_GOV_API_KEY` | Free api.data.gov key; default 1,000 requests/hour | — |
| `us-govinfo-bills` | Required: `NOESIS_GOVINFO_API_KEY` | Free api.data.gov key; default 1,000 requests/hour | — |
| `us-fec-*` (6 sources) | Required: `NOESIS_OPENFEC_API_KEY` | Free api.data.gov key; default 1,000 requests/hour | — |
| `courtlistener-dockets`, `courtlistener-opinions` | Required: `NOESIS_COURTLISTENER_API_TOKEN` | **Since May 2026** a free token allows only 5 requests/min, 50/hour, 125/day; higher limits come with a membership (free EDU membership for academic researchers and students), commercial use needs an agreement | EDU membership; CourtListener bulk data downloads |
| `fbi-cde-summarized` | Required: `NOESIS_FBI_CDE_API_KEY` | Free api.data.gov key; default 1,000 requests/hour | — |
| `sam-opportunities` | Required: `NOESIS_SAM_API_KEY` | Free key, but non-federal users without a SAM role get about 10 calls/day (1,000/day with a role); keys expire after 90 days (secondary sources) | SAM.gov public Contract Opportunities data extract (download) |
| `companies-house` | Required: `NOESIS_COMPANIES_HOUSE_API_KEY` | Free; 600 requests per 5 minutes per key | — |
| `ownership_providers` OpenCorporates | Required: `NOESIS_OPENCORPORATES_API_KEY` | Free share-alike key only for public-benefit users (journalists, NGOs, academics), with attribution and open contribution back; otherwise paid, API plans from £2,250/year | GLEIF LEI (`lei_sources`, free, no key); national registers such as `companies-house` |
| `ownership_providers` Unternehmensregister (not implemented) | Account for some documents | Register information free without registration since August 2022; filed annual accounts cost €1 plus VAT | Free register extracts |
| optional integration `opensanctions` | Required: `NOESIS_OPENSANCTIONS_API_KEY` | API not free for general users (about €0.10 per call); free keys only via a public-interest grant (journalists, NGOs, academic researchers); business use needs a licence | Bulk data download, free for non-commercial use (CC BY-NC 4.0, no key) |
| `sec-edgar-ownership` | Required: `NOESIS_SEC_CONTACT` (contact string, not a key) | Free | — |
| `sec-edgar` | Optional: `NOESIS_SEC_USER_AGENT` (contact string) | Free | — |

## OSINT, news, humanitarian, platforms

| Connector | Needs API key | Free / not free | Suggested alternative if not free |
|---|---|---|---|
| `acled-events` | Required: `NOESIS_ACLED_ACCESS_TOKEN` | Free with a myACLED account; the access level depends on the user category (civil society/academic broadest, corporate restricted); commercial use needs a licence. Programmatic access moved to OAuth tokens in 2025. | `ucdp-ged-sdn` / `ucdp-candidate-sdn` |
| `ucdp-candidate-sdn`, `ucdp-ged-sdn` | Optional: `NOESIS_UCDP_ACCESS_TOKEN` | Unconfirmed (not re-checked): free | — |
| `gfw-vessels-effort`, `movements-gfw-port-visits` | Required: `NOESIS_GFW_API_TOKEN` | Free for non-commercial use (self-registration; some APIs need approval); no public commercial licence | — |
| `movements-kystdatahuset-ais` | Required: `NOESIS_KYSTDATAHUSET_TOKEN` | Open AIS data are free under NLOD without registration (some small vessels excluded); full AIS data by application; the Kystdatahuset token process is unconfirmed | Open AIS tier |
| `movements-opensky` | Optional: `NOESIS_OPENSKY_CREDENTIAL` | Free for research / non-commercial use; OAuth2 client credentials for accounts since March 2025; credit-based limits | — |
| `news-fact-checks-google`, argument-mining fact check | Required: `NOESIS_GOOGLE_FACTCHECK_API_KEY` / `GOOGLE_FACTCHECK_API_KEY` | Free (Google Cloud API key, standard project quotas) | — |
| Guardian news pack | Required: `NOESIS_GUARDIAN_API_KEY` | Free developer key; about 5,000 requests/day per secondary source | — |
| `lumen-notices` | Required: `NOESIS_LUMEN_API_TOKEN` | Free researcher token on request to the Lumen team | — |
| `meta-ad-library-political` | Required: `NOESIS_META_AD_LIBRARY_TOKEN` | Free; requires personal identity verification (facebook.com/ID) and a developer app; outside the EU/UK only political and social-issue ads are returned | — |
| `glofas-notifications` | Required: `NOESIS_GLOFAS_TOKEN` | Copernicus GloFAS data are free and open with registration; token process for notifications unconfirmed (some EFAS real-time products are partner-only) | — |
| `brave_discovery` (web discovery) | Required: `NOESIS_BRAVE_API_KEY` | **Not free** for new accounts since 2026: about $5 per 1,000 searches with $5 of monthly credit (≈ 1,000 searches) | Common Crawl index (`common_crawl`, free, no key); Tavily free tier |
| optional integration `exa` (search) | Required: `NOESIS_EXA_API_KEY` / `EXA_API_KEY` | Paid with free credits (sign-up and monthly credits; amounts differ between Exa's own pages) | Tavily free tier; Common Crawl |
| optional integration `tavily` (search) | Required: `NOESIS_TAVILY_API_KEY` / `TAVILY_API_KEY` | Free 1,000 credits per month (no card); pay-as-you-go above | — |
| optional integration `jina` (reader) | Required: `NOESIS_JINA_API_KEY` / `JINA_API_KEY` | Free 10 million tokens per new key, non-commercial only; paid top-ups | Keyless Reader at a low rate limit |
| optional integration `firecrawl` (scraping) | Required: `NOESIS_FIRECRAWL_API_KEY` / `FIRECRAWL_API_KEY` | Free 1,000 credits per month; paid plans from $16–19/month | — |
| optional integration `zyte` (scraping) | Required: `NOESIS_ZYTE_API_KEY` / `ZYTE_API_KEY` | **Not free**: $5 trial credit for 30 days, then pay per successful response | Firecrawl free tier; direct HTTP acquisition |

## Products, chemicals, materials, space, sport, culture, software

| Connector | Needs API key | Free / not free | Suggested alternative if not free |
|---|---|---|---|
| `eprel-*` (3 sources) | Required: `NOESIS_EPREL_API_KEY` | No fee found; key request may require an electronic seal or signature (secondary source, unconfirmed) | — |
| `icecat-*` (3 sources) | Optional: `NOESIS_ICECAT_API_TOKEN` | Open Icecat free (registration; attribution required, no AI training); Full Icecat (all brands) paid | Open Icecat; `eprel-*` for energy-label data |
| `fdc-foods` | Required: `NOESIS_FDC_API_KEY` | Free data.gov key; 1,000 requests/hour | — |
| `openfda-products`, `medicines-drugsfda-submissions` | Optional: `NOESIS_OPENFDA_API_KEY` | Free; with key 240/min and 120,000/day, without 1,000/day per IP | — |
| `comptox-toxval` | Required: `NOESIS_COMPTOX_API_KEY` | Free key by email request (ccte_api@epa.gov) | — |
| `materials-project-ti-al-oxides` | Required: `NOESIS_MATERIALS_PROJECT_API_KEY` | Free account | — |
| `esa-discos-objects`, `esa-discos-reentries` | Required: `NOESIS_ESA_DISCOS_TOKEN` | Public DISCOSweb API with an ESA account; no fee found | — |
| `astronomy_registration_sources` Space-Track decay/TIP (not implemented) | Account (email + password) | Free account; user agreement restricts redistribution | ESA DISCOS (`esa-discos-*`) |
| `biodiversity_sources` IUCN Red List | Required: `NOESIS_IUCN_API_TOKEN` | Free token on application, non-commercial use; bulk use needs written permission | — |
| `etherscan-v2-mainnet` | Required: `NOESIS_ETHERSCAN_API_KEY` | Free tier: 3 calls/s, 100,000/day, selected chains, attribution required; paid plans from $49/month | Free tier; Blockscout API or a public Ethereum RPC endpoint |
| `football-data-pl-*` (3 sources) | Required: `NOESIS_FOOTBALL_DATA_API_KEY` | Free tier: 12 competitions incl. the Premier League, 10 calls/min, delayed scores; paid €12–199/month | Free tier covers these sources |
| `ddb-berlin-photographs` | Required: `NOESIS_DDB_API_KEY` | Free (account; metadata CC0) | — |
| `europeana-berlin-images` | Required: `NOESIS_EUROPEANA_API_KEY` | Free (account) | — |
| `nvd-cve-api`, `nvd-cve-history`, `nvd-cpe-dictionary` | Optional: `NOESIS_NVD_API_KEY` | Free; 50 requests per 30 s with a key, 5 without | — |
| `peeringdb-network` | Optional: `NOESIS_PEERINGDB_API_KEY` | Free; anonymous access has lower limits and no contact data | — |
| `software-heritage` | Optional: `NOESIS_SWH_TOKEN` | Free account token; raises rate limits | — |
| `uniprot-lifesci-proteins`, `rcsb-pdb-lifesci-structures`, `chembl-lifesci-bioactivity` | Optional: `NOESIS_SCIENCE_CONTACT` (contact string) | Free | — |
| `dnb-*`, `loc-*`, `musicbrainz-media`, `openlibrary-media`, `wikidata-media` | Optional: `NOESIS_MEDIA_METADATA_CONTACT` (contact string) | Free | — |
| SEC EDGAR filings connector (`edgar`) | Optional: `NOESIS_EDGAR_USER_AGENT` (contact string) | Free | — |
| source-pack runtime | Optional: `NOESIS_RESEARCH_CONTACT` (contact string) | Free | — |
| scholarly connectors (`openalex`, `crossref`, …) | Optional: `NOESIS_SCHOLARLY_CONTACT` (contact e-mail for polite API use) | Free | — |

## Integrations and AI services (not data sources)

| Connector | Needs API key | Free / not free | Suggested alternative if not free |
|---|---|---|---|
| Internet Archive Save Page Now (`wayback`, `memento`) | Required for captures: `NOESIS_IA_S3_ACCESS_KEY` + `NOESIS_IA_S3_SECRET_KEY` | Free (archive.org account; keys grant full account access) | — |
| TypeSafe / JEV suggestions (`typesafe_jev`) | Required: `TYPESAFE_API_KEY` (+ `NOESIS_JEV_API_KEY`) | **Not free**: about $0.042 per million input tokens, output free; direct access via waitlist (secondary sources) | Human screening without suggestions (the review workflow does not depend on them) |
| Media transcription | Optional: `OPENAI_API_KEY` | Not free (paid API) | Local `faster-whisper` or `openai-whisper` backend (built in, no key) |
| Embeddings backend `openai` | Required for that backend: `OPENAI_API_KEY` | Not free (paid API) | `local_sentence_transformers` backend (built in, no key) |
| Vision describer | Required: `ANTHROPIC_API_KEY` (`NOESIS_VISION_PROVIDER=anthropic`) | Not free (paid API) | A local open-weight vision model; no built-in local provider yet |
| Telegram alert channel | Required for that channel: `TELEGRAM_BOT_TOKEN` | Free (about 30 messages/s broadcast limit) | — |
| Email alert / report channel | Required for that channel: `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD` (+ `SMTP_FROM`, `SMTP_PORT`, `SMTP_TLS`) | Depends on the mail provider | — |
| optional integration `github` | Required: `NOESIS_GITHUB_TOKEN` / `GITHUB_PERSONAL_ACCESS_TOKEN` / `GITHUB_TOKEN` | Free (personal access token) | — |
| MCP integration `github` | Required: `NOESIS_GITHUB_MCP_TOKEN` | Free (personal access token) | — |
| MCP integration `context7` | Required: `NOESIS_CONTEXT7_API_KEY` | Free tier (about 1,000 requests/month after a January 2026 cut); paid Pro plan | Self-hosted Context7 (open source) |

## Where keys live

Keys are environment variables of the process that runs the connector. Do not
commit them, paste them into tickets or chat, or store them in `.env` files
that are backed up unencrypted; inject them at start from a secret manager
(e.g. OpenBao via a small wrapper) instead.

## Sources (checked October 2026)

- Scopus: [pybliometrics access](https://pybliometrics.readthedocs.io/en/stable/access.html), [Using the Scopus API](https://www.casrai.org/guides/using-the-scopus-api)
- CORE: [core.ac.uk API](https://core.ac.uk/services/api) (via secondary guides)
- Semantic Scholar: [API overview](https://semanticscholar.org/product/api)
- NCBI: [E-utilities documentation](https://www.ncbi.nlm.nih.gov/books/NBK25497/), [API key announcement](https://ncbiinsights.ncbi.nlm.nih.gov/2017/11/02/new-api-keys-for-the-e-utilities)
- OpenAlex: [pricing overview](https://help.openalex.org/access/pricing/), [authentication](https://developers.openalex.org/api-reference/authentication)
- OpenCitations: [access token](https://opencitations.net/accesstoken/)
- ORCID: [read-public token](https://info.orcid.org/ufaqs/how-do-i-get-read-public-access-token/)
- EPO OPS: [fair use](https://www.epo.org/en/service-support/ordering/fair-use), [OPS](https://www.epo.org/en/searching-for-patents/data/web-services/ops), [paid subscription](https://shop.epo.org/en/Data-and-services/Web-services/Open-Patent-Services-%28OPS%29/p/OPS)
- Hugging Face: [Hub rate limits](https://huggingface.co/docs/hub/en/rate-limits), [tokens](https://huggingface.co/docs/hub/security-tokens)
- BLS: [API FAQ](https://www.bls.gov/developers/api_faqs.htm), [features](https://www.bls.gov/bls/api_features.htm)
- UN Comtrade: [comtradr vignette](https://packages.ropensci.org/comtradr/doc/comtradr.Rmd)
- FRED: [secondary guide](https://freeapihub.com/blog/fred-api-tutorial-python-economic-data)
- Destatis: [API / web services](https://destatis.de/EN/Service/OpenData/api-webservice.html)
- IOM DTM: [dtmapi user guide](https://displacement-tracking-matrix.r-universe.dev/dtmapi/doc/user_guide.Rmd)
- EIA: [developer](https://www.eia.gov/developer)
- Ember: [new API](https://ember-energy.org/latest-insights/levelling-up-a-new-api-for-ember-data)
- ENTSO-E: [entsoe-api-node README](https://github.com/rabinage/entsoe-api-node/blob/main/README.md)
- NASS: [rnassqs](https://docs.ropensci.org/rnassqs/articles/rnassqs.html)
- Census: [API key](https://www.census.gov/data/developers/guidance/api-user-guide.API_Key.html)
- FMP: [free API sign-up](https://site.financialmodelingprep.com/developer/docs/blog/how-to-sign-up-and-use-a-free-stock-market-data-api)
- Bundestag DIP: [Informationsblatt zur DIP-API](https://dip.bundestag.de/documents/informationsblatt_zur_dip_api.pdf)
- EUR-Lex: [web service](https://eur-lex.europa.eu/content/help/data-reuse/webservice.html?locale=en)
- api.data.gov: [developer manual](https://api.data.gov/docs/developer-manual/)
- CourtListener: [API included in memberships](https://free.law/2026/05/07/api-included-in-memberships/), [REST v4 overview](https://wiki.free.law/c/courtlistener/help/api/rest/v4/overview)
- SAM.gov: [rate limits](https://api.sam.gov/docs/rate-limits), [secondary summary](https://www.cleat.ai/developers/sam-gov-api)
- Companies House: [developer guidelines](https://developer.company-information.service.gov.uk/developer-guidelines)
- ACLED: [myACLED FAQs](https://acleddata.com/myacled-faqs)
- Global Fishing Watch: [our APIs](https://globalfishingwatch.org/our-apis), [commercial use FAQ](https://globalfishingwatch.org/faqs/can-i-use-global-fishing-watch-apis-for-commercial-purposes/)
- Kystverket: [access to AIS data](https://kystverket.no/en/navigation-and-monitoring/ais/access-to-ais-data)
- OpenSky: [authentication overview](https://apispine.com/opensky-network/authentication)
- Google Fact Check: [Fact Check Tools API](https://developers.google.com/fact-check/tools/api?hl=he)
- Guardian: [API directory entry](https://dataglobehub.com/de/api-finder/die-guardian-open-platform-api/)
- Lumen: [API documentation](https://github.com/berkmancenter/lumendatabase/wiki/Lumen-API-Documentation)
- Meta Ad Library: [secondary guide](https://metapi.io/compare/facebook-ad-library-api)
- GloFAS: [data and services](https://global-flood.emergency.copernicus.eu/general-information/data-and-services), [GFM data access](https://extwiki.eodc.eu/GFM/PUM/DataAccess/GloFAS)
- EPREL: [secondary source](https://apify.com/promptica/eprel-etichette-energetiche/api)
- Icecat: [import manual](https://iceclog.com/manual-how-to-import-product-content-into-your-webshop-via-icecat/), [Full Icecat](https://iceclog.com/why-choose-to-upgrade-to-full-icecat/)
- FoodData Central: [API guide](https://fdc.nal.usda.gov/api-guide.html)
- openFDA: [authentication](https://open.fda.gov/apis/authentication)
- CompTox: [CTX APIs](https://epa.gov/comptox-tools/computational-toxicology-and-exposure-apis)
- Materials Project: [secondary guide](https://mle4217-5219.matsci.dev/database/materials-project)
- ESA DISCOS: [DISCOSweb API notice](https://discosweb-api.sdo.esoc.esa.int)
- Etherscan: [rate limits](https://docs.etherscan.io/etherscan-v2/rate-limits)
- football-data.org: [free tier limits 2026](https://www.thestatsapi.com/blog/football-data-org-free-tier-limits-2026)
- Europeana: [get API](https://pro.europeana.eu/get-api); DDB: [OpenAPI](https://api.deutsche-digitale-bibliothek.de/OpenAPI)
- NVD: [API key announcement](https://nvd.nist.gov/general/news/API-Key-Announcement)
- PeeringDB: [API keys how-to](https://docs.peeringdb.com/howto/api_keys/)
- Software Heritage: [web client](https://docs.softwareheritage.org/devel/swh-web-client)
- Internet Archive: [S3 credentials](https://doc-tools.readthedocs.io/en/ia-test-gsod/tutorial-get-ia-credentials.html)
- TypeSafe / JEV: [pricing summary](https://www.eesel.ai/blog/typesafe-jev-pricing), [Requesty listing](https://www.requesty.ai/model/typesafe/jev)
- Telegram: [Bot FAQ](https://core.telegram.org/bots/faq)
- IATI: [Datastore API](https://iatistandard.org/en/iati-tools-and-resources/iati-datastore/how-to-use-the-datastore-api/), [API Gateway](https://iatistandard.org/en/iati-tools-and-resources/api-gateway/)
- OpenAQ: [getting started](https://docs.openaq.org/docs/getting-started)
- Copernicus ADS: [account setup](https://providentia.readthedocs.io/en/stable/ADS.html)
- OpenCorporates: [pricing](https://opencorporates.com/pricing/), [terms of use](https://opencorporates.com/terms-of-use-2/)
- Unternehmensregister: [register overview](https://ecovis-kso.com/blog/register-uebersicht-das-unternehmensregister/)
- OpenSanctions: [free and non-commercial use](https://www.opensanctions.org/docs/commercial/exemption/), [API](https://www.opensanctions.org/api/)
- Brave Search API: [pricing 2026](https://costbench.com/software/ai-search-apis/brave-search-api/), [free plan](https://www.costbench.com/software/ai-search-apis/brave-search-api/free-plan/)
- Exa: [pricing](https://exa.ai/docs/reference/pricing), [billing](https://exa.ai/docs/reference/billing)
- Tavily: [API credits](https://docs.tavily.com/guides/api-credits)
- Jina: [pricing](https://jina.ai/api-dashboard/pricing/)
- Firecrawl: [pricing](https://www.firecrawl.dev/pricing)
- Zyte: [API pricing](https://docs.zyte.com/zyte-api/pricing.html)
- Space-Track: [overview](https://newspaceeconomy.ca/2023/06/22/overview-of-space-track-org-a-space-situational-awareness-service-by-the-u-s-department-of-defense/)
- IUCN Red List: [API client notes](https://zitniklab.hms.harvard.edu/ToolUniverse/_modules/tooluniverse/iucn_tool.html), [Jentic FAQ](https://jentic.com/apis/iucnredlist)
- Context7: [free-tier change](https://blog.devgenius.io/context7-quietly-slashed-its-free-tier-by-92-16fa05ddce03), [usage docs](https://context7.com/docs/howto/usage.md)
