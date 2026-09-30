# Regulatory enforcement: source-contract audit, minimisation decision and bounded coverage (EN01)

Tracking: #2651 · delivery issue #2655 · recorded 2026-09-30.

This audit sets out, per source, what the Legal pack's `legal.enforcement`
provider (optional features `enforcement-sec`, `enforcement-fca`,
`enforcement-epa`, `enforcement-edpb`) may acquire, how, and on what terms.
**The official pages could not be fetched from this runtime**: the egress
proxy refused `www.sec.gov`, `www.fca.org.uk`, `echo.epa.gov` and
`www.edpb.europa.eu` on 2026-09-30. The points below come from web-search
snippets of those official pages read on 2026-09-30 (cited per point) and from
the documented shapes of the services; **every item marked _verify_ must be
checked against the live pages, the live terms and a real response before the
first dated live run (EN14, #2720). No source is `live` until that run
exists.** The machine-readable copy of these decisions is `PROVIDER_CONTRACTS`,
`DECLINED`, `IDENTIFIERS`, `BOUNDED_COVERAGE`, `MINIMISATION` and
`LIVE_VERIFICATION` in `src/ingestion/enforcement_sources.py`
(`MINIMISATION` is defined in `src/kb/enforcement_records.py`); each source
entry in `config/source_packs/legal.json` (`legal-research` 1.5.0, earlier
sources verbatim) states `enforcement.live_verification: unverified-live`, and
the MCP tool `enforcement_source_contracts` returns the same decisions.

Non-goals for every source: no risk or compliance scoring, no inference of
wrongdoing from an initiated action, no merging of a settled "neither admit
nor deny" outcome into a finding, no summing of penalties across currencies or
authorities, no profiling of named individuals and no legal advice.

## Sources read (2026-09-30)

| Point | Source | Status |
| --- | --- | --- |
| SEC enforcement landing page, litigation releases, administrative proceedings | https://www.sec.gov/enforcement-litigation, https://www.sec.gov/enforcement-litigation/litigation-releases, https://www.sec.gov/enforcement-litigation/administrative-proceedings | fetch refused by the egress proxy; titles and descriptions from search results only (_verify_) |
| SEC litigation-release RSS feed `/rss/litigation/litreleases.xml` | https://www.sec.gov/about/rss-feeds | search snippet (_verify_); used for discovery only, never as the record |
| SEC fair access: at most 10 requests per second per user, a declared User-Agent with contact, a 10-minute block when exceeded | https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data, https://www.sec.gov/about/webmaster-frequently-asked-questions | search snippet (_verify_ that it applies to the enforcement pages, not only EDGAR) |
| FCA final notices list | https://www.fca.org.uk/news/search-results?np_category=notices%20and%20decisions-final%20notices | fetch refused; not read |
| FCA final notices are PDFs at `/publication/final-notices/{slug}.pdf` stating "To:", "Firm Reference Number" or "Individual Reference Number", the date, the penalty and the "30% (stage 1) discount" wording | search results listing e.g. https://www.fca.org.uk/publication/final-notices/barclays-bank-uk-plc-2025.pdf | search snippets (_verify_ the text layer of real notices) |
| FCA copyright: information made available under the Open Government Licence; other re-use needs FCA permission | https://www.fca.org.uk/legal | search snippet (_verify_) |
| ECHO Enforcement Case REST services (`case_rest_services.get_case_report`, `get_crcase_report`, `get_cases`), GET, XML/JSON/JSONP | https://echo.epa.gov/tools/web-services | search snippet (_verify_ JSON key names; the fixtures use an authored shape) |
| EDPB register of Article 60 final decisions: lead and concerned supervisory authorities, legal reference, decision, key words, links to final decisions and English summaries; some authorities (DE, LT, NL) publish none or only some decisions, and personal data of physical or legal persons may be omitted | https://www.edpb.europa.eu/our-work-tools/consistency-findings/register-for-article-60-final-decisions_en | search snippet (_verify_ field classes) |
| EDPB reuse: authorised for commercial and non-commercial purposes with acknowledgement, without distorting meaning | https://www.edpb.europa.eu/copyright_en | search snippet (_verify_) |

## Access decisions

| Source (source-pack id) | Provider | Delivers | Decision | Reason |
| --- | --- | --- | --- | --- |
| `sec-litigation-releases` | U.S. SEC | litigation releases (civil actions in federal court) | `unverified-live` | Public-domain US government pages; bounded declared releases; fair-access User-Agent declared in the source |
| `sec-administrative-proceedings` | U.S. SEC | administrative-proceeding release pages | `unverified-live` | As above; keyed by release and `3-` file number |
| `fca-final-notices` | UK FCA | final notices (PDF) addressed to firms | `unverified-live` | OGL per the FCA copyright notice (_verify_); text read from the PDF text layer - a notice whose text layer cannot be read fails the unit with `schema_drift` |
| `epa-echo-cases` | U.S. EPA (ECHO) | civil and criminal case reports | `unverified-live` | Documented REST services, no key; public domain |
| `edpb-art60-decisions` | EDPB | Article 60 register entries | `unverified-live` | Reuse with acknowledgement; bounded declared entries |

No candidate source is refused outright on its terms. Declined within scope
(`DECLINED`): SEC trading suspensions (not an action against a respondent),
FCA decision notices and warning notice statements (not final), FCA final
notices addressed only to individuals (minimisation), ECHO bulk downloads
(per-case acquisition suffices) and national data-protection authority
registers outside the Article 60 register (different terms per authority).

## Per-source contract

| Source | Endpoint and request (relative to the declared endpoint) | Authentication and keys | Rate limits and pagination | Identifiers (stored as published) | Revision model (updates, corrections, removals) |
| --- | --- | --- | --- | --- | --- |
| SEC litigation releases | `/enforcement-litigation/litigation-releases/{lr-nnnnn}` | none; `User-Agent` declared per source (`enforcement.user_agent`), never a secret | 10 requests/second (_verify_); one page per declared release, at most 20 per run | release `LR-nnnnn`, respondent CIK where the release states `(CIK No. ...)`, federal civil action number (citation) | `article:modified_time` or the page digest is the revision; a changed page is a new action revision; HTTP 404/410 is a removal revision |
| SEC administrative proceedings | `/enforcement-litigation/administrative-proceedings/{release}` | as above | as above | release (`33-`, `34-`, `IA-`, `IC-`, `AE-`), file number `3-nnnnn` | as above |
| FCA final notices | `/publication/final-notices/{slug}.pdf` | none | none published (_verify_); one PDF per declared notice, at most 20 per run | notice slug, Firm Reference Number, Upper Tribunal reference (`FS/yyyy/nnnn`) | the PDF digest is the revision; a replaced PDF (a correction) is a new revision marked `corrected` when the notice says so; 404/410 is a removal revision |
| EPA ECHO | `/echo/case_rest_services.get_case_report?output=JSON&p_id={case}` (criminal: `get_crcase_report`) | none | none published (_verify_); one report per declared case, at most 20 per run | case number, ICIS activity id, FRS registry id per facility, court docket number (citation) | `LastUpdated` (or the digest) is the revision; changed penalties or status are a new revision |
| EDPB Article 60 register | `/our-work-tools/consistency-findings/register-for-article-60-final-decisions/decision-no-{n}_en` | none | none published (_verify_); at most 20 entries per run | register entry number, EDPBI identifier, lead and concerned authority codes | page modified time or digest; a corrected entry is a new revision; an entry no longer served is a removal revision |

**Unavailable-access fallback.** A failed unit (HTTP 401/403/429/5xx, a
redirect to another host, schema drift, an unreadable PDF text layer) fails
the run for that source with its code; earlier revisions stay current and
nothing is marked decided, corrected or removed because of a failure. Only an
explicit 404/410 for a declared unit is recorded as a removal, and it is a new
revision of the action, never a deletion.

## Minimisation decision

Enforcement releases name natural persons (officers, traders, sole
advisers). The decision, enforced at ingestion (`enforcement_sources._Builder`)
and again at write time (`enforcement_records.validate_record` refuses a
natural-person respondent with a name, identifiers or no pseudonym, and any
personal field anywhere in a record, with `minimisation_violation`):

* **Organisations** (a published name carrying a legal-form or public-body
  token - the Legal courts feature's list plus B.V., N.V., S.A., SARL,
  S.p.A., KG): the name as published and the identifiers the regulator
  published (CIK, FRN, LEI, company number); matched to entities only through
  reviewable identity (EN07).
* **Natural persons** - and every respondent whose type is unclear - keep an
  **action-scoped pseudonym** (`natural person N`) and the role as published,
  nothing else. Names, individual reference numbers (FCA IRN), CRD numbers,
  addresses, ages and dates of birth are never stored. Their names are
  replaced by the pseudonym in every quoted text (titles, outcome sentences,
  appeal text) before the record is written, and a penalty stated only for a
  natural person is not recorded (the receipt counts it).
* **Individual-only actions** (an FCA final notice to an individual, a release
  naming only individuals) are **not recorded**; the acquisition receipt
  counts them as `individual_only_actions`.
* **Retention**: records are immutable revisions like every other record;
  pseudonyms derive from the action and ordinal, never from the name, so they
  cannot be linked across actions.
* **Who may query**: holders of `knowledge:legal:read` (and namespace access)
  see organisations and pseudonyms. Nobody can use a natural person as a query
  key (`natural_person_not_a_query_key`) or as a monitor target; natural
  persons are never identity candidates.

## Bounded first coverage

* **Entities**: companies already acquired by the Corporate Ownership sources;
  fixtures use the fictional Exampla group (Exampla Holdings plc, CIK
  0009999101; Exampla UK Limited, FRN 999002; Exampla Intermediate B.V.) and
  the unmatched Northwind Payments Limited (FRN 999777).
* **Selections**: actions are declared by release, notice slug, case number or
  register entry (at most 20 per source per run), never crawled. Fixtures:
  SEC `LR-99901` and `34-99902`, FCA `exampla-uk-limited-2099`,
  `northwind-payments-limited-2099` and the withheld `jordan-example-2099`,
  ECHO `04-2099-0101`, EDPB entries `99901` (lead IE) and `99902` (lead NL,
  controller and fine not published).
* **Period**: actions published or decided from 2020-01-01; a live run names
  at most 20 actions per source within one year.
* **Places**: EPA facilities keep their FRS registry id and the published
  coordinates only; no geocoding and no inference of a place from a name.

No selection implies coverage of a regulator, a statute or a company.

## LIVE_VERIFICATION

| Provider | Intended status after EN14 | Current status |
| --- | --- | --- |
| us-sec | `verified-live` after a dated run with the declared User-Agent and a check of the Drupal field classes | `unverified-live` |
| uk-fca | `verified-live` after a dated run confirming the PDF text layer of real notices and the reuse terms | `unverified-live` |
| us-epa-echo | `verified-live` after a dated run confirming the JSON key names | `unverified-live` |
| edpb | `verified-live` after a dated run confirming the register field classes | `unverified-live` |

Revisions acquired live while a provider is `unverified-live` are withheld
from subscription notifications (EN11) until the dated live run exists.
