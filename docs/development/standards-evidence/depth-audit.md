# Standards and certification: second-publisher depth audit (DS01)

Tracking: [#2828](https://github.com/Ikey168/Noesis/issues/2828) DS01 (wave 4
tracker [#2825](https://github.com/Ikey168/Noesis/issues/2825)) · decision
[ADR-006](../../architecture/decisions/ADR-006-source-depth.md) · plan
[source depth program](../../roadmaps/source-depth-program.md) · recorded
2026-11-01.

`standards-certification` is thin: its only provider, `technology.standards`,
reads one publisher, ISO (`iso-open-data` in `technical-software-knowledge`
1.2.0, connector `iso-open-data`, adapter `src/ingestion/standards_sources.py`,
record contract `noesis-standard-catalogue-v1`, store `src/kb/standards.py`).
Certificates enter only through the explicit import path
(`noesis-certificate-record-v1`) until a certification registry is validated.

**It was written without network access.** The publisher hosts
(`webgate.ec.europa.eu`, `ec.europa.eu`, `www.rfc-editor.org`, `api.w3.org`,
`www.w3.org`, `www.etsi.org`, `portal.etsi.org`) are blocked by this runtime's
egress proxy (verified 2026-11-01). Terms, endpoints, key requirements and rate
limits come from the publishers' documentation as the author knows it. Every
item marked _verify_ must be checked against the live pages, the live terms and
a real response before the first dated live run. None of the sources below is
live; none counts toward live depth (ADR-006, decision 6).

## No protected standard text is stored

Confirmed for the existing ISO source from the code: `IsoOpenDataAdapter`
reads only the ISO deliverables-metadata JSONL export and keeps reference,
edition, stage and derived status, ICS codes, committee, titles, the published
scope abstract, replaces/replaced-by, languages and the catalogue page URL; every
record carries `content_access: "protected-link-only"` (a constant in the
contract), and the adapter never requests a standard document. The Technical
model mirror (`record_object` in `src/kb/standards.py`) stores the same metadata
and `source_url` = catalogue link.

Every candidate below keeps the same rule: **catalogue metadata and links only,
never the text of a standard, specification, RFC or deliverable**, even where
the publisher distributes the text freely (W3C, IETF, ETSI). Free text is still
not needed for a catalogue answer, and storing it would change the licence
analysis of every evidence bundle. The existing `rfc-editor` web source in the
technical knowledge base is a separate source with its own purpose and is not
reused for this provider.

## Summary

| Candidate | Publisher | Registry entry | Independent of ISO | Shape fit | Decision |
| --- | --- | --- | --- | --- | --- |
| W3C Technical Reports API | World Wide Web Consortium | new (`w3c`) | yes (own process; some W3C specifications are later transposed as ISO/IEC standards, which are separate catalogue records) | fits `noesis-standard-catalogue` with a provider widening | **recommended** for `technology.standards` |
| RFC Editor (IETF) RFC index | RFC Editor | existing (`rfc-editor`) | yes | fits with a status mapping; RFCs have no editions | **acceptable alternative**; implement if W3C fails verification |
| European Commission NANDO | European Commission (DG GROW) | existing (`european-commission`) | yes | does not fit: notified bodies are neither standards nor certificates | `not-implemented`: new record shape needs a decision record, and no documented machine interface |
| ETSI deliverables | ETSI | new (`etsi`) | yes | would fit the catalogue shape | `not-implemented`: no documented machine catalogue interface (as `standards_sources.py` already records) |

## Per-candidate contract

### W3C Technical Reports API

| Item | Record |
| --- | --- |
| Endpoint | W3C API, `https://api.w3.org/specifications` and `/specifications/{shortname}/versions`, JSON (HAL), with `supersedes`, `superseded-by` and per-version `status` and `date` (_verify_ paths and fields); the TR listing `https://www.w3.org/TR/` is the human view |
| Access | no key required for read access (_verify_: the API historically took an `apikey` parameter). If a key is required, proposed variable name `NOESIS_W3C_API_KEY` |
| Rate limits | a per-IP request limit (_verify_ the figure); the source budget bounds every run |
| Licence | W3C API data: reuse terms on the W3C site (_verify_; the author knows of no restriction on the metadata); W3C specification text is under the W3C Document License or CC BY 4.0 depending on date (_verify_), but text is not stored |
| Redistribution | metadata with attribution and a link to the specification (_verify_) |
| Revision model | each dated version of a specification is immutable at its `/TR/YYYY/...` URL; the latest-version pointer moves; status changes (WD, CR, PR, REC, NOTE, Superseded, Obsolete, Rescinded, Discontinued) are new versions or new status assertions |

Fit with `noesis-standard-catalogue-v1` and `src/kb/standards.py`:

- **Holds.** `native_id` = shortname plus dated version, `reference` = the
  specification's title and maturity as W3C states it (_decide in delivery_),
  `catalogue_url` = the dated `/TR/` URL, `replaces`/`replaced_by` from
  `supersedes`/`superseded-by`. Status maps by a declared table: REC →
  `published`; FPWD, WD, CR, CRD, PR → `under-development`; Superseded,
  Obsolete, Rescinded, Discontinued → `withdrawn`; anything else → `unknown`
  (_verify_ the status vocabulary). Revisions are already keyed by provider
  (`standard_current` primary key `namespace, provider, native_id`).
- **Does not yet hold.** (a) The contract fixes `provider` to `iso-open-data`.
  (b) `content_access` is the constant `protected-link-only`; for W3C the text
  is open but still not stored, so the contract needs a second value
  (e.g. `open-link-only`) rather than a false "protected". (c) The Technical
  model mirror hard-codes object ids `standard:iso:<id>` and ISO's
  supplement-type amendment logic; it must take a provider prefix.
- **Judgement.** A provider widening of the same catalogue shape, not a new
  shape; the delivery issue bumps the contract version. No decision record is
  needed under ADR-006 decision 7 unless review judges otherwise.
- **Transpositions.** A W3C specification also published as an ISO/IEC
  standard (for example WCAG 2.0 as ISO/IEC 40500, _verify_) gives two catalogue
  records from two publishers for one text. They stay two records; a link
  between them is a reviewed assertion with the publisher's stated basis, never
  inferred from titles. The pair is one origin for the text and does not raise
  any corroboration count.

### RFC Editor (IETF): RFC index

| Item | Record |
| --- | --- |
| Endpoint | `https://www.rfc-editor.org/rfc-index.xml` (one XML index of every RFC with number, title, date, current status, publication status, stream, obsoletes/obsoleted-by, updates/updated-by, STD/BCP/FYI membership, DOI) (_verify_ elements); per-RFC JSON metadata `https://www.rfc-editor.org/rfc/rfcNNNN.json` (_verify_) |
| Access | none |
| Rate limits | none published (_verify_); one index document per run, within the byte budget |
| Licence | IETF Trust Legal Provisions (the existing `rfc-editor` source records `ietf-trust-terms`, "terms-governed"); index metadata reuse is not restricted as far as the author knows (_verify_) |
| Redistribution | metadata with a link; RFC text is not stored here |
| Revision model | an RFC never changes; status changes (for example Proposed Standard → Internet Standard, or Historic) and obsoleted-by/updated-by appear in later index versions, each a new revision |

Fit: holds with the same provider widening as W3C. `native_id` = `RFC NNNN`,
`replaces`/`replaced_by` from obsoletes/obsoleted-by; "updates/updated-by" maps
to the Technical model's `amends` relation (_decide in delivery_). Status maps:
Internet Standard, Proposed Standard, Draft Standard, Best Current Practice,
Informational, Experimental → `published`; Historic or obsoleted → `withdrawn`
only where the index states it; Unknown → `unknown`. The maturity level is kept
verbatim beside the mapped status. RFCs have no editions; an STD number names a
set of RFCs and is kept as a membership, not as an edition. The fit is weaker
than W3C's, which is why it is the alternative.

Registry: `rfc-editor` is already registered in `packs/depth.json` (`match`:
`RFC Editor`), so a new source id (proposed `rfc-editor-index`) with publisher
`RFC Editor (RFC index)` resolves without a registry change.

### European Commission NANDO (notified bodies)

| Item | Record |
| --- | --- |
| Endpoint | NANDO in the Single Market Compliance Space, `https://webgate.ec.europa.eu/single-market-compliance-space/#/notified-bodies` (single-page application; the former `ec.europa.eu/growth/tools-databases/nando/` pages, _verify_ whether they still answer) |
| Access | interactive; no documented public API or bulk export known to the author (_verify_). Undocumented JSON calls behind the application are not a permitted interface and are not used |
| Licence | Commission reuse policy (Commission Decision 2011/833/EU; CC BY 4.0 under Decision C(2019) 1655 for Commission content, _verify_ that it covers NANDO) |
| Redistribution | with attribution (_verify_) |
| Content | notified and other conformity assessment bodies: NB number, name, country, legislation (EU harmonisation acts) under which each is notified, products, procedures/modules, notification status and dates; NANDO lists bodies, not certificates |

Fit: **does not fit.** A notification is neither a standard edition nor a
certificate (`noesis-certificate-record-v1` requires a certificate number,
issuer and status of a certificate). It is a third record type (conformity
assessment body and its notifications), so ADR-006 decision 7 requires a
decision record before any code. Registry: the publisher string would name the
European Commission, already registered as `european-commission` (distinct from
`eurostat` in the registry).

Decision: **`not-implemented`.** A new record shape needs a decision record
first, and no documented machine interface was established offline. NANDO stays
the first certification-side candidate for a later decision.

### ETSI deliverables

| Item | Record |
| --- | --- |
| Endpoint | ETSI deliverables search `https://www.etsi.org/standards-search` and the deliverables tree `https://www.etsi.org/deliver/` (PDF per deliverable); ETSI work programme `https://portal.etsi.org/` (_verify_ any export) |
| Access | interactive search; deliverable PDFs downloadable without charge (_verify_); no documented machine catalogue interface known to the author (_verify_) |
| Licence | ETSI copyright; free download for personal use, redistribution of text restricted (_verify_); metadata reuse terms not established (_verify_) |

Decision: **`not-implemented`**, unchanged from `PROVIDER_CONTRACTS["etsi"]` in
`src/ingestion/standards_sources.py`: no documented, permitted machine catalogue
interface. The search interface and the deliverables tree are never crawled for
the catalogue.

## Recommendation

1. **Implement the W3C Technical Reports API in `technology.standards`**:
   proposed source id `w3c-technical-reports`, publisher string
   `World Wide Web Consortium (Technical Reports)`, catalogue metadata and links
   only, bounded to pinned shortnames (for example a handful of
   accessibility and web-platform specifications) and a record ceiling as for
   ISO. Before the source: the `noesis-standard-catalogue` provider widening
   (provider value, `content_access` value for open-but-not-stored text,
   provider-prefixed Technical object ids).
2. If the W3C API fails verification (key, terms or interface), implement the
   **RFC Editor index** instead (proposed `rfc-editor-index`), with the same
   widening and the status table above.
3. In the same delivery change: register `w3c` in `packs/depth.json`
   (`match`: `World Wide Web Consortium`) (no registry change for RFC Editor), add
   the source to `providers.technology.standards.sources`, and remove the
   `standards-certification` row from the thin table. Note in the depth-two
   watchlist that both publishers are on the standards side; the certification
   side has no registry publisher yet.
4. NANDO: `not-implemented`; a decision record for a conformity-assessment-body
   record type is the prerequisite. ETSI: `not-implemented`.

Depth after the recommended delivery: offline 2 (ISO, W3C or RFC Editor), live 0
until the track's "Validate live coverage" run.

## Live verification

| Candidate | Status | Checked | Evidence |
| --- | --- | --- | --- |
| W3C Technical Reports API | `unverified-live` (proposed) | - | none; publisher host blocked 2026-11-01 |
| RFC Editor index | `unverified-live` (alternative) | - | none; publisher host blocked 2026-11-01 |
| NANDO | `not-implemented` | - | new record shape; no documented machine interface (_verify_) |
| ETSI deliverables | `not-implemented` | - | no documented machine catalogue interface (_verify_) |
