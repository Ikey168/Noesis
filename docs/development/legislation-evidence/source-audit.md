# Legislation: source-contract audit and bounded provider coverage (LT01)

Tracking: #2208 · delivery issue #2388 · recorded 2026-09-29.

This audit sets out, per source, what the Political pack's legislation
features (`legislation-us`, `legislation-uk`) may acquire, how, and on what
terms. It was written without network access. Endpoints, fields and terms come
from the providers' published documentation as the author knows it. **Every
item marked _verify_ must be checked against the live documentation, the live
terms and a real response before the first dated live run (LT13, #2458). No
source is `live` until that run exists.** The machine-readable copy of these
decisions is `PROVIDER_CONTRACTS`, `LIVE_VERIFICATION` and `BOUNDED_COVERAGE`
in `src/ingestion/legislation_sources.py`; each source entry in
`config/source_packs/political.json` (`official-political-records` 1.3.0)
states `legislation.live_verification: unverified-live`, and the MCP tool
`legislation_source_contracts` returns the same decisions.

Non-goals for every source: no passage prediction, no member scoring or
ideology rating, and no summary presented as a bill's legal effect. Records are
kept as each provider published them; congress.gov and GovInfo BILLSTATUS
statements about one bill are separate source assertions and are never
reconciled.

## Access decisions

| Source (source-pack id) | Provider | Delivers | Decision | Reason |
| --- | --- | --- | --- | --- |
| `us-congress-gov-bills` | congress.gov API v3 | bill, actions, sponsor, cosponsors, public-law citations, CRS summaries | `unverified-live` | Documented API with an api.data.gov key; fixture-verified parser; field names are _verify_ |
| `us-congress-gov-house-votes` | congress.gov API v3 (`house-vote`, beta) | House roll calls with member positions by bioguide ID | `unverified-live` | The House vote endpoints are documented as beta; `bioguideID`, `voteCast`, `legislationType` / `legislationNumber` are _verify_ |
| `us-senate-roll-calls` | senate.gov LIS roll-call vote XML | Senate roll calls with LIS member ids | `unverified-live` | congress.gov publishes no Senate roll calls; the XML URL pattern and element names are _verify_. Senate files carry LIS member ids, not bioguide IDs; the two are matched only through reviewable identity (LT07) |
| `us-govinfo-bills` | GovInfo API (`BILLS` collection) | text versions: version code, issue date, content hash, locator | `unverified-live` | Documented API with an api.data.gov key; `billVersion`, `dateIssued`, `lastModified`, `download.xmlLink` are _verify_ |
| `us-govinfo-billstatus` | GovInfo bulk data (`BILLSTATUS`) | the BILLSTATUS XML for a bill | `unverified-live` | Anonymous bulk data; element names (`bill/number`, `actions/item`, `cosponsors/item`, `laws/item`, `textVersions/item`) are _verify_ |
| `uk-parliament-bills` | UK Parliament Bills API | bill, sponsors, stages with sittings, publications | `unverified-live` | Anonymous documented API; field casing (`billId`, `stageSittings`, `publicationType`) is _verify_ |
| `uk-commons-divisions` | Commons Votes API | Commons divisions: ayes, noes, tellers per member id | `unverified-live` | Anonymous documented API; the division payload states no bill (_verify_), so a declared bill is an unlinked candidate |
| `uk-lords-divisions` | Lords Votes API | Lords divisions: contents, not contents, tellers | `unverified-live` | As for the Commons (_verify_ field casing) |
| `uk-hansard-debates` | Hansard API | debate sections as references (locators, contribution ids, member ids) | `unverified-live` | Anonymous documented API; contribution text is never retained; the section states no bill id (_verify_) |
| `us-senate-lda` (lobbying register `us-lda`) | Senate LDA REST API | LD-2 quarterly reports whose issue descriptions name bills | `unverified-live` | Added to the existing lobbying feature so US disclosures can name bills (LT08). Income and expense figures are not ingested (rounded point figures, not declared ranges); query parameters are _verify_ |

## Per-source contract

| Source | Endpoints (relative to the declared endpoint) | API key handling | Rate limits and pagination | Identifiers (stored as published) | Revision model |
| --- | --- | --- | --- | --- | --- |
| congress.gov bills | `/bill/{congress}/{type}/{number}`, `/actions`, `/cosponsors`, `/summaries` (`format=json`, `limit=250`) | `NOESIS_CONGRESS_GOV_API_KEY` sent as the `X-Api-Key` header; never in a URL, receipt or record | 5,000 requests/hour per key (_verify_); a list page with a `next` link or a larger `count` is `budget_exhausted`, never truncated | congress + bill type + number (`us-bill:156-hr-9901`), action code, bioguide ID, public-law number | `updateDate` / `updateDateIncludingText`; a changed record is a new document revision; actions keep date, code and text verbatim |
| congress.gov House votes | `/house-vote/{congress}/{session}/{roll}` and `/members` | as above | as above | chamber + congress + session + roll number (`us-roll-call:156-house-1-101`), bioguide ID | `updateDate` |
| senate.gov votes | `/vote{congress}{session}/vote_{congress}_{session}_{roll:05d}.xml` | none | undocumented; one file per vote | congress + session + vote number, LIS member id, the measure the vote names | `modify_date` |
| GovInfo BILLS | `/packages/{packageId}/summary`, `/packages/{packageId}/xml` (hashed, not stored) | `NOESIS_GOVINFO_API_KEY` as `X-Api-Key` | api.data.gov default (_verify_) | package id (`BILLS-156hr9901ih`), version code | `lastModified`; each version is its own package |
| GovInfo BILLSTATUS | `/{congress}/{type}/BILLSTATUS-{congress}{type}{number}.xml` | none | undocumented; one file per bill | BILLSTATUS bill identity (`us-bill-status:156-hr-9901`) | `updateDate` |
| UK Bills API | `/Bills/{billId}`, `/Bills/{billId}/Stages?Take=250`, `/Bills/{billId}/Publications` | none | undocumented (_verify_); `Skip`/`Take`, a longer list is `budget_exhausted` | bill id (+ introduced and included session ids), bill stage id, stage type id, sitting id, publication id, member id | bill `lastUpdate`; changed stages, sittings and publications are new document revisions; Royal Assent recorded only when a stage says so |
| Commons Votes API | `/division/{divisionId}.json` | none | undocumented | division id and number, member id | `PublicationUpdated`; a corrected list is a new revision |
| Lords Votes API | `/Divisions/{divisionId}` | none | undocumented | division id and number, member id | payload digest; a corrected list is a new revision |
| Hansard API | `/debates/debate/{debateSectionExtId}.json` | none | undocumented | section external id, contribution external id, member id | payload digest of the overview and contribution ids |

**Licences and attribution.** congress.gov, senate.gov and GovInfo content is
a US federal government work (17 U.S.C. 105), in the public domain in the US;
the source is credited and no endorsement is implied (_verify_ each site's
notice). UK Parliament data is used under the Open Parliament Licence v3.0 with
the statement "Contains Parliamentary information licensed under the Open
Parliament Licence v3.0." (`OGL_ATTRIBUTION`), carried by every UK provider
contract and shown in answers.

**Unavailable-access fallback.** A failed unit (HTTP error, redirect to
another host, missing API key, schema drift, a list longer than one page) fails
the run for that source with its code; earlier revisions stay current and
nothing is marked withdrawn, defeated or enacted because of a failure.

## Bounded first coverage

* **US:** one bill (`H.R. 9901` of the placeholder 156th Congress in the
  fixtures), its House roll call and Senate vote, three GovInfo text versions
  and its BILLSTATUS file. A live run replaces the placeholders with one
  Congress (the current one), at most 10 bills per source run
  (`max_pages` = the declared units), at most 250 actions and 250 cosponsors
  per bill (longer lists fail rather than truncate), and the roll calls the
  actions name. Justification: the dossier journey needs every record kind
  for a bill; a small declared set keeps the api.data.gov quota and review
  load bounded.
* **UK:** one bill of one session (`Bills API bill 3901`, session 2098-99 in
  the fixtures) with its stages, sittings and publications, and the divisions
  and Hansard sections an operator declares for it (each a candidate until
  reviewed). A live run uses the current session, at most 10 bills and 20
  divisions or debate sections per run.
* **Lobbying:** one bounded LDA query (filing year, period and client) per
  run; a response with a `next` page is refused.

No selection implies coverage of a whole Congress, session or register.

## Mapping gap against the existing dossier model

`src/domains/political/legislative_dossiers.py` (#1535) accepted only
committed Bundestag DIP and EUR-Lex document revisions, the jurisdictions `DE`
and `EU`, and a document-type-to-stage table. The gap and how LT02 closes it
without a second dossier store:

| Needed | Existing model | Change (LT02) |
| --- | --- | --- |
| US and UK jurisdictions | `DE`, `EU` only | `US`, `GB` added (additive contract change, `v1` kept) |
| Sources | two source ids | the nine legislation source ids, each bound to its jurisdiction (`legislation_mapping.LEGISLATION_SOURCES`) |
| Bill identity | procedure id from `metadata.political` | `us-bill:<congress>-<type>-<number>`, `uk-bill:<bill id>` (sessions kept beside it) |
| Stages, versions, votes | one stage per document type | one stage per acquired record; category from `legislation_mapping.stage_for` (version code / stage description decide proposal, amendment or adoption); the record as published rides along as `stage.legislation` |
| Debates | none | additive `debate` stage category (never counted as missing) |
| Records whose source names no bill | `review_candidates` only | still `review_candidates`; an accepted reviewer decision (`reviewed_links`) links it with `link_basis: reviewed_assertion` |
| Member positions, sponsors | none | kept inside the record (`fields.positions`, `fields.sponsors`, `fields.cosponsors`) as published |

Bundestag and EUR-Lex dossiers serialise exactly as before.
