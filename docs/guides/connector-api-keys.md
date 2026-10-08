# Connector API keys

Every Noesis connector that reads an API key, token or credential, whether it
is free to obtain, and a free alternative where it is not. Source-pack sources
declare their secret as `auth.secret_ref` in `config/source_packs/*.json`
(`required-secret` = the source is skipped without it, `optional-secret` =
higher rate limits or extra data); code-level connectors read the variable
named in the second column directly.

**Needs API key**: **Required** = the connector does nothing without it;
**Optional** = works without it, with lower rate limits or less data.
**Free / not free** describes access for a non-commercial user as of
October 2026; check the provider's terms before relying on it.

> A connector whose required key is missing is skipped with
> `PermanentFetchError(... needs <VAR>; skipping ...)` and its harvest yields
> zero records. Record such a source as *not searched* in any search receipt,
> never as *zero hits*.

## Scholarly and research

| Connector | Needs API key | Free / not free | Suggested alternative if not free |
|---|---|---|---|
| `scopus` (PR #2834) | Required: `ELSEVIER_API_KEY` | Free key for non-commercial use; without an institutional entitlement only the STANDARD view (no abstracts, first author only, 25 per request). Full access needs a Scopus subscription. | Abstracts by DOI from `openalex` / `crossref` / `semantic_scholar`; broad coverage from `openalex` |
| `core` | Required: `CORE_API_KEY` | Free (registration) | — |
| `semantic_scholar` | Optional: `SEMANTIC_SCHOLAR_API_KEY` | Free (application); keyless works at a low shared rate limit | — |
| `pubmed`, clinical providers, `ncbi-gene-lifesci`, `ncbi-taxonomy-lifesci` | Optional: `NCBI_API_KEY` / `NOESIS_NCBI_API_KEY` | Free | — |
| `openalex-works` | Optional: `NOESIS_OPENALEX_API_KEY` | Free tier; paid premium for high volume | Keyless free tier; `crossref` |
| OpenCitations (knowledge engine) | Optional: `NOESIS_OPENCITATIONS_TOKEN` | Free | — |
| `research-entities-orcid` | Required: `NOESIS_ORCID_PUBLIC_TOKEN` | Free (public API client) | — |
| `epo-ops-patents` | Required: `NOESIS_EPO_OPS_CREDENTIALS` | Free up to the fair-use weekly quota | — |
| `huggingface-hub`, media diarizer | Optional: `NOESIS_HF_TOKEN` / `HUGGINGFACE_HUB_TOKEN` | Free | — |
| Zotero sync | Required: `NOESIS_ZOTERO_<NAME>` (e.g. `NOESIS_ZOTERO_MAIN`) | Free | — |

## Economics, statistics, energy, agriculture

| Connector | Needs API key | Free / not free | Suggested alternative if not free |
|---|---|---|---|
| `bls-public-data-api` | Required: `NOESIS_BLS_KEY` | Free | — |
| `un-comtrade-trade-flows` | Required: `NOESIS_COMTRADE_KEY` | Free tier with limits; paid premium for bulk | Free tier; WTO / Eurostat Comext for EU trade |
| `fred-series`, dataset connector `fred` | Required: `NOESIS_FRED_API_KEY` / `FRED_API_KEY` | Free | — |
| `destatis-genesis-*` | Required: `NOESIS_DESTATIS_GENESIS_TOKEN` | Free (registration) | — |
| `iom-dtm-idps` | Required: `NOESIS_IOM_DTM_KEY` | Free (registration) | — |
| `energy-eia-*` (4 sources) | Required: `NOESIS_EIA_API_KEY` | Free | — |
| `energy-ember-*` (3 sources) | Required: `NOESIS_EMBER_API_KEY` | Free | — |
| `energy-entsoe-de-lu` | Required: `NOESIS_ENTSOE_SECURITY_TOKEN` | Free (token on request) | — |
| `fas-psd-balances` | Required: `NOESIS_FAS_API_KEY` | Free | — |
| `nass-quickstats-crops` | Required: `NOESIS_NASS_API_KEY` | Free | — |
| `us-census-cbp` | Optional: `NOESIS_CENSUS_API_KEY` | Free | — |
| market connector `fmp` | Required: `FMP_API_KEY` | Freemium: small free daily quota, paid plans | SEC EDGAR XBRL company facts (`sec-edgar`, free) for fundamentals |

## Politics, law, government, companies

| Connector | Needs API key | Free / not free | Suggested alternative if not free |
|---|---|---|---|
| `de-bundestag-dip` | Required: `NOESIS_BUNDESTAG_API_KEY` | Free (public key) | — |
| `eu-eurlex-regulatory` | Required: `NOESIS_EURLEX_API_KEY` | Free (web-service registration) | — |
| `us-congress-gov-bills`, `us-congress-gov-house-votes` | Required: `NOESIS_CONGRESS_GOV_API_KEY` | Free (api.data.gov key) | — |
| `us-govinfo-bills` | Required: `NOESIS_GOVINFO_API_KEY` | Free (api.data.gov key) | — |
| `us-fec-*` (6 sources) | Required: `NOESIS_OPENFEC_API_KEY` | Free (api.data.gov key) | — |
| `courtlistener-dockets`, `courtlistener-opinions` | Required: `NOESIS_COURTLISTENER_API_TOKEN` | Free | — |
| `fbi-cde-summarized` | Required: `NOESIS_FBI_CDE_API_KEY` | Free (api.data.gov key) | — |
| `sam-opportunities` | Required: `NOESIS_SAM_API_KEY` | Free | — |
| `companies-house` | Required: `NOESIS_COMPANIES_HOUSE_API_KEY` | Free | — |
| `sec-edgar-ownership` | Required: `NOESIS_SEC_CONTACT` (contact string, not a key) | Free | — |
| `sec-edgar` | Optional: `NOESIS_SEC_USER_AGENT` (contact string) | Free | — |

## OSINT, news, humanitarian, platforms

| Connector | Needs API key | Free / not free | Suggested alternative if not free |
|---|---|---|---|
| `acled-events` | Required: `NOESIS_ACLED_ACCESS_TOKEN` | Free for registered non-commercial users, tiered access; commercial use licensed | `ucdp-ged-sdn` / `ucdp-candidate-sdn` (free) |
| `ucdp-candidate-sdn`, `ucdp-ged-sdn` | Optional: `NOESIS_UCDP_ACCESS_TOKEN` | Free | — |
| `gfw-vessels-effort`, `movements-gfw-port-visits` | Required: `NOESIS_GFW_API_TOKEN` | Free for non-commercial use | — |
| `movements-kystdatahuset-ais` | Required: `NOESIS_KYSTDATAHUSET_TOKEN` | Free (registration) | — |
| `movements-opensky` | Optional: `NOESIS_OPENSKY_CREDENTIAL` | Free for research / non-commercial use | — |
| `news-fact-checks-google`, argument-mining fact check | Required: `NOESIS_GOOGLE_FACTCHECK_API_KEY` / `GOOGLE_FACTCHECK_API_KEY` | Free | — |
| Guardian news pack | Required: `NOESIS_GUARDIAN_API_KEY` | Free developer key (non-commercial) | — |
| `lumen-notices` | Required: `NOESIS_LUMEN_API_TOKEN` | Free researcher access on application | — |
| `meta-ad-library-political` | Required: `NOESIS_META_AD_LIBRARY_TOKEN` | Free (identity verification required) | — |
| `glofas-notifications` | Required: `NOESIS_GLOFAS_TOKEN` | Free (registration) | — |

## Products, chemicals, materials, space, sport, culture, software

| Connector | Needs API key | Free / not free | Suggested alternative if not free |
|---|---|---|---|
| `eprel-*` (3 sources) | Required: `NOESIS_EPREL_API_KEY` | Free (key on request) | — |
| `icecat-*` (3 sources) | Optional: `NOESIS_ICECAT_API_TOKEN` | Open Icecat free; Full Icecat paid | Open Icecat; `eprel-*` for energy-label data |
| `fdc-foods` | Required: `NOESIS_FDC_API_KEY` | Free (api.data.gov key) | — |
| `openfda-products`, `medicines-drugsfda-submissions` | Optional: `NOESIS_OPENFDA_API_KEY` | Free | — |
| `comptox-toxval` | Required: `NOESIS_COMPTOX_API_KEY` | Free (key on request) | — |
| `materials-project-ti-al-oxides` | Required: `NOESIS_MATERIALS_PROJECT_API_KEY` | Free | — |
| `esa-discos-objects`, `esa-discos-reentries` | Required: `NOESIS_ESA_DISCOS_TOKEN` | Free (registration, approval) | — |
| `etherscan-v2-mainnet` | Required: `NOESIS_ETHERSCAN_API_KEY` | Free tier with limits; paid plans | Free tier; Blockscout API or a public Ethereum RPC endpoint |
| `football-data-pl-*` (3 sources) | Required: `NOESIS_FOOTBALL_DATA_API_KEY` | Free tier (limited competitions); paid plans | Free tier covers the Premier League; openfootball open datasets |
| `ddb-berlin-photographs` | Required: `NOESIS_DDB_API_KEY` | Free | — |
| `europeana-berlin-images` | Required: `NOESIS_EUROPEANA_API_KEY` | Free | — |
| `nvd-cve-api`, `nvd-cve-history`, `nvd-cpe-dictionary` | Optional: `NOESIS_NVD_API_KEY` | Free | — |
| `peeringdb-network` | Optional: `NOESIS_PEERINGDB_API_KEY` | Free | — |
| `software-heritage` | Optional: `NOESIS_SWH_TOKEN` | Free | — |
| `uniprot-lifesci-proteins`, `rcsb-pdb-lifesci-structures`, `chembl-lifesci-bioactivity` | Optional: `NOESIS_SCIENCE_CONTACT` (contact string) | Free | — |
| `dnb-*`, `loc-*`, `musicbrainz-media`, `openlibrary-media`, `wikidata-media` | Optional: `NOESIS_MEDIA_METADATA_CONTACT` (contact string) | Free | — |

## Integrations and AI services (not data sources)

| Connector | Needs API key | Free / not free | Suggested alternative if not free |
|---|---|---|---|
| Internet Archive Save Page Now (`wayback`, `memento`) | Required for captures: `NOESIS_IA_S3_ACCESS_KEY` + `NOESIS_IA_S3_SECRET_KEY` | Free (archive.org account) | — |
| TypeSafe / JEV suggestions (`typesafe_jev`) | Required: `TYPESAFE_API_KEY` (+ `NOESIS_JEV_API_KEY`) | Not verified | Human screening without suggestions (the review workflow does not depend on them) |
| Media transcription | Optional: `OPENAI_API_KEY` | Not free (paid API) | Local `faster-whisper` or `openai-whisper` backend (built in, no key) |
| Embeddings backend `openai` | Required for that backend: `OPENAI_API_KEY` | Not free (paid API) | `local_sentence_transformers` backend (built in, no key) |
| Vision describer | Required: `ANTHROPIC_API_KEY` (`NOESIS_VISION_PROVIDER=anthropic`) | Not free (paid API) | A local open-weight vision model; no built-in local provider yet |
| Telegram alert channel | Required for that channel: `TELEGRAM_BOT_TOKEN` | Free | — |

## Where keys live

Keys are environment variables of the process that runs the connector. Do not
commit them, paste them into tickets or chat, or store them in `.env` files
that are backed up unencrypted; inject them at start from a secret manager
(e.g. OpenBao via a small wrapper) instead.
