# Clinical Evidence medicines regulation: source-contract audit and bounded coverage (MR01)

Tracking: #2214 · delivery issue #2381 · recorded 2026-09-29.

This audit sets out, per source, what the Clinical Evidence pack's optional
`medicines` feature may acquire, how, and on what terms. It was written without
network access. Endpoints, layouts, field names and terms come from the
providers' published documentation as the author knows it. **Every item marked
_verify_ must be checked against the live page and a published response before
the first dated live run (MR14, #2429). No provider is `live` until that run
exists.** The machine-readable copy of these decisions is `PROVIDER_CONTRACTS`
in `src/ingestion/medicines_sources.py`, merged into the clinical
`PROVIDER_CONTRACTS` of `src/ingestion/clinical_providers.py` (MCP tool
`clinical_provider_contracts`). The sources are entries of the existing
`config/source_packs/clinical-evidence.json` (version 0.1.2, the 0.1.1 sources
unchanged).

These non-goals apply to every source:

- Records quote the regulator. Label and communication text is kept verbatim
  with a locator; nothing is summarised, paraphrased or rated.
- No prescribing, dosing or treatment advice. Dosing sections (SmPC 4.2 and
  4.9; SPL LOINC 34068-7, 43678-2 and 34088-5) are listed as omitted with their
  code and title, and their text is never retained, as in the existing openFDA
  label parser.
- No efficacy or safety verdict beyond quoting the regulator. FAERS figures stay
  reporting counts, as the existing openFDA provider already labels them.
- A status change is a new dated record. Earlier records and revisions are never
  rewritten or deleted.

## Access decisions

| Source | Delivers | Access | Auth | Licence / terms and attribution | Rate limits | Decision |
| --- | --- | --- | --- | --- | --- | --- |
| EMA medicine data and EPARs | authorisation status and dates (grant, refusal, suspension, withdrawal), EPAR revision number and date, latest procedure affecting the product information; SmPC (Annex I) sections; withdrawal public statements | medicines data export (JSON) at `/en/documents/report/medicines-output-medicines_json-report_en.json` (the file the existing `ema-medicines` source reads), filtered to pinned EMA product numbers; product information and public statements at pinned `/en/documents/...` paths | none | EMA legal notice: reproduction authorised provided the source is acknowledged; the attribution sentence is stored on every record | none documented; bounded to 50 products and 3 requests per product | `unverified-live` |
| Drugs@FDA (openFDA) | applications, products with marketing status, every submission (ORIG, SUPPL incl. LABELING) with status and date | **reuses the existing `openfda` provider** in `src/ingestion/clinical_providers.py` (`OpenfdaAdapter`, `/drug/drugsfda.json`) through a new `drugsfda-submissions` endpoint of the same adapter; **no second openFDA client is planned or built** | the openfda provider's optional key `NOESIS_OPENFDA_API_KEY` (request parameter only, never stored) | openFDA Terms of Service; openFDA's `meta.disclaimer` is required on and stored with every record, as in the existing provider | 240/min; 1,000/day without a key, 120,000/day with one | `unverified-live` |
| DailyMed SPL | SPL version history; the current SPL document with LOINC-coded sections, active ingredients, brand names and approval (application) numbers | web services v2: `/dailymed/services/v2/spls/{setid}/history.json` and `/dailymed/services/v2/spls/{setid}.xml`, per pinned set id | none | NLM terms: DailyMed content is public; cite DailyMed and the set id; no NLM endorsement implied (attribution stored) | none documented; bounded to 50 set ids, 2 requests each | `unverified-live` |
| RxNorm (RxNav) | RxCUI, term type and ingredient concepts for published names; the RxNorm release version | RxNav REST `/REST/version.json`, `/REST/rxcui.json?name=&search=0` (exact), `/REST/rxcui/{rxcui}/properties.json`, `/REST/rxcui/{rxcui}/related.json?tty=IN`, through the bounded `RxNavClient` used by identity resolution | none | NLM RxNav terms; RxNorm is UMLS-derived, NLM-produced (SAB=RXNORM) content is usable without a UMLS licence (_verify_); cite RxNorm and the release | NLM guidance of 20 requests/second per IP; bounded to 50 names per resolution run | `unverified-live` |
| FDA Drug Safety Communications | title, issue date, dated updates, named generic and brand names, verbatim paragraphs | pinned communication pages (HTML) under `/drugs/drug-safety-and-availability/`. The index page is HTML and no documented feed of communications was identified (_verify_ whether an FDA RSS feed covers DSCs), so the operator pins communications; nothing is crawled | none | US government work (public domain); cite FDA and the page URL (attribution stored) | none documented; bounded to 50 pages, one request each | `unverified-live` |

## Revision behaviour

- **EMA.** The export carries a `revision_number` and `last_updated_date` per
  medicine and the latest procedure affecting the product information
  (`EMEA/H/C/xxxxxx/II/xxxx`). The product information in force is published at
  a stable URL, so an earlier SmPC revision is the one acquired while it was
  current; revisions are keyed by EMA product number and revision number and
  kept side by side. Export field names beyond the ones the existing
  `ema-medicines` parser reads (`withdrawal_of_marketing_authorisation_date`,
  `suspension_of_marketing_authorisation_date`,
  `date_of_refusal_of_marketing_authorisation`,
  `latest_procedure_affecting_product_information`) are _verify_. The
  product information is a PDF; its text is extracted with the existing
  `pdf_parser.extract_pdf_text` (PyMuPDF, optional) and the offline fixtures are
  authored text renditions. A withdrawal reason is quoted from the public
  statement with its paragraph locator.
- **Drugs@FDA.** Each submission is a dated record keyed by application number
  and submission type and number (original approval, supplements, labeling
  revisions). Marketing status is current-only in openFDA, so a changed status is
  a new record and the earlier one stays as history; its effective date is
  unknown (not published) and listed in `unknowns`.
- **DailyMed.** `history.json` lists every SPL version with its published date;
  the web services serve the current version's document only. Each version's
  text is the one acquired while it was current and is retained; versions listed
  but never acquired are reported (`versions_text_not_acquired`). Records are
  keyed by set id and version number with the SPL effective time.
- **FDA DSC.** The issue date is published on the page (`[M-D-YYYY] FDA Drug
  Safety Communication`); an update is a dated paragraph added to the same page
  (`[M-D-YYYY] UPDATE ...`) and becomes a new revision of the same record, the
  original kept. The page structure is _verify_.

## Bounded medicine set

| Medicine | EU | US | Why |
| --- | --- | --- | --- |
| noetiglutide (fixture: *Noetiglu*) | EMA product `EMEA/H/C/009001`, authorised, SmPC revisions 3 and 4 | Drugs@FDA `NDA299001` (original approval and a labeling supplement), SPL set `9a1f0c3e-0000-4000-8000-000000009001` versions 7 and 8 | a product in both jurisdictions **with a safety communication** (pancreatitis DSC with one update), a label revision adding a boxed warning, cited trial `NCT09000001` and FAERS counts already held by the pack |
| fixturamab (fixture) | EMA product `EMEA/H/C/009002`, **withdrawn** 2024-11-15 with a public statement | none | a withdrawn product; an EU-only substance without RxNorm coverage (reported unmatched) |

The offline fixtures use these fictional placeholders (see
`tests/fixtures/clinical/README.md`). The live bounded set for MR14 swaps in
real products of the same shape (one EU+US product with a DSC, one withdrawn EU
product) chosen at run time; that choice is recorded with the run.

## Identity (MR07)

US products, labels, named substances and FAERS product names are looked up in
RxNav by their published name (exact search); an exact RxNorm name is an
`equivalent` crosswalk accepted by rule (`rxnav-exact-name`) and remains
reviewable, rejectable and revertible. EU products are matched only by their
active substance, as `narrower` than the ingredient concept, and start
`proposed` until a reviewer accepts them. Names without an RxNorm concept are
reported unmatched. The RxNorm release is stored on every crosswalk record.
