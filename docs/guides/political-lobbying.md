# Political lobbying guide

Tracking: #1911. The optional `lobbying` feature of the Political bundle
(default off) adds transparency-register evidence: for a legislative dossier,
an organisation or an office holder, what the registers declare - registrants,
clients, declared interests, declared spend ranges, meetings with officials -
each cited to the register revision it comes from. Declarations are what a
registrant or official filed: nothing here states influence or wrongdoing,
infers undeclared lobbying or turns a declared range into a point estimate.
Registers are never merged; conflicting declarations are shown side by side.

## Pieces

| Piece | Where |
| --- | --- |
| Source audit and access decisions (five `unverified-live`, two `not-implemented`) | `docs/roadmaps/political-lobbying-source-audit.md`, `PROVIDER_CONTRACTS` in `src/ingestion/lobbying_sources.py` |
| Acquisition (`lobbying-register` connector) | `eu-transparency-register`, `de-lobbyregister`, `ep-mep-meetings`, `ec-meetings`, `uk-consultant-lobbyists` in `config/source_packs/political.json` (1.1.0) |
| Record owner (`noesis-lobbying-record-v1`) | `src/kb/lobbying.py` (exports, entries, append-only register revisions) |
| Identity review | `src/kb/lobbying_identity.py` (the ownership identity state machine; entity identity decisions, reversible) |
| Dossier links | `src/kb/lobbying_links.py`; `legislative_dossier_timeline` / `legislative_dossier_dependencies` with `lobbying_namespace` |
| Answers and evidence bundle | `src/kb/lobbying_queries.py` |
| Monitors | `src/kb/lobbying_monitoring.py` (knowledge subscriptions) |
| MCP tools | `tools/knowledge_engine_mcp/lobbying.py` |
| Provider and feature | `packs/political/providers/political.lobbying.json`, `packs/political/composition.json` (`lobbying`, profile `political.lobbying-review`) |

## Journey

1. Enable the feature with a coordinator selection (`features=["lobbying"]`
   for the `political` bundle); the bundle works unchanged without it.
2. Run the five sources through the source-pack runtime. Each run records one
   export; unchanged entries add no revision, changed entries add a revision
   that references its predecessor, and an entry missing from a newer full
   export becomes a `deregistered` lifecycle revision. An older export that
   arrives later adds its distinct statements as dated observations only.
3. Build the dossier with `save_legislative_dossier`, then
   `link_lobbying_dossier`: register fields naming the dossier's procedure
   reference, CELEX/ELI or Bundestag printed-paper number link it
   (`explicit-field`); shared words only produce candidates. Accept or reject
   a candidate with `review_lobbying_dossier_link` (a `reviewed-assertion`),
   revert with `revert_lobbying_dossier_link`.
4. `propose_lobbying_identity_matches` proposes candidates from stated
   identifiers across registers and to Corporate Ownership, LEI and canonical
   entity records; review or revert with the `…_lobbying_identity_match`
   tools. A name alone is never acceptable, and register records are never
   rewritten. For a registrant that is also a media source,
   `lobbying_source_identity_candidates` hands the match to the source-identity
   alias review (`decide_source_alias`).
5. `list_dossier_declared_interests` answers who declared an interest and which
   officials met about the dossier (optionally `as_of` a date);
   `list_registrant_declarations` shows a registrant's revisions (or every
   reviewed register record of one entity side by side);
   `list_official_meetings` lists an office holder's declared meetings.
   `export_dossier_interests_report` stores the answer as a cited authored report.
6. `create_lobbying_monitor` watches a dossier, registrant, client or office
   holder; runs at committed watermarks deliver registrations,
   deregistrations, spend and client revisions (old and new as filed) and
   meetings, each citing the new and previous register revision.

Every answer is scoped by namespace and `knowledge:political:lobbying:read`;
identity views also need `knowledge:ownership:read`.

## Evidence

Offline acceptance runs on authored fixtures (`tests/fixtures/lobbying`,
fictional parties) in `tests/unit/domains/test_lobbying_acceptance.py`; every
export it records is marked `fixture` evidence. No register has a dated live
run yet; live validation and a cited demo are tracked separately (#2024).
