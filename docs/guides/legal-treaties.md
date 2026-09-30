# Legal treaties and treaty actions

The Legal pack's treaties provider (`legal.treaties`, #2581) answers, with
citations to the depositary revision used:

* given a **treaty and a participant** (a state, an international organisation
  or the EU) and a date, which actions the depositary records on or before that
  date - signature, consent to be bound, reservations and declarations,
  withdrawals and denunciations, entry into force - with pending and unclear
  items returned as such;
* given a **participant**, its treaty actions over a period;
* given a **treaty**, its reservations, declarations and objections quoted
  verbatim, an objection linked to the objected reservation only where the
  source links it.

It describes the depositary record. It gives no legal advice, infers no
obligation or compliance and never interprets the legal effect of a
reservation; treaty texts are linked, not reproduced. Sources and terms:
[source audit](../development/treaties-evidence/source-audit.md).

## Features and sources (`legal-research` 1.5.0, connector `treaties`)

Each source is a separate optional Legal feature, default off.

| Feature | Source | Provider | Delivers | Status |
| --- | --- | --- | --- | --- |
| `treaties-untc` | `untc-multilateral-status` | UN Treaty Collection | status pages of treaties deposited with the Secretary-General | `declined`: written permission required; fetches nothing |
| `treaties-eu` | `cellar-eu-international-agreements` | CELLAR (reusing the Legal CELLAR adapter) | EU agreements: language expressions, signature and entry-into-force dates, linked EU acts | `unverified-live` |
| `treaties-coe` | `coe-treaty-office-charts` | Council of Europe Treaty Office | chart of signatures and ratifications; reservations, declarations, withdrawals, denunciations | `unverified-live` |

Every depositary update is a revision: a corrected date or text is a new
revision with the source's status stamp, an action the source stops listing is
a `removed-by-source` revision, and nothing is deleted.

## Journeys

1. **Treaty and participant to the action chain.** Run the `legal-research`
   pack, then `treaty_status_as_of(treaty="cets:990", participant="Germany",
   as_of="2100-03-01")`. Each source's `chain` lists the actions dated on or
   before the date (published action, deposit or effective dates only),
   `pending` those effective later, `unclear` those without a published date
   (with the source's note) and `removed_by_source` what the depositary no
   longer lists. `record_state` describes the record (for example
   `denunciation-or-withdrawal-deposited-effective-later`), never a legal
   status. `known_as_of` answers with the revisions the source had published by
   an earlier date. `treaty_revision_history` shows every revision and the
   fields each correction changed.
2. **Participant identity.** `propose_treaty_matches` offers participants to
   geospatial places - a published ISO 3166-1 code first, then the exact name of
   a place that carries an ISO code - and treaties to each other through
   published cross-references only (a "CETS No. 990" citation in a CELLAR title,
   a shared UNTS registration number). A reviewer accepts or rejects each
   (`review_treaty_match`) and may revert it; a name alone is never acceptable.
   After acceptance, `participant="iso3166:DE"` reaches the participant in
   every source, and an accepted treaty match adds the other source's record.
   Unmatched participants and treaties stay visible.
3. **Participant's actions and a treaty's statements.**
   `participant_treaty_actions` filters by period, action type and source;
   `treaty_reservations_and_objections` returns the statements verbatim with
   their anchors and linked objections.
4. **Cross-pack links.** `link_treaty_records` links an EU agreement to the
   Legal works of the EU acts CELLAR relates to it (by CELEX), sanctions legal
   bases that cite a treaty identifier, and Trade flows reporter areas that
   reach the same place through two accepted matches. Missing targets and
   absent providers are reported.
5. **Monitoring.** `create_treaties_monitor(watch="treaty", key="cets:990")` or
   `watch="participant"`; `run_treaties_monitor` reports new actions, depositary
   corrections (with the changed fields), removals by the source and entry into
   force, each citing the new and previous revision.
6. **Evidence bundle.** `export_treaty_evidence_bundle` cites every item with
   source, record revision and as-of time; pending, unclear and not-acquired
   items are omissions.

## Exclusions

No legal advice, no inference of obligations or compliance, no interpretation
of the legal effect of reservations, declarations or objections, no treaty-text
redistribution beyond what each source licenses and no natural-person data.

## Evidence

Offline: `tests/unit/domains/test_treaties_acceptance.py` and the other
`test_treaties_*` suites over authored fixtures in `tests/fixtures/treaties/`.
Live: outstanding (TR13, #2645).
