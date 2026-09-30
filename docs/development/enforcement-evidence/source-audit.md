# Regulatory enforcement: source-contract audit, minimisation decision and bounded coverage (EN01)

Tracking: #2651 · delivery issue #2655 · recorded 2026-09-30.

This audit sets out, per source, what the Legal pack's regulatory enforcement
features may acquire, how and on what terms. **It was written without network
access to the publishers: `www.sec.gov`, `www.fca.org.uk`, `echo.epa.gov` /
`echodata.epa.gov` and `www.edpb.europa.eu` were unreachable from the authoring
environment (egress blocked), so no terms page, rate-limit statement or
response shape was re-verified live.** Endpoints, fields and terms come from
the tracker's references and the publishers' documentation as the author knows
it. Every item marked _verify_ must be checked against the live pages, the live
terms and a real response before the first dated live run (EN14, #2720). No
source is `live` until that run exists.

The machine-readable copy of these decisions is `PROVIDER_CONTRACTS`,
`MINIMISATION`, `IDENTIFIERS`, `BOUNDED_COVERAGE`, `DECLINED` and
`LIVE_VERIFICATION` in `src/ingestion/enforcement_sources.py`. Each source
entry in `config/source_packs/legal.json` (`legal-research` 1.5.0, earlier
sources verbatim) states `enforcement.live_verification: unverified-live`, and
the MCP tool `enforcement_source_contracts` returns the same decisions.

Non-goals for every source: no risk or compliance scoring, no inference of
wrongdoing from an initiated action, no merging of settled "neither admit nor
deny" outcomes into findings, no profiling of named individuals, no legal
advice. Action types, statuses, outcomes and admission wording are kept **as
the regulator published them**.

## Access decisions

| Source (source-pack id) | Publisher | Delivers | Access | Decision |
| --- | --- | --- | --- | --- |
| `sec-enforcement-releases` | US Securities and Exchange Commission | litigation releases (`LR-nnnnn`) and administrative proceedings (`33-`, `34-`, `IA-` releases with `3-nnnnn` file numbers): respondents as named, charges and legal bases, sanctions, settlement wording, related civil actions | bounded page acquisition of declared release pages under `https://www.sec.gov/enforcement-litigation/...` (the Drupal field classes read are _verify_) | `unverified-live` |
| `fca-final-notices` | Financial Conduct Authority | final notices to firms: addressee, firm reference number (FRN), date, the penalty imposed, the penalty before the settlement discount, the Principles and Handbook rules cited, Upper Tribunal references, amendments | bounded acquisition of declared notice documents `https://www.fca.org.uk/publication/final-notices/{slug}.pdf`; the notice is a PDF whose text layer is parsed (layout _verify_); PDF text extraction uses the optional `pdfminer.six` dependency and reports `source_unavailable` when it is missing | `unverified-live` |
| `epa-echo-enforcement-cases` | US EPA, Enforcement and Compliance History Online | civil (and, where defendants are organisations, criminal) enforcement cases: statutes and sections, defendants, facilities with FRS registry ids and coordinates, penalties, SEP cost, cost recovery, compliance action cost, outcome, court docket | documented ECHO case web service `https://echodata.epa.gov/echo/case_rest_services.get_case_info?p_id={case}&output=JSON` (response keys _verify_), one declared case number per unit | `unverified-live` |
| `edpb-art60-final-decisions` | European Data Protection Board | register of Article 60 final decisions: lead and concerned supervisory authorities, GDPR provisions, corrective measures, fines, date, controller where published | bounded acquisition of declared register entry pages under `https://www.edpb.europa.eu/` (entry path and labels _verify_) | `unverified-live` |

## Per-source contract

| Source | Authentication and key handling | Licence and redistribution | Rate limits | Updates, corrections and removals |
| --- | --- | --- | --- | --- |
| SEC | no key; the SEC fair-access policy requires a declared User-Agent naming the operator and a contact address. It is read from `NOESIS_SEC_USER_AGENT` (a documented variable, not a secret); a live run without it is refused as `source_unavailable`, never sent anonymously | US government work, public domain (17 U.S.C. § 105); credit the SEC | fair access: at most 10 requests per second (_verify_); at most 20 declared releases per source per run | `article:modified_time` (or the page digest) is the page revision; a changed page is a new revision of the action and its notice, respondent and penalty records; a 404/410 for a declared release is a `removed_by_source` revision |
| FCA | none | FCA website terms: material may be reproduced free of charge with acknowledgement, accurately and not in a misleading context (_verify_; this is **not** the Open Government Licence) | none published (_verify_); at most 20 declared notices per run | the notice digest is the revision; "This Final Notice was amended on ..." makes the notice and action revisions `corrected`, with the amendment date and wording; a withdrawn notice (404/410) is `removed_by_source` |
| EPA ECHO | none | US government work, public domain; ECHO data disclaimer (_verify_) | no published quota; ECHO asks clients not to poll in bulk (_verify_); at most 20 declared cases per run | `DataRefreshDate` is the revision stamp; ECHO refreshes weekly; a changed case (status, settlement, penalties) is a new revision; penalty fields that are absent are `not_published` records, later filled as revisions |
| EDPB | none | EDPB legal notice: reuse with acknowledgement (Decision 2011/833/EU applied by the EDPB; _verify_) | none published (_verify_); at most 20 declared entries per run | the entry digest is the revision; a withdrawn entry (404/410) is a `removed_by_source` revision |

**Unavailable-access fallback.** A failed unit (HTTP error other than 404/410,
redirect to another host, schema drift, missing optional PDF extractor,
missing SEC User-Agent) fails the run for that source with its code; earlier
revisions stay current and nothing is marked decided, corrected or removed
because of a failure.

**Gated or declined.** The FCA Financial Services Register API needs a
registered key; it is declined, and FRNs are taken as the notice publishes them.
SEC trading suspensions (not actions against a respondent), ECHO bulk downloads
(outside a bounded selection) and national supervisory authorities' own
registers (outside the first bounded coverage) are documented in `DECLINED` and
not acquired. No source was found whose terms forbid the intended use, so no
"not implemented" provider contract is needed.

## Data-minimisation decision (named individuals)

The sources name individuals: SEC releases charge officers alongside firms,
ECHO lists individual defendants, FCA notices mention staff, and EDPB entries
are sometimes about sole traders.

* **Stored.** Organisational respondents only: name and role as published and
  their published identifiers (CIK, FRN, LEI, company number); the **count** of
  individual respondents on each action; facility names, FRS ids and published
  coordinates (organisations' sites).
* **Redacted.** An individual's published name (and surname, with or without
  an honorific) in a stored title or caption is replaced by `[individual]`.
  Sentences about an individual - their charges, outcome or sanction - are not
  stored at all.
* **Excluded.** Individuals' names, roles, dates of birth, addresses,
  nationalities and personal registration numbers (IRN, CRD); releases and
  cases whose only respondents are individuals; FCA notices addressed to
  individuals; the controller of an EDPB entry when it is a natural person (the
  entry is kept, the controller is only counted).
* **Enforced at write time.** `validate_record` rejects a `respondent` that is
  not an organisation and any record carrying a personal attribute
  (`date_of_birth`, `home_address`, `nationality`, ...); the store validates
  every record before writing.
* **Retention.** Revisions are kept as the audit trail of what the regulator
  published. An action the source removes becomes a `removed_by_source`
  revision; answers leave it out unless history is requested.
* **Who may query.** Any principal with `knowledge:legal:read` and namespace
  read access. Nothing personal is stored, so no further restriction applies.
* **Matching.** Individuals are never offered to identity matching (EN07).

## Stable native identifiers (stored as published)

| Source | Identifiers |
| --- | --- |
| SEC | release number (the action key), file number, respondent CIK where the page publishes it, federal civil action number (citation only) |
| FCA | notice slug (the action key), firm reference number (FRN), Upper Tribunal reference (appeal record) |
| EPA ECHO | ECHO case number (the action key), facility FRS registry id, federal civil action number (citation only) |
| EDPB | register entry slug (the action key), register number, lead supervisory authority (the action's authority, `eu-dpa-<country>`) |

## Bounded first coverage

* **Seed.** Companies and groups already acquired by the Corporate Ownership
  sources; in the fixtures the fictional Exampla and Northwind groups. Actions
  are declared by release number, notice slug, case number or register entry,
  never crawled.
* **Window.** Actions published from 2020-01-01; the history of an in-window
  action is kept.
* **Caps.** At most 20 declared units per source per run; 50 records per unit.
* **Respondents.** Actions with at least one organisational respondent.

Justification: the tracker's journey is company-to-actions; declaring units
from companies already in the ownership graph keeps acquisition proportionate,
reproducible and free of bulk personal data.

## LIVE_VERIFICATION

| Provider | Status | Outstanding |
| --- | --- | --- |
| `us-sec` | `unverified-live` | page field classes, fair-access limit, User-Agent acceptance |
| `uk-fca` | `unverified-live` | PDF text layout, website reuse terms |
| `us-epa-echo` | `unverified-live` | `get_case_info` response keys, disclaimer |
| `edpb-art60` | `unverified-live` | entry path and labels, reuse terms |

A dated live run from this runtime belongs to EN14 (#2720) and is recorded in
this directory, separately from the offline evidence, when it exists.
