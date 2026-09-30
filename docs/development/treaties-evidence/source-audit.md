# Treaties: source-contract audit, licence decisions, minimisation and bounded coverage (TR01)

Tracking: #2581 · delivery issue #2586 · recorded 2026-09-30.

This audit sets out, per source, what the Legal pack's treaties provider
(`legal.treaties`, features `treaties-untc`, `treaties-eu`, `treaties-coe`) may
acquire, how, and on what terms. **The official pages could not be fetched from
this runtime: the egress proxy blocks `treaties.un.org`, `www.un.org`,
`www.coe.int`, `op.europa.eu` and `publications.europa.eu`.** Terms and
endpoints below come from search-result extracts of the official pages, read on
2026-09-30 and cited with their URLs, and from the Legal pack's existing CELLAR
adapter. Every item marked _verify_ must be checked against the live page, the
live terms and a real response before the first dated live run (TR13, #2645).
No source is `live` until that run exists.

The machine-readable copy of these decisions is `PROVIDER_CONTRACTS`,
`LICENCE_DECISIONS`, `MINIMISATION`, `BOUNDED_COVERAGE` and `LIVE_VERIFICATION`
in `src/ingestion/treaties_sources.py`; each source entry in
`config/source_packs/legal.json` (`legal-research` 1.5.0, earlier sources
verbatim) states `treaties.live_verification` and `treaties.licence_decision`,
and the MCP tool `treaties_source_contracts` returns the same decisions.

Non-goals for every source: no legal advice, no inference of obligations or
compliance, no interpretation of the legal effect of reservations, declarations
or objections, and no treaty-text redistribution beyond what each source
licenses (treaty texts are linked, never stored).

## Access decisions

| Source (source-pack id) | Provider | Delivers | Decision | Reason |
| --- | --- | --- | --- | --- |
| `untc-multilateral-status` | UN Treaty Collection (UN Office of Legal Affairs, Treaty Section) | status pages of multilateral treaties deposited with the Secretary-General: participants, signatures, consents, declarations, reservations, objections, notes | `declined` (not implemented for acquisition) | The UN terms of use allow downloading for personal, non-commercial use "without any right to resell or redistribute them or to compile or create derivative works"; the online collection is stated to be proprietary and reusable only with prior written permission. Building a status store is a compilation. The adapter and parser exist and are fixture-tested, but refuse to fetch (`licence_declined`) until an operator records an accepted decision with its permission reference. |
| `cellar-eu-international-agreements` | Publications Office of the EU (CELLAR SPARQL) | EU international agreements (CELEX sector 2): expressions, signature and entry-into-force dates, linked EU acts | `unverified-live` | Reuse of EUR-Lex/CELLAR documents is authorised with acknowledgement (Commission Decision 2011/833/EU). Reuses the Legal pack's CELLAR adapter, whose base query has prior live evidence; the agreement query's `cdm:resource_legal_date_signature` and any declared relation IRIs are _verify_. |
| `coe-treaty-office-charts` | Council of Europe Treaty Office | chart of signatures and ratifications; reservations, declarations, withdrawals and denunciations | `unverified-live` | Reproduction of Council of Europe web material is authorised for private, informational and educational use with the source acknowledged; commercial use needs prior permission (the operator confirms the use). No documented API was found; the chart and declarations pages are parsed in the published layout, which is _verify_. |

### UN Treaty Collection licence decision

* UN terms of use: "The United Nations grants permission to Users to visit the
  Site and to download and copy the information, documents and materials ...
  for the User's personal, non-commercial use, without any right to resell or
  redistribute them or to compile or create derivative works therefrom"
  (search extract of <https://www.un.org/en/about-us/terms-of-use> and the
  same standard text at
  <https://www.un.org/Depts/los/LEGISLATIONANDTREATIES/terms_and_conditions.htm>,
  read 2026-09-30; the pages themselves were not fetchable - _verify_).
* "Permission is required to reuse content from any and all United Nations'
  online platforms and databases, including legal and statistical databases";
  permissions are requested through the Copyright Clearance Center (search
  extract of <https://shop.un.org/rights-permissions>, read 2026-09-30 -
  _verify_).
* "While each individual treaty text is in the public domain, the online UN
  Treaty Collection is proprietary and cannot be reproduced, translated,
  distributed, sold or otherwise used without a prior written permission"
  (search extract, attributed to the UN, of
  <https://commons.wikimedia.org/wiki/Commons:Copyright_rules_by_territory/United_Nations>,
  read 2026-09-30 - a secondary source; _verify_ against the UN's own notice).

Decision (`declined`, recorded 2026-09-30): the `untc-multilateral-status`
entry makes no request and stores no record; readiness and every answer list
UNTC as "not acquired (declined licence decision)". The `treaties-untc`
feature stays selectable, and the subdomain's offline coverage rests on the
CELLAR and Council of Europe sources. An operator who obtains written
permission records it in the entry's `licence_decision` (`status: accepted`,
`reference` to the permission) and sets `live_verification: unverified-live`;
the adapter then fetches the declared status pages only.

## Per-source contract

| Source | Endpoints (relative to the declared endpoint) | Key handling | Rate limits and pagination | Identifiers (stored as published) | Revision model |
| --- | --- | --- | --- | --- | --- |
| UNTC | `/Pages/ViewDetails.aspx?mtdsg_no={chapter-number}&chapter={chapter}&clang=_en` (URL pattern seen in search results for <https://treaties.un.org/pages/ViewDetails.aspx?mtdsg_no=XVIII-10&chapter=18&clang=_en>, read 2026-09-30 - _verify_) | none | none published (_verify_); one page per declared treaty | MTDSG chapter and number, UNTS registration number, UNTS volume reference, participant as published | the page's "Status as at" stamp is the depositary revision; a changed record is a new revision; an action no longer listed is a `removed-by-source` revision |
| CELLAR | the public SPARQL endpoint `https://publications.europa.eu/webapi/rdf/sparql`: the Legal CELLAR adapter's bounded query, plus one agreement query on the same host | none | no published quota (_verify_); 1-20 CELEX per source, one page of at most `page_size` rows (a full page is `budget_exhausted`, never truncated) | CELEX, ELI, CELLAR work/expression/manifestation/item URIs, linked acts' CELEX | no source stamp: a changed result is a new revision dated by the acquisition |
| Council of Europe | `/full-list?module=signatures-by-treaty&treatynum={number}` (seen in search results for <https://www.coe.int/en/web/conventions/full-list?module=signatures-by-treaty&treatynum=185>, read 2026-09-30) and `/full-list?module=declarations-by-treaty&treatynum={number}` (_verify_) | none | none published (_verify_); two pages per declared treaty | CETS/ETS number, state or organisation as charted | the chart's "Status as of" date is the depositary revision; denunciations and withdrawals are actions, never deletions |

**CELLAR agreement facts.** CDM names a "Date of signature" data property
(`cdm:resource_legal_date_signature`) and a "Date of entry into force"
(`cdm:resource_legal_date_entry-into-force`, which can be multi-valued - every
value is kept, never collapsed) (search extracts of
<https://publications.europa.eu/resource/ontology/cdm> and
<https://github.com/Honeyfield-Org/eurlex-mcp-server/issues/48>, read
2026-09-30 - _verify_). CDM also has an object property labelled "Decision or
regulation associates an international agreement"; its IRI could not be
verified, so it is not used by default: an operator may declare verified CDM
relation IRIs in `selection.act_relations`. By default the linked acts are
those CELLAR relates to the agreement by `cdm:work_cites_work` (either
direction), a predicate the Legal adapter already queries. No conclusion-date
property was verified, so the record states `conclusion.date: null` with that
reason and cites the linked acts with their own document dates; the act's role
(signing, concluding, implementing) is never inferred.

**Council of Europe terms.** "Unless otherwise indicated, reproduction of
material posted on Council of Europe websites ... is authorised for private use
and for informational and educational uses relating to the Council of Europe's
work"; reproduction is authorised with the source acknowledged, and commercial
use needs prior permission (search extracts of
<https://www.coe.int/en/web/portal/disclaimer> and
<https://www.coe.int/en/web/portal/copyright-licensing-permissions>, read
2026-09-30 - _verify_). The Treaty Office legal notice states that online
documents may not exactly reproduce the adopted text and that only the treaties
published by the Secretary General in the Council of Europe Treaty Series are
authentic (search extract of <https://www.coe.int/en/web/conventions/disclaimer>,
read 2026-09-30). Noesis therefore links texts and stores only the chart
entries and the declarations as published, with the attribution
`COE_ATTRIBUTION`.

**Updates, corrections and removals.** Each run re-reads the declared pages.
A record whose content is unchanged adds nothing (a new status stamp alone is
not a change). A depositary correction (a corrected date, a changed text) is a
new revision with the new stamp; an action the source no longer lists for a
re-published treaty is a `removed-by-source` revision and a later re-listing a
`relisted` revision. Nothing is overwritten or deleted.

**Unavailable-access fallback.** A failed unit (HTTP error, redirect to another
host, schema drift, an over-budget page) fails the run for that source with its
code; earlier revisions stay current, and nothing is marked withdrawn or
corrected because of a failure.

## Minimisation decision

Treaty sources name states and organisations, not private individuals, but
official texts (declarations, notes) can mention an office holder. The
decision, enforced at write time by `check_minimisation` and on every MCP
answer by `guarded`:

* **Stored:** participants as the source names them (state, international
  organisation or the EU; the kind as the source's section states it), the
  codes a source publishes for them, and the texts of reservations,
  declarations, objections, withdrawals, denunciations and notes verbatim.
* **Never stored:** signatory or representative names as fields, contact
  details, and treaty full texts (linked, not reproduced). A record carrying a
  `signatory`, `representative`, `person`, `contact`, `email`, `address` or
  similar key is refused (`minimisation_violation`).
* **Person text:** an office holder mentioned in an official text stays in the
  quoted text; it is never parsed into a person record, used as a query key or
  a subscription target.
* **Retention:** revisions are kept for as long as the namespace retains Legal
  records; corrections and removals by the source are revisions.
* **Who may query:** `knowledge:legal:read` with namespace read access;
  identity review needs `knowledge:legal:review`.

## Bounded coverage

| Source | First coverage | Justification |
| --- | --- | --- |
| UNTC | the declared MTDSG treaties, at most 20 per source (fixture: fictional XXIX-99, 2098) | declined: nothing is acquired until written permission is recorded |
| CELLAR | 1-20 declared sector-2 CELEX numbers and 1-24 languages (fixture: fictional 22099A0101(01), ENG and FRA, two linked acts) | the Legal CELLAR adapter's own bounds; one page per query |
| Council of Europe | at most 20 declared CETS/ETS treaties, every state or organisation the chart lists (fixture: fictional CETS No. 990, 2098-2100) | two pages per treaty; charts are small |
| Periods | whatever the declared pages cover | as-of answers use the published dates only |

Offline fixtures (`tests/fixtures/treaties/`) are authored in each provider's
documented page or result shape for fictional treaties, participants' actions
and texts; nothing in them is live coverage.

## LIVE_VERIFICATION

| Source | Status | Evidence |
| --- | --- | --- |
| `untc-multilateral-status` | `declined` | not acquired: written permission required |
| `cellar-eu-international-agreements` | `unverified-live` | fixtures only; a dated live run is TR13 (#2645) |
| `coe-treaty-office-charts` | `unverified-live` | fixtures only; a dated live run is TR13 (#2645) |
