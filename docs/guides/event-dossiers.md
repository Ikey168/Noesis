# OSINT and Guardian event dossiers

An event dossier captures one existing event identity and the accounts and source revisions linked to it. It preserves each publication and the evidence-independence graph's origin clusters separately. The supported independent-origin count uses explicit lineage evidence; probable origins and unresolved lineage remain separate. None of these counts decides which account is true.

After ingesting Guardian and public OSINT material, attach the source revision IDs to event mentions or account evidence. Create a dossier using the established `event_id`:

```json
{"tool":"create_event_dossier","arguments":{"namespace":"osint","request_key":"incident-2026-01","event_id":"event:existing-id"}}
```

`inspect_event_dossier` shows the captured event, attributed accounts, exact source locators, origin evidence, text coverage, and acquisition representation. It accepts `offset` and `limit` up to 50. `event_dossier_timeline` pages through event, source, account and resolution history. The dossier does not group reports by title or create a new event resolution decision.

To exclude a syndicated publication or leave membership ambiguous, call `revise_event_dossier` with `expected_revision` and an override bound to a source already linked by the event records:

```json
{"tool":"revise_event_dossier","arguments":{"namespace":"osint","dossier_id":"event-dossier:existing-id","expected_revision":1,"overrides":[{"document_revision_id":"document-revision:copy","decision":"exclude","rationale":"A copied Guardian report","reviewed_by":"analyst"}]}}
```

`compare_event_dossier_revisions` and `export_event_dossier_comparison` pin two dossier revisions and show changed accounts, before and after source revisions, origin evidence, and unresolved conflicts. Contradicting values, accounts about different time windows, and unresolved entity identity are labeled separately. A missing retained revision appears as unavailable; earlier exports preserve their captured content.

`create_event_dossier_report` turns source-linked accounts into a versioned authored report. Its source dependencies can be monitored with `subscribe_cited_evidence`, `evaluate_cited_evidence_subscription`, and `poll_cited_evidence_alerts`, or checked with `assess_authored_report_changes`. These are explicit polling operations; no external messages are sent. Missing source revisions prevent creation of a sourced report. Fixture examples verify the workflow and access checks; they do not establish live provider coverage or independent human corroboration.
