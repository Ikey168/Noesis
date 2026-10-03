# Government records and transparency: source-contract audit and bounded coverage (GT01)

Tracking: wave 2 tracker #2736 · recorded 2026-10-03.

No per-track tracker or delivery issue exists yet. They are opened once an
audit names a surviving source; this audit names three, subject to the live
check.

This audit sets out, per source, what the Political pack's proposed
`political.transparency` provider (subdomain `government-transparency`,
ADR-005) may acquire, how, and on what terms. **It was written without network
access: the publishers' hosts (`search.dip.bundestag.de`,
`questions-statements-api.parliament.uk`, `fragdenstaat.de`) were blocked by
this runtime's egress proxy (HTTP 403 on CONNECT, verified 2026-10-03), so
terms, endpoints, parameters and field names were not re-verified live.** They
come from the roadmap's candidate list and the publishers' documentation as the
author knows it. Every item marked _verify_ must be checked against the live
documentation, the live terms and a real response before the first dated live
run (the track's "Validate live coverage" issue, not yet opened). No source is
`live` until that run exists.

**The machine-readable copy does not exist yet.** `PROVIDER_CONTRACTS`,
`BOUNDED_COVERAGE`, `MINIMISATION` and `LIVE_VERIFICATION` will be added in
`src/ingestion/government_transparency_sources.py` by the track's acquisition
issues and must match this audit; a change to either needs the other changed
in the same pull request.

Non-goals for every source: no transparency, responsiveness or openness
scores for a government, ministry or public body; no rating of answer quality
or of a member's activity; no refusal or success rates presented as a
judgement (counts of stored records state their selection and window); no
summary presented as a question's or answer's meaning; no reading of a
question as evidence of the fact it asks about. Records are kept as each
publisher published them.

## Access decisions

| Source (proposed source-pack id) | Publisher | Delivers | Access | Decision |
| --- | --- | --- | --- | --- |
| `de-bundestag-dip-questions` | Deutscher Bundestag, Dokumentations- und Informationssystem für Parlamentsmaterialien (DIP) | printed papers (Drucksachen) of type Kleine Anfrage, Große Anfrage, Antwort and the weekly Schriftliche Fragen collections, with their Vorgang (procedure) references, authors, originating parliamentary groups and answering ministry (Ressort) | DIP API v1 JSON (`/drucksache`, `/drucksache-text`, `/vorgang`), API key | `unverified-live` |
| `uk-parliament-written-questions` | UK Parliament, Written questions, answers and statements API | Commons and Lords written questions with asking member, answering body, holding and substantive answers, corrections and withdrawals | anonymous JSON API | `unverified-live` |
| `fragdenstaat-requests` | Open Knowledge Foundation Deutschland, FragDenStaat | freedom-of-information requests made through the platform: public body, law invoked, dates, status and resolution as the platform states them; the public-body directory entry | anonymous JSON API v1 (`/api/v1/request/`, `/api/v1/publicbody/`) | `unverified-live` (terms are the most uncertain, see below) |

Documented, not acquired: DIP Plenarprotokolle and Bundesrat papers (acquired
for dossiers by the existing `de-bundestag-dip` source of the Political pack,
not re-acquired here); the DIP `/person` endpoint beyond id, name and role
(biographical data is not needed); UK written ministerial statements
(`/api/writtenstatements/statements`, _verify_), declared as the next UK
document; FragDenStaat message bodies and attachments (see minimisation);
WhatDoTheyKnow (UK FOI platform; not audited here, a later version of this
audit). No candidate was found whose known terms forbid the intended use (store
records with citations, show them side by side, export cited evidence
bundles). Should the live check find otherwise, the source moves to `blocked`
in `LIVE_VERIFICATION` and queries report it as unavailable rather than empty.

FragDenStaat is not an official publisher. It is a civil-society platform on
which private persons send requests and publish the replies. Its statuses and
resolutions are set by the requester or the platform, not by the public body,
and every stored value says so (`stated_by: platform`).

## Per-source contract

| Source | Endpoints | Authentication and keys | Licence and redistribution | Rate limits | Revision model: updates, corrections, removals |
| --- | --- | --- | --- | --- | --- |
| DIP | `https://search.dip.bundestag.de/api/v1/drucksache?f.drucksachetyp=Kleine%20Anfrage&f.ressort=...&f.datum.start=...&f.datum.end=...&f.wahlperiode=...`; `/drucksache/{id}`; `/drucksache-text/{id}`; `/vorgang/{id}`; cursor paging (`cursor`, `numFound`, `documents[]`) (_verify_ every filter name, the `f.ressort` filter in particular, and the page size) | API key, the same required secret as the existing `de-bundestag-dip` source: `NOESIS_BUNDESTAG_API_KEY`, sent as `Authorization: ApiKey ...` rather than the `apikey` query parameter (_verify_ header support), so it never appears in a URL, receipt or record. DIP publishes a public key with an expiry date and issues personal keys on request (_verify_ current policy) | Bundestag open-data terms (`bundestag-open-data`, attribution required, as in `config/source_packs/political.json`); Drucksachen are official works under § 5 UrhG (_verify_). Attribution: "Quelle: Deutscher Bundestag, DIP" (_verify_ wording) | concurrent-request limit documented by DIP (_verify_ the number); one request at a time, at most the declared pages per unit | each document carries `aktualisiert` (_verify_); a changed document is a new revision of the record keyed by DIP `id` and `dokumentnummer`; a corrected reprint (Neudruck, Berichtigung) is stored as DIP states it, as a revision or as its own Drucksache (_verify_ how DIP models it); an answer is its own Drucksache linked to the question only through the shared Vorgang (`vorgangsbezug`); a document no longer returned for the same declared selection is a `not-returned` observation, never a deletion |
| UK written questions | `https://questions-statements-api.parliament.uk/api/writtenquestions/questions?tabledWhenFrom=...&tabledWhenTo=...&answeringBodies=...&house=Commons&answered=Any&expandMember=true&skip=...&take=...`; `/api/writtenquestions/questions/{id}` (_verify_ parameter casing and `take` maximum) | none; nothing secret is sent or stored | Open Parliament Licence v3.0 with the statement "Contains Parliamentary information licensed under the Open Parliament Licence v3.0." (the existing `OGL_ATTRIBUTION` of the legislation sources) | undocumented (_verify_); `skip`/`take` pages of 100, at most the declared pages per unit; a longer list is `budget_exhausted`, never truncated | a question keeps its `id` and `uin`; a holding answer followed by a substantive answer, a corrected answer (`answerIsCorrection`, `originalAnswerText`, `dateAnswerCorrected`) and a withdrawal (`isWithdrawn`) are each new revisions, the earlier answer text kept (_verify_ all field names); the API carries no update stamp (_verify_), so the payload digest decides whether a revision is new; an unchanged re-acquisition adds nothing |
| FragDenStaat | `https://fragdenstaat.de/api/v1/request/?public_body={id}&...` (filter for creation date _verify_), `/api/v1/request/{id}/`, `/api/v1/publicbody/{id}/`, `/api/v1/law/{id}/`; `limit`/`offset` paging with `meta.next` (_verify_) | none for reading public requests; the OAuth write API is not used | FragDenStaat terms of use and the licence of request metadata and of the public-body database (_verify_ both; the author does not know them with certainty). Attribution to FragDenStaat and a link to each request page. If the live terms do not allow storing metadata, the source moves to `blocked` | undocumented (_verify_); at most the declared pages per unit; HTTP 429 is `rate_limited` | requests change status (`awaiting_response`, `resolved`, `asleep`, ...) and resolution (`successful`, `partially_successful`, `refused`, `not_held`, ...) over time (_verify_ vocabularies); each change is a new revision keyed by request `id`; a request made non-public or removed by the platform is a `not-returned` observation and its stored revisions are reduced to the minimised fields already held; nothing is inferred from an absence |

**Unavailable-access fallback.** A failed unit (HTTP error, an expired or
missing DIP key, a redirect to another host, schema drift, a response over its
budget) fails that source's run with its code and a receipt; earlier revisions
stay current and nothing is marked removed or revised because of a failure.
Without `NOESIS_BUNDESTAG_API_KEY` the DIP source fails with
`authentication_failed` and readiness reports it as `unavailable` with the
reason; it never silently returns nothing.

## Definitions recorded per record

- **Question kind as the parliament names it:** DIP `drucksachetyp` (Kleine
  Anfrage, Große Anfrage, Antwort, Schriftliche Fragen) and UK `house` plus
  named-day flag (`isNamedDay`, _verify_). Kinds are never mapped onto one
  vocabulary; a cross-parliament view shows both labels.
- **Answering body:** DIP `ressort` with its `federfuehrend` flag; UK
  `answeringBodyId` and `answeringBodyName` as published. Department identity
  across sources is a reviewable assertion, never a name match.
- **FOI request:** the law invoked (IFG, UIG, VIG or a Land law, as the
  platform states it), jurisdiction, due date, status and resolution, each
  labelled `stated_by: platform`.

## Data minimisation decision

Members of the Bundestag, Members of Parliament and peers asking or answering
questions, and the ministers or ministries answering them, are public office
holders acting in office. FragDenStaat requesters are private persons. Policy
`government-transparency-minimisation-v1`, enforced in the parser and again at
write time (`minimisation_violation` before anything is written):

- **Stored:** for questions and answers, the document identifiers, dates,
  kind, title, question and answer text as published (subject to the caps
  below), the answering body, and each asking or answering member as the
  parliament's own member id, display name and role (`autoren_anzeige`, UK
  `askingMemberId`/`answeringMemberId`, _verify_), with the published
  registered-interest flag (`memberHasInterest`, _verify_) kept verbatim. For
  FOI requests: request id and URL, title as published (capped at 500
  characters, labelled requester-authored), public body id and name, law,
  jurisdiction, dates, status and resolution, costs as published.
- **Redacted at acquisition:** the FragDenStaat `user` field and any requester
  name or username (requests are stored as from "a requester"); the
  `description` (request text written by a private person).
- **Excluded:** FragDenStaat message bodies, attachments and the names of
  officials appearing in replies (the request page is linked instead); DIP
  person biographies; UK member contact details and constituency addresses;
  attachment files of UK answers (locators only, _verify_ `attachments` field).
- **Retention:** revisions are retained with their source run for provenance.
  Office holders are stored as acting in office; nothing about a private
  person is stored, so no erasure workflow applies to stored fields. If a
  source later withholds something it published (a redacted answer, a request
  made non-public), the next acquisition records that as a revision and the
  earlier revisions keep only the fields still allowed.
- **Identity:** members are external identifiers
  (`legislation:member:uk-parliament:<id>`, as in `src/kb/legislation_identity.py`;
  `dip-person:<id>` for DIP), proposed to existing political identities only
  through the reviewable state machine; nothing is merged by name.
- **Who may query:** principals with `knowledge:political:transparency:read`
  and access to the namespace; writes need
  `knowledge:political:transparency:write`, link and identity reviews
  `knowledge:political:transparency:review`.

## Record shapes and reuse

Shapes from `packs/taxonomy.json`: **versioned-documents** (questions and
answers, with holding, substantive and corrected answers as revisions) and
**events-notices** (FOI requests with their status revisions). No new shape.

Reuse, not duplication: the declarative connector
`src/ingestion/connectors/political_official.py` and its catalog
`config/political_sources.json` (the `de-bundestag-dip` entry and its key);
revisions through `src/ingestion/document_store.py` as
`src/kb/legislation.py` does; member identity through
`src/kb/legislation_identity.py` and `src/kb/elections_identity.py` on
`src/kb/ownership_identity.py` and `src/kb/entity_history.py`; legislation
links only by a source-stated reference (a DIP `vorgangsbezug` id or a
Drucksache number committed in a dossier of
`src/domains/political/legislative_dossiers.py`, the rule
`src/kb/lobbying_links.py` applies); UK questions name no bill (_verify_), so
any bill link is a reviewer-accepted candidate. Public-finance links
(`economics.public-finance`) are reviewed assertions only, never topic
matching. Monitoring follows `src/kb/legislation_monitoring.py` on
`src/kb/subscriptions.py`.

## Bounded first coverage

| Source | Selection | Window | Caps |
| --- | --- | --- | --- |
| DIP | Kleine Anfragen answered by one declared Ressort (Bundesministerium der Finanzen), their Antworten and Vorgänge; one Schriftliche Fragen collection | one declared 92-day window in one Wahlperiode | 20 Vorgänge, 2 pages per list, text at most 300 KB per Drucksache |
| UK written questions | Commons questions to one declared answering body (HM Treasury, id _verify_) | one declared 31-day tabling window | 200 questions (2 pages of 100), answer text at most 100 KB per answer |
| FragDenStaat | requests to the same federal ministry and its public-body entry | one declared 92-day creation window | 100 requests (2 pages of 50) |

A Drucksache text or an answer over its cap is not stored at all: the record
keeps the locator and content hash and states `text: not-retained (over cap)`,
never a truncated text. Justification: one German and one UK finance ministry
show both parliaments' question-and-answer journeys and the FOI path to the
same German body, and give the public-finance links a subject. Every further
body, window or document type is a source-pack version bump.

## Live verification

| Source | Status | Checked | Evidence |
| --- | --- | --- | --- |
| DIP questions | `unverified-live` | - | none; offline fixtures not yet authored |
| UK written questions | `unverified-live` | - | none; offline fixtures not yet authored |
| FragDenStaat | `unverified-live` | - | none; offline fixtures not yet authored |

The fixtures, once written, will be authored, not captured: fictional members,
bodies and requesters, tabling dates in 2094-2097 and update stamps in
2098-2099, so nothing can be mistaken for a published record.
