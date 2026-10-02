# Legal treaties and international agreements

The Legal pack's `legal.treaties` provider (#2581) answers, with citations:

* given a **treaty and a participant** (a state, an international
  organisation or the EU) and a date, which actions the depositary had
  published - signature, consent to be bound, entry into force, denunciation or
  withdrawal - with the reservations, declarations, objections and notes
  linked to them, verbatim;
* given a **participant**, its treaty actions over a period across treaties;
* given a **treaty**, its reservations, declarations and objections, each
  objection linked to the reservation it objects to where the source links it.

Every answer cites the record revision, the depositary's revision stamp
("Status as at" / "Status as of") and the as-of time of each record. Nothing
here gives legal advice, infers obligations or compliance, interprets the legal
effect of a reservation or redistributes treaty text (texts are linked).

UN Treaty Collection, EU agreement and Council of Europe coverage are three
independent, default-off features of the Legal composition: `treaties-untc`,
`treaties-eu` and `treaties-coe`. Legislation, Sanctions and Trade flows links
degrade to reported `provider-missing` / `target-missing` links when those
providers are absent. Sources, terms and the minimisation decision:
[source audit](../development/treaties-evidence/source-audit.md).

## Sources (`legal-research` 1.5.0, connector `treaties`)

| Source | Provider | Delivers |
| --- | --- | --- |
| `untc-treaty-status` | UN Treaty Collection status pages | treaty header, participants, signatures and consents (column heading and suffix verbatim), reservations, declarations, objections and notes with anchors |
| `eu-cellar-agreements` | CELLAR SPARQL (Legal pack CELLAR query) | agreement by CELEX/ELI, language expressions, dates as CELLAR states them, contracting parties, EU acts pointing at it as citations |
| `coe-treaty-office` | Council of Europe Treaty Office | signatures, ratifications, entry into force per state, denunciations, reservations, declarations and objections |

Every provider is `unverified-live` until a dated live run is recorded (#2645).
The offline evidence is authored fixtures for fictional treaties and states.

## Journeys

1. **Treaty to a participant's status.** Run the `legal-research` pack, then
   `treaty_status_as_of(treaty="XXVII-7", participant="<name as published>",
   as_of="2025-01-01")`. The chain is selected by the action, deposit and
   effective dates as published. An action without a date is `unclear`; a
   deposited instrument whose published effective date is later is `pending`;
   both carry the source text. `depositary_as_of` answers against an earlier
   depositary status.
2. **State across depositaries.** `propose_treaty_matches(geo_namespace=...)`
   proposes participant-to-place candidates (published ISO codes first, names
   only where a source publishes no code) and treaty-to-treaty candidates from
   published cross-references. A reviewer accepts with `review_treaty_match`;
   `participant_treaty_actions(participant="geospatial:place:<id>")` then
   reaches every accepted participant record. Unmatched records stay visible
   (`list_treaty_identity_candidates`).
3. **Reservations and objections.** `treaty_reservations_and_objections`
   returns the statements verbatim with their anchors and dates.
4. **Links.** `link_treaty_records` links agreements to the EU acts CELLAR
   cites (Legal works by CELEX), sanctions legal bases citing a treaty's exact
   identifier and Trade flows reporters sharing a published ISO code or reached
   through an accepted match.
5. **History and monitors.** `treaty_record_history` lists every revision,
   including depositary corrections and rows the source no longer shows
   (`removed-by-source`, never deleted). `create_treaties_monitor` subscribes
   to a treaty or participant; `run_treaties_monitor` at a committed watermark
   notifies new actions, statements, corrections, entry into force and
   removals, each citing the new and previous revision.
6. **Evidence.** `export_treaty_evidence_bundle` returns assertions each citing
   source, record revision, depositary revision and as-of time.

## Minimisation

No natural person is a participant, field, key, match or monitor target.
Telephone numbers and e-mail addresses inside published declarations are
withheld at ingestion (`[contact details withheld: TR01]`); a record carrying a
signatory, representative or contact field is refused at write time.

## Offline acceptance

`tests/unit/domains/test_treaties_acceptance.py` replays the three pinned
fixtures through the source-pack runtime with sockets blocked and covers
revision history, as-of answers, reviewable identity, cross-pack links, a
subject with no records, the exclusions and the minimisation decision.
