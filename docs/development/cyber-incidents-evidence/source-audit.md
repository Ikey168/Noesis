# Cyber incidents: source-contract audit and bounded coverage (CY01)

Tracking: wave 2 tracker #2736 · recorded 2026-10-03.

No per-track tracker or delivery issue exists yet. They are opened once this
audit names a surviving source, which it does (SEC 8-K Item 1.05 filings, and
conditionally the Washington State breach list below), so the next step is to
open them and link them here.

This audit sets out, per source, what the Technology bundle's
`technology.cyber-incidents` provider (subdomain `cyber-incidents`, ADR-005)
may acquire, how, and on what terms. **It was written without network access:
the provider hosts (`data.sec.gov`, `www.sec.gov`, `efts.sec.gov`,
`ocrportal.hhs.gov`, `data.wa.gov`, `oag.ca.gov`) were blocked by this
runtime's egress proxy (verified 2026-10-03), so terms, endpoints and field
names were not re-verified live.** They come from the publishers'
documentation as the author knows it. Every item marked _verify_ must be
checked against the live pages and a real response before the first dated
live run. No source is `live` until that run exists.

**There is no machine-readable copy yet.** `PROVIDER_CONTRACTS`,
`BOUNDED_COVERAGE`, `CAPS`, `MINIMISATION`, `EXCLUSIONS` and
`LIVE_VERIFICATION` will be added in `src/ingestion/cyber_incidents_sources.py`
by the track's acquisition issues, and must match this audit.

Never, for any source: no attribution of an incident to an attacker, group or
state (a threat actor named in a filing stays inside the quoted passage and is
never extracted, linked or counted); no severity, materiality, impact or
liability judgement beyond what the filing or register states; no count of
affected people other than the source's own number; no reclassification of a
filing's items (an Item 8.01 cyber disclosure is not turned into an Item 1.05
one); no inference from absence (no Item 1.05 filing reads "no filing
observed", never "no incident"); and no merging of an SEC filing with a state
register row except by a reviewed identity decision.

## Access decisions

| Source | Publisher | Delivers | Access | Decision |
| --- | --- | --- | --- | --- |
| SEC EDGAR 8-K Item 1.05 filings | US Securities and Exchange Commission | Form 8-K and 8-K/A filings whose stated items include 1.05 (material cybersecurity incidents) for declared issuers: filing metadata and the Item 1.05 passage | submissions JSON per CIK, then the filing's primary document; declared User-Agent required | `unverified-live` |
| Washington State Attorney General breach notifications | Washington State Office of the Attorney General | notices of breaches affecting more than 500 Washington residents: organisation, dates, residents affected, information types, cause category as the AG states it | state open-data portal (Socrata SODA API) copy of the AG list (_verify_ that the dataset exists and its id) | `unverified-live`, conditional: if the live check finds no structured publication, the source moves to `not-implemented` |
| HHS OCR breach portal | US Department of Health and Human Services, Office for Civil Rights | HIPAA breaches affecting 500 or more individuals: covered entity, state, entity type, individuals affected, submission date, breach type, location | web application listing; no documented API (_verify_) | `not-implemented`: the export needs a stateful form postback of the web application, which the no-scraping rule excludes; covered entities include individual practitioners, so names cannot be assumed to be organisations. Revisit if HHS publishes a documented downloadable dataset (_verify_ data.gov or HealthData.gov) |

Documented, not acquired: EDGAR full-text search
(`https://efts.sec.gov/LATEST/search-index`, the backend of the EDGAR
full-text search page; not a documented API, _verify_), which an operator may
use by hand to choose the declared issuers but which no adapter calls;
8-K Item 8.01 voluntary cyber disclosures (finding them needs text
classification, which is inference); Form 6-K and 10-K Item 1C (no incident
item, and Item 1C describes risk management, not incidents); the California
Attorney General list (`oag.ca.gov/privacy/databreach/list`, an HTML table with
PDF notice letters) and other state lists (Maine, Vermont, Oregon and others),
which publish HTML or PDF only (_verify_).

**Unavailable-access fallback.** A failed unit (no `NOESIS_EDGAR_USER_AGENT`
configured, HTTP error, 403 or 429 from SEC fair-access throttling, redirect to
an undeclared host, schema drift, a document over the byte budget) fails with
its code and a receipt; a live run without the User-Agent is refused as
`source_unavailable`, never sent anonymously. Earlier revisions stay current
and nothing is marked removed because of a failure.

## Per-source contract

| Source | Endpoints | Authentication | Licence and redistribution | Rate limits | Revision model: updates, amendments, corrections, removals |
| --- | --- | --- | --- | --- | --- |
| SEC 8-K Item 1.05 | `https://data.sec.gov/submissions/CIK##########.json` (`filings.recent`: `accessionNumber`, `form`, `items`, `filingDate`, `reportDate`, `acceptanceDateTime`, `primaryDocument`; older pages under `filings.files`, _verify_ that `items` lists `1.05`); `https://www.sec.gov/Archives/edgar/data/{cik}/{accession without dashes}/{primaryDocument}` | no key; the fair-access policy requires a User-Agent naming the operator and a contact, read from `NOESIS_EDGAR_USER_AGENT` (a documented variable, not a secret), as `src/ingestion/connectors/edgar.py` already does | EDGAR filings are public records; SEC website content may be copied (_verify_ the SEC's privacy and website notice). Filing text is written by the registrant, so it is **not** a US Government work: passages are stored as citations with the accession and document digest, not relicensed | fair access: at most 10 requests per second (_verify_); paced at 2 requests per second | Item 1.05 applies from 2023-12-18, for smaller reporting companies from 2024-06-15 (_verify_). An 8-K/A that adds information not known at first filing (_verify_ Instruction 2 to Item 1.05) is its own revision, linked to the original where it states the original or shares issuer and `reportDate` (the pattern `edgar_materials.py` uses for Item 2.02); an unlinkable amendment is kept with `amendment_link: not-stated`. Nothing is merged; the original keeps its passage. A declared accession answering 404 is a `removed_by_source` revision. A stated Attorney General delay is recorded only as the filing words it |
| Washington AG list | `https://data.wa.gov/resource/{dataset-id}.json` with `$where` on the reported date, `$order`, `$limit`; dataset metadata `https://data.wa.gov/api/views/{dataset-id}.json` (_verify_ id, host and field names) | none; optional Socrata app token `NOESIS_SOCRATA_APP_TOKEN` (`optional-secret`) raises throttling limits (_verify_) | Washington open-data terms (_verify_); public record; cite the AG and the dataset | anonymous requests throttled, app token raises the limit (_verify_); one request per window | the dataset's `rowsUpdatedAt` (_verify_) dates each release; a changed row is a `revised` revision; a declared-window row missing from a later complete answer is `not-returned`, stated as an observed absence, not a deletion |
| HHS OCR | `https://ocrportal.hhs.gov/ocr/breach/breach_report.jsf` (_verify_) | none | US Government work (_verify_) | n/a | entries move from "under investigation" to the archive (_verify_); not acquired |

## Record shape and reuse

Record shapes (`packs/taxonomy.json`): **events-notices** for the disclosed
incident notice (issuer or organisation, dates, items or categories as stated,
revision chain including 8-K/A amendments); **versioned-documents** for the
filing itself, cited by accession, document digest and the character span of
the Item 1.05 passage. No new shape is needed.

Reuse points (paths verified in this checkout):

- `src/ingestion/connectors/edgar.py`: `EdgarClient` (`submissions`,
  `filing_document` with the `MAX_INLINE_FILING_BYTES` ceiling), `normalize_cik`
  and `primary_document_for_accession`; no second SEC client is written.
- `src/ingestion/connectors/edgar_materials.py`: the 8-K `items` filter and
  the 8-K/A linking pattern used for Item 2.02, applied to Item 1.05.
- Market filings: the issuer is the CIK, the key `src/domains/market/research.py`
  (`MarketResearchStore`) and the filings connector
  (`src/ingestion/connectors/filings_connector.py`) already use, so an incident
  notice links to the issuer's market materials by CIK, never by name.
- `technology.vulnerabilities` (`src/kb/vulnerabilities.py`): a notice links to
  a CVE only where the filing text states the CVE id; nothing is inferred
  from product names or dates.
- Transport, budgets and receipts: `src/ingestion/source_pack_runtime.py` and
  `src/ingestion/provider_execution.py`.

## Data minimisation decision

Filings and breach registers concern organisations, but a filing's signature
block names officers and a register can name a sole trader or an individual
practitioner. Decision:

- **Stored:** issuer CIK and name as filed, form, accession, filing date,
  acceptance time, `reportDate`, the item list as stated, the primary
  document's URL and SHA-256, the Item 1.05 passage verbatim with its span; for
  the Washington list, the organisation name as published, dates, residents
  affected, information types and the AG's cause category as published.
- **Redacted:** a register row that review identifies as naming a natural
  person is withheld (`withheld_person`), counted and never returned.
- **Excluded:** signature blocks and signatory names, cover-page contact
  details, exhibits (press releases included) in first coverage, individual
  notice letters, any list of affected people; no person entity is extracted
  from a passage. Queries are keyed by issuer or organisation only.
- **Retention:** revisions are kept for provenance with their run; withheld
  rows keep only their count and reason.
- **Who may query:** `knowledge:technical:cyber-incidents:read` with namespace
  access; writes `...:write`; identity and withheld-row reviews `...:review`.

## Bounded first coverage

| Source | Selection | Caps |
| --- | --- | --- |
| SEC 8-K Item 1.05 | three to five declared issuers (by CIK) known to have filed under Item 1.05, named in the acquisition issue; forms 8-K and 8-K/A accepted in a declared window starting 2023-12-18 | 5 issuers, 1 submissions document plus at most 2 older pages per issuer, 10 filings per issuer, 5 MB per primary document |
| Washington AG list | one window of at most 92 days of reported dates | 1 request, 500 rows; more is `budget_exhausted`, never truncated |

Justification: a few issuers with an original filing and at least one 8-K/A
show the amendment chain, and one state window shows a register beside the
filings without merging them. Every further issuer or window is a source-pack
version bump.

## Live verification

| Source | Status | Checked | Evidence |
| --- | --- | --- | --- |
| SEC 8-K Item 1.05 | `unverified-live` | - | none yet |
| Washington AG list | `unverified-live` | - | none yet; existence of the structured copy is itself _verify_ |
| HHS OCR breach portal | `not-implemented` | - | no documented machine interface (_verify_) |

Fixtures will be authored, not captured: fictional issuers with invented CIKs
and accession numbers, the domain `example.org`, fictional organisations, and
filing and report dates in 2094-2099, so nothing can be mistaken for a
published filing or notice.
