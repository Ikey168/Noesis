# Treaties and international law: source-contract audit, minimisation decision and bounded coverage (TR01)

Tracking: #2581 · delivery issue #2586 · recorded 2026-09-30.

This audit sets out, per source, what the Legal pack's `legal.treaties`
provider may acquire, how, and on what terms. **It was written without network
access: the sources' terms and documentation pages were not re-read live for
this audit.** Endpoints, element structures, fields and terms come from the
sources' public documentation as the author knows it and from the issue's
references. **Every item marked _verify_ must be checked against the live
documentation, the live terms and a real response before the first dated live
run (TR13, #2645). No source is `live` until that run exists.** The
machine-readable copy of these decisions is `PROVIDER_CONTRACTS`,
`MINIMISATION`, `BOUNDED_COVERAGE` and `LIVE_VERIFICATION` in
`src/ingestion/treaties_sources.py`; each source entry in
`config/source_packs/legal.json` (`legal-research` 1.5.0, earlier sources
verbatim) states `treaties.live_verification: unverified-live` and the
minimisation policy `treaties-minimisation-v1`, and the MCP tool
`treaties_source_contracts` returns the same decisions.

Non-goals for every source: no legal advice, no inference of obligations or
compliance, no interpretation of the legal effect of reservations, and no
treaty-text redistribution beyond what each source licenses (texts are
referenced by link, never mirrored).

## Access decisions

| Source (source-pack id) | Provider | Delivers | Decision | Reason |
| --- | --- | --- | --- | --- |
| `untc-treaty-status` | UN Treaty Collection status pages (`treaties.un.org`) | treaty header, participants, signatures and consents to be bound, declarations, reservations, objections, notes | `unverified-live` | No documented API; the status page is public HTML. The parser reads the element structure recorded in the authored fixture (`treaty-header`, `treaty-info`, `participants`, `declarations`, `objections`, `notes`); the live page's element ids and date formats are _verify_ |
| `eu-cellar-agreements` | CELLAR SPARQL endpoint (Publications Office) | agreement work, language expressions, dates as stated, contracting parties, EU acts pointing at the agreement | `unverified-live` | The work/expression query is the Legal pack's reviewed CELLAR query (prior live evidence 2026-09-09, `docs/development/workflow-review-evidence/cellar-native-2026-09-09.json`). The agreement-date, contracting-party and act-relation predicates are _verify_ |
| `coe-treaty-office` | Council of Europe Treaty Office (`www.coe.int/conventions`) | chart of signatures and ratifications, entry into force per state, denunciations, reservations, declarations and objections | `unverified-live` | No documented API; public HTML chart and declarations list. Element structure and query parameters are _verify_ |

No candidate source is recorded as "not implemented": the three sources'
public-status information may be reproduced with attribution for the intended
non-commercial research use, subject to the _verify_ items below. The UNTS
full-text database (treaty texts) is **documented, not acquired**: texts are
referenced by their published link only.

## Per-source contract

| Source | Endpoints | Authentication and keys | Rate limits and bounds | Identifiers (stored as published) | Updates, corrections and removals |
| --- | --- | --- | --- | --- | --- |
| UNTC | `/Pages/ViewDetails.aspx?src=TREATY&mtdsg_no={chapter}-{number}&chapter={n}&clang=_en` | none; no key is issued or handled | none documented (_verify_); one page per declared treaty, sequential, at most 20 treaties, 250 participants and 1,000 statements per page (larger is `budget_exhausted`, never truncated) | UNTC chapter and number (`mtdsg_no`), UNTS registration number, depositary notification (C.N.) references in notes | The page states "Status as at" (date and time): that stamp is the depositary revision. A changed row or statement is a new revision naming its predecessor; a corrected date is a revision (the depositary's correction note is kept verbatim); a row the page no longer shows is a `removed-by-source` revision, never a deletion |
| CELLAR | `https://publications.europa.eu/webapi/rdf/sparql` (two bounded queries per agreement) | none | fair use; queries time out after 60 s (_verify_); at most 20 agreements, three expressions (ENG, FRA, DEU) | CELEX (sector 2), ELI where published, CELLAR work/expression/manifestation/item URIs, authority-table country codes for contracting parties | No per-work revision stamp is queried; each acquisition with different bindings is a new revision. Corrigenda are separate works linked by an explicit CDM triple |
| Council of Europe | `/en/web/conventions/full-list?module=signatures-by-treaty&treatynum={cets}`, `...?module=declarations-by-treaty&numSte={cets}&codeNature=0` | none | none documented (_verify_); two pages per declared treaty, sequential, at most 20 treaties | CETS/ETS number, state name, member or non-member as published | The chart states "Status as of" (date): the depositary revision. Denunciations and withdrawals are actions with their dates, never deletions; a row no longer shown is a `removed-by-source` revision |

**Licences and attribution.**
UNTC: UN website terms of use allow reproduction of content for
non-commercial purposes with attribution (_verify_ the current wording and any
commercial-use clause); attribution `UN_ATTRIBUTION`.
CELLAR: Commission Decision 2011/833/EU authorises reuse with acknowledgement
of the source; attribution `EU_ATTRIBUTION`.
Council of Europe: website terms allow reproduction with the source cited,
not for commercial purposes without permission (_verify_); attribution
`COE_ATTRIBUTION`.
Treaty texts (UNTS PDFs, official CoE texts, EUR-Lex items) are **linked,
never mirrored** by any source.

**Unavailable-access fallback.** A failed unit (HTTP error, redirect to
another host, schema drift, a page longer than its bound) fails the run for
that source with its code; earlier revisions stay current and nothing is
marked withdrawn, corrected or in force because of a failure.

## Minimisation decision

Treaty sources are about states and organisations, but some published text
names people: signatories and plenipotentiaries appear in treaty texts and
depositary notifications, and Council of Europe declarations designating a
national authority can carry an official's contact details. The decision,
enforced both at parsing (`src/ingestion/treaties_sources.py`) and at write
time (`src/kb/treaties_records.py`, `minimisation_violation`):

* **Subjects.** Participants are states, international organisations and the
  EU as each source names them. No natural person is ever a participant, a
  key, an identity candidate, a link or a monitor target.
* **Stored.** Participant name as published, codes the source publishes,
  action type and dates as published, verbatim statement text (the
  depositary's publication of a state's act), anchors and footnotes.
* **Never stored.** Signatory, plenipotentiary or representative names as
  separate fields (`signatory`, `representative`, `person`, `official_name`,
  ...), contact details as fields (`contact`, `email`, `telephone`, ...), and
  any per-person index or profile. A record carrying such a field is refused.
* **Redacted.** Telephone numbers and e-mail addresses inside a verbatim
  statement are replaced by `[contact details withheld: TR01]` before a record
  exists; the statement records `contact_details_withheld: true`. The rest of
  the statement stays verbatim.
* **Retention.** Retained with the treaty record; revisions are immutable and
  superseded, never purged. No personal identifier is stored as a field, so
  nothing personal remains to purge.
* **Who may query.** Callers with the Legal read scope
  (`knowledge:legal:read`) and namespace access. Statements are returned only
  in treaty and participant answers and are never searchable by a person's
  name.

## Bounded first coverage

| Source | Selection | Caps | Justification |
| --- | --- | --- | --- |
| UNTC | treaties named by `mtdsg_no` and chapter; first live selection: named treaties of chapters IV (human rights), XXVII (environment) and X (trade), chosen for their links to Sanctions, Legislation and Trade flows | 20 treaties, 250 participants, 1,000 statements per treaty | status pages are one request per treaty; the chapters are those the Legal, Sanctions and Trade flows packs already cite |
| CELLAR | EU agreements named by sector-2 CELEX | 20 agreements, 3 expressions each | the EU agreements that conclude or implement a selected UNTC or CoE treaty |
| Council of Europe | treaties named by CETS/ETS number | 20 treaties, all states listed | the conventions the EU has signed or concluded, for cross-source matching |
| Period | the status as published at acquisition time | - | earlier statuses only as revisions this runtime has itself observed; no back-filled history |

The pinned fixtures (`tests/fixtures/source_packs/legal-treaties-*.json`)
select one fictional treaty per source (`XXVII-99`, CELEX `22090A0510(01)`,
CETS `999`); the live selection replaces them in TR13.

## LIVE_VERIFICATION

All three providers are `unverified-live`. Before TR13 marks a provider
`verified-live`, a dated bounded run must confirm: the element ids and date
formats of the UNTC status page and the CoE chart and declarations list; the
CELLAR predicates `resource_legal_date_signature`,
`resource_legal_date_entry-into-force`,
`agreement_international_has_contracting_party` and the act relations
`resource_legal_based_on_resource_legal` and `work_cites_work`; the licence
wording above; and that a replayed page adds no revision.

## Record model

`noesis-treaty-record-v2` (`contracts/schemas/jsonschema/noesis-treaty-record-v2.json`):
`treaty` (identifiers, title as published, adoption, entry-into-force
conditions and date, registration, depositary status stamp, text links,
footnotes, cross-references, citations), `treaty-expression` (one language
version), `participant`, `treaty-action` (participant as published, action
type and heading as published, action date, deposit date, effective date) and
`treaty-statement` (reservation, declaration, objection, communication or
note, verbatim with its anchor, linked to the action and to the objected
statement where the source links it). Stored by `src/kb/treaties_records.py`
with source, record revision and as-of time on every revision.
