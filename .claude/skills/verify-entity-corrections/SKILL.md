---
name: verify-entity-corrections
description: Smoke-test the user-driven knowledge-graph entity corrections pipeline end to end. Seeds a KG entity, submits every correction type (rename, add_alias, remove_alias, add_property, remove_property, merge) as a trusted user, checks each is stored pending, approves them as an admin, confirms the changes are visible in the live KG store, and verifies rejection. Imports the Python layer directly, so no API server, auth tokens, or database are needed. Use after changing entity_corrections.py, entity_correction_routes.py, or the kg_updater store.
---

# verify-entity-corrections

Smoke-test the user-driven entity corrections pipeline end-to-end.

**Use when:** you've changed `src/knowledge_graph/entity_corrections.py`,
`src/api/routes/entity_correction_routes.py`, or the `kg_updater.py` store,
and want to confirm the full lifecycle works before opening a PR.

## What it tests

1. Seeds a KG entity directly in the shared store.
2. Submits each correction type (rename, add_alias, remove_alias,
   add_property, remove_property, merge) as a trusted user.
3. Verifies each correction is stored as `pending`.
4. Approves all pending corrections as an admin.
5. Verifies each approved change is visible in the live KG store.
6. Rejects one additional correction and confirms it is rejected.
7. Prints a pass/fail summary table.

No API server, no auth tokens, no DB: imports the Python layer directly.

## Steps

Run the smoke-test script:

```bash
PYTHONPATH=. python3 .claude/skills/verify-entity-corrections/smoke.py
```

All lines must print `PASS`. If any print `FAIL`, investigate
`src/knowledge_graph/entity_corrections.py` and re-run.

## Interpreting failures

| Symptom | Likely cause |
|---------|-------------|
| `KeyError: entity not found` on approve | `_shared_store()` was reset between submit and approve — check singleton wiring |
| Correction applied but node unchanged | `_apply_correction` mutates the wrong object — check `store.get_node()` returns a mutable reference |
| Version sequence wrong | `_entity_version` counter not thread-safe — check lock scope |
| Merge source not removed | `store._nodes.pop()` path not reached — check `_apply_merge` logic |
