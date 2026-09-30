# Chemicals and Substances: source-contract audit and bounded provider coverage (CH01)

Tracking: #2212 · delivery issue #2281 · recorded 2026-09-29.

This audit sets out, per source, what the Chemicals and Substances pack may
acquire, how, and on what terms. It was written without network access.
Endpoints, field names, identifiers and terms come from the providers' public
documentation as the author knows it, or (ECHA CHEM) from the shape of the
portal's own JSON. **Every item marked _verify_ must be checked against the
live page and a published response before the first dated live run (CH13,
#2317). No provider is `live` until that run exists.**

The machine-readable copy of these decisions is `PROVIDER_CONTRACTS`,
`LIVE_VERIFICATION` and `EXCLUDED_FIELDS` in
`src/ingestion/substance_sources.py`; the source-pack declaration is
`config/source_packs/chemicals.json` (`chemicals-substances` 1.0.0). The MCP
tool `substance_source_contracts` returns them and `chemicals_bundle_status`
reports readiness per provider.

These non-goals apply to every source:

- No hazard or exposure assessment, risk characterisation or safety advice.
  Classifications, list entries and data points are quoted as published with a
  locator into the provider payload; nothing is scored, ranked, combined or
  turned into a conclusion.
- No synthesis, preparation, reaction or manufacturing information is acquired
  (see *Excluded fields*).
- A substance with no entries on record is reported as having *none on
  record*, never as safe or unregulated.
- Absence from a later response never removes an entry; removals are dated
  events the source itself publishes.
- Identity is never merged automatically: identifiers are matched through
  reviewable candidates (CH07), and group entries, mixtures, salts and isomers
  are never grouped with a parent substance without an explicit reviewed match.

## Access decisions

| Source | Delivers | Decision | Reason |
| --- | --- | --- | --- |
| PubChem PUG-REST | compound identity: CID, InChI/InChIKey, names, depositor synonyms (CAS, EC, DTXSID among them), modification date | `unverified-live` | Documented anonymous REST service (`https://pubchem.ncbi.nlm.nih.gov/rest/pug`). The adapter calls `compound/cid/{cid}/property/Title,IUPACName,MolecularFormula,InChI,InChIKey/JSON`, `compound/cid/{cid}/synonyms/JSON` and `compound/cid/{cid}/dates/JSON?dates_type=modification` (the `dates` operation and its field names are _verify_). No key |
| ECHA CHEM - CLP | harmonised Annex VI classifications (per ATP) and notified C&L inventory aggregates | `unverified-live` | ECHA CHEM (`https://chem.echa.europa.eu`) publishes substance, Annex VI and C&L inventory views; its portal JSON is **not a documented API**. The adapter reads one substance at a time from `/api-substance/v1/substance/{echa_id}`, `/api-cnl-inventory/v1/harmonised/{echa_id}` (or `/harmonised/by-index/{index}` for a group entry) and `/api-cnl-inventory/v1/notified/{echa_id}`; every path and field name is _verify_. If ECHA offers no supported machine interface for these views, the source moves to the documented downloads (the Annex VI table published with each ATP and the C&L inventory export) or to `not-implemented`; the portal is never crawled |
| ECHA CHEM - REACH lists | registration status, SVHC Candidate List events, Annex XIV and Annex XVII entries | `unverified-live` | Same portal and caveat: `/api-dossier-list/v1/registrations/{echa_id}`, `/api-lists/v1/candidate-list/{echa_id}`, `/api-lists/v1/authorisation-list/{echa_id}`, `/api-lists/v1/restriction-list/{echa_id}` are _verify_. The Candidate List, Authorisation List and Restriction List are also published as downloadable tables, the documented fallback. Registration dossier contents are never mirrored; only the published status, type, tonnage band and last-updated date |
| US EPA CompTox (CTX APIs) | DTXSID identity and ToxValDB data points | `unverified-live` | Documented CTX Chemical and Hazard APIs (`https://api-ccte.epa.gov`): `/chemical/detail/search/by-dtxsid/{dtxsid}` and `/hazard/toxval/search/by-dtxsid/{dtxsid}` (path and field names _verify_). Requires an API key issued by EPA, sent as the `x-api-key` header and held only as the secret reference `NOESIS_COMPTOX_API_KEY` (auth `required-secret`); the key never appears in manifests, receipts, records or fixtures. Without the secret the source preflight reports it unavailable |

**Unavailable-access fallback.** A payload is not stored when any of these
happens: an HTTP error other than 404, a redirect to another host, schema
drift, a response over the byte ceiling or a selection with more statements
than the run's result budget (refused, never truncated). The source run is
recorded as failed and monitors do not evaluate it. An explicitly selected
substance the provider does not know (HTTP 404 on its primary request) is a
`not_found` selection outcome in the page receipt, not a failure and not a
removal.

## Per-source contract

| Source | Identifiers | Revision behaviour | Cadence | Paging and limits | Licence, legal notice and attribution |
| --- | --- | --- | --- | --- | --- |
| PubChem | CID (primary), InChIKey, InChI; CAS and EC only as depositor synonyms | A compound record changes in place; its modification date is the record version (`source.record_version`). Each distinct payload is a new stored revision; depositor synonyms that disagree (two valid CAS numbers) are all kept with `conflict: true`, none is chosen | continuous | one CID per page, three requests; at most 5 requests/s and 400/min, dynamic throttling via `X-Throttling-Control` (_verify_); 429 is `rate_limited` with Retry-After | NCBI/NLM policies: PubChem data are generally public; depositor records may carry their own terms (_verify_ per depositor). Attribution: "Source: PubChem, National Library of Medicine" |
| ECHA CLP | EC number, CAS number, Annex VI index number, ECHA substance id (`100.xxx.xxx`) | Annex VI changes only by an **adaptation to technical progress (ATP)**, a Commission (Delegated) Regulation with a publication date and a later date of application. Each ATP that introduced, amended or deleted an entry is a dated classification revision (`effective.from` = date of application) with the act's CELEX; the prior classification stays. Notified aggregates change continuously and are dated by the inventory's as-of date | per ATP (roughly yearly) | one substance per page, three requests; no published rate limit, kept within the source budget | ECHA legal notice: reproduction authorised provided the source is acknowledged; no endorsement implied; _verify_ conditions for bulk or commercial reuse. Attribution: "Source: European Chemicals Agency, https://echa.europa.eu/" |
| ECHA REACH | EC, CAS, ECHA substance id; list entry numbers | The **Candidate List** is updated about twice a year: each entry has an inclusion date and reason (Article 57 property) and later amendments or removals are dated events. **Annex XIV** and **Annex XVII** entries change by amending regulation; each inclusion or amendment is a dated event with its act (CELEX), sunset and latest application dates (XIV) and the conditions text verbatim (XVII). Registration status carries its last-updated date | Candidate List twice yearly; annexes per amending act | one substance per page, five requests | as ECHA CLP |
| CompTox | DTXSID (primary), DTXCID, CAS, InChIKey; data points by `toxvalId` | Data points belong to a ToxValDB release; the release label is recorded as `source.data_version` with every data point, and a changed value is a new revision. Values are quoted as published (value, qualifier, unit), never converted, combined or ranked | per ToxValDB release | one DTXSID per page, two requests; per-key limits set by EPA (_verify_) | U.S. Government data, generally public domain; ToxValDB rows cite third-party sources whose own citation requirements are kept per data point (_verify_). Attribution: "Source: U.S. EPA CompTox Chemicals Dashboard / CTX APIs" |

## Bounded substance set

Each source acquires only the explicit selection in
`config/source_packs/chemicals.json`; nothing is enumerated.

| Substance | CAS | EC | InChIKey | PubChem CID | ECHA id / index | DTXSID | Why selected |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Bisphenol A | 80-05-7 | 201-245-8 | IISBACLAFKSPIT-UHFFFAOYSA-N | 6623 | 100.001.133 / 604-030-00-0 | DTXSID7020182 | **classified with an ATP revision** (Repr. 2 to Repr. 1B by the 9th ATP, Regulation (EU) 2016/1179), **SVHC** (inclusion and amendment), **restricted** (Annex XVII entry 66, thermal paper) |
| Bis(2-ethylhexyl) phthalate (DEHP) | 117-81-7 | 204-211-0 | BJQHLKABXJIVAM-UHFFFAOYSA-N | 8343 | 100.003.829 / 607-317-00-9 | DTXSID5020607 | classified, SVHC, **authorisation** (Annex XIV entry 4: latest application and sunset dates) and restricted (Annex XVII entry 51) |
| Ethanol | 64-17-5 | 200-578-6 | LFQSCWFLJHTTHZ-UHFFFAOYSA-N | 702 | 100.000.526 / 603-002-00-5 | DTXSID9020584 | classified (Flam. Liq. 2) but on **no REACH list** |
| Sucrose | 57-50-1 | 200-334-9 | CZMRCDWAGMRECN-UGDNZRGBSA-N | 5988 | 100.000.304 | (not selected) | **unregulated example**: registered, no harmonised classification, on no list; reported as *none on record*, never as safe |
| Lead compounds (group entry) | none | none | none | none | index 082-001-00-6 | none | **CLP group entry** with no EC or CAS: never matched to lead or any lead compound without an explicit reviewed match |
| (unknown CID 999999999) | - | - | - | 999999999 | - | - | exercises the `not_found` selection outcome |

The identifiers above are the substances' public identifiers. The offline
fixtures (`tests/fixtures/source_packs/chemicals-*.json`) are authored in each
provider's response shape; their classification, list and data-point values
are transcribed or authored for illustration (data-point values and study
references are explicitly fictional) and are not live evidence.

## Excluded fields

Parsers read an explicit allow-list of identity and regulatory-status fields.
The following are never acquired; when a response carries any of them, the
key is dropped unparsed and named in the page receipt
(`excluded_fields_dropped`), and statements carrying such keys are rejected by
the record contract:

- synthesis routes, synthesis references, preparation or manufacturing
  methods, reactions and precursors (PubChem PUG-View sections such as
  *Methods of Manufacturing* or *Synthesis References* are never requested);
- bioassay and bioactivity results;
- registration dossier contents (studies, uses, exposure scenarios) beyond the
  published registration status;
- any hazard score, risk or exposure conclusion, or safety advice.

## Revision behaviour summary

| Change | How it arrives | How it is stored |
| --- | --- | --- |
| CLP ATP amendment | a new history item in the Annex VI entry with the ATP act and date of application | a new classification revision; the prior one stays and remains the answer for dates before the application date |
| SVHC inclusion, amendment, removal | a dated Candidate List event with reason and decision | one revision per event; a removal is a revision, never a deletion |
| Annex XIV / XVII amendment | a dated event with the amending act, dates and conditions | one revision per event, conditions verbatim |
| PubChem record update | a changed payload with a new modification date | a new revision of each changed statement |
| ToxValDB release | changed values under a new release label | a new data-point revision citing the release |
