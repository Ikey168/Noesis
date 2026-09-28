# Engineering Safety pack

Tracking issue: #2059. Given an aircraft type, vehicle model, pipeline
operator, facility or component, the pack assembles what authorities
published about it: airworthiness directives with applicability and
supersession, accident and incident investigations with findings and probable
causes as the investigating body stated them, safety recommendations with
their dated status, pipeline incidents and defect investigations with the
recalls they cite. Records are quoted; the pack never gives a safety verdict,
risk score, ranking or compliance determination, and a subject with no record
is *none on record*, never *safe*.

## Parts

| Part | Where |
| --- | --- |
| Source audit and access decisions | `docs/development/engineering-safety-evidence/source-audit.md` |
| Source pack (bounded selections, files and link-only entries) | `config/source_packs/engineering-safety.json` |
| Connectors (FAA, EASA, NTSB, PHMSA, CSB, NHTSA ODI and complaints, BFU, BEA link-only) | `src/ingestion/engineering_safety_sources.py` |
| Records and store (revisions, parts, runtime projector) | `src/kb/engineering_safety_records.py`, `src/kb/engineering_safety_store.py` |
| Contracts | `contracts/schemas/jsonschema/noesis-engineering-safety-record-v1.json`, `...-dossier-v1.json` (registered through `register_schemas`) |
| Reviewable subject identity | `src/kb/engineering_safety_identity.py` |
| Citation extraction and links | `src/kb/engineering_safety_citations.py` |
| Directives as of a date, recommendations, dossiers | `src/kb/engineering_safety_queries.py` |
| Monitors (knowledge subscriptions) | `src/kb/engineering_safety_monitoring.py` |
| MCP tools | `tools/knowledge_engine_mcp/engineering_safety.py` |
| Pack, composition overlay, providers | `packs/engineering-safety/` |

## Journey

1. Install and enable the `engineering-safety` source pack, accept each
   source's terms, and run it through the source-pack runtime
   (`run_source_pack_execution`, operation `records`).
2. `propose_safety_subject_matches` proposes candidates from published
   subjects to Products models (`products_namespace`) and canonical entities;
   a reviewer accepts or rejects each with `review_safety_subject_match`.
3. `link_safety_citations` links cited AD and recommendation numbers, and
   (when their namespaces are named) recall campaigns, standards and CFR/EU
   acts, by exact identifier.
4. `directives_as_of` answers which directives applied to a model on a date,
   with the revision used and the supersession chain.
5. `engineering_safety_dossier` assembles directives, investigations,
   recommendations, defect investigations, occurrences and complaint counts
   for a subject, a Products model id or an entity id.
6. `create_engineering_safety_monitor` / `run_engineering_safety_monitor`
   notify new directives, revisions, final reports, findings, recommendation
   status changes and defect-investigation upgrades.

Recalls stay with the Products safety feature (#1916); this pack links to
them by campaign number and never acquires them.
