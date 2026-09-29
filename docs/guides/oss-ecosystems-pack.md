# Open-source Software Ecosystems pack guide

From a package coordinate to its cited release, yank and licence history and to
its dependency graph as of a date. Tracking issue:
[#2192](https://github.com/Ikey168/Noesis/issues/2192). Source decisions:
[source audit](../development/oss-ecosystems-evidence/source-audit.md);
evidence: [offline and live](../development/oss-ecosystems-evidence/README.md).

## What the pack keeps, and what it does not

| Kept | Owner |
| --- | --- |
| Release states per source (`published`, `yanked`, `deprecated`, `unpublished`, `not_observed`) with reasons verbatim | `src/kb/oss_ecosystem_store.py` |
| Declared dependencies per release, as written (constraint, scope, optional, marker/target) | same |
| Licence declarations as published, with an SPDX normalisation per pinned SPDX License List release | same (`src/kb/oss_spdx.py` parses) |
| Organisation-level publisher declarations (PyPI organisation, npm scope, Maven `groupId`, crates.io team, POM `<organization>`) | same |
| Repository link assertions, Software Heritage visits and snapshot tags | same |
| deps.dev versions, licences, related projects and its resolved graphs, beside the registries | same |
| Repository and organisation matches (reviewable, revertible) | `src/kb/oss_ecosystem_identity.py` over `src/kb/entity_history.py` |

Not kept, by design: maintainer, author, contributor or committer identities
and activity; download counts, stars or any quality, health, popularity or
trust score; advisory data (cited from `technology.vulnerabilities`); package
or version objects (the Technology model's `package_object_id` and
`immutable_artifact_id` are referenced); licence interpretation.

## Revision rules

- Keys: record type, source, canonical coordinate (PEP 503 on PyPI,
  `group:artifact` on Maven, lower-case npm and crates names) and the
  ecosystem-normalised version. The same name in two ecosystems is two packages.
- Order: the source's own modification date where it states one (npm
  `time.modified`, crates.io `updated_at`), otherwise the observation time.
- A statement equal to the revision in effect adds nothing; a yank, un-yank or
  correction is a new revision; a reversion is a new revision too. Late older
  polls are kept as history, never become current and never produce events.
- A release missing from a later complete listing becomes `not_observed`;
  `unpublished` is recorded only when the registry says so (npm).

## Journey

1. `oss_source_contracts` lists every source's decision, terms, limits and the
   personal fields it drops; `oss_ecosystems_readiness` shows the bundle
   selection, the optional features and ready / fixture-only / unavailable per
   source, with live verification kept separate.
2. Install `config/source_packs/oss-ecosystems.json` with `install_source_pack`,
   accept each source's terms and run it through `run_source_pack_execution`
   (schedules and the maintenance orchestrator refresh it). Software Heritage
   runs take `origins` from repository link assertions only; the token, if any,
   is `NOESIS_SWH_TOKEN`.
3. `package_release_history` — each release's state on a date, reasons
   verbatim, deps.dev beside the registry, publishers and the reviewed
   repository with its archive snapshots, knowledge cutoff and gaps.
4. `licence_history` — declarations and SPDX expressions per release, the list
   version cited, changes and registry/deps.dev disagreements highlighted.
5. `dependency_graph_as_of` — declared-constraint resolution (not an observed
   lockfile) against releases published, acquired and available on the date;
   unresolved edges with reasons; deps.dev's graph beside; a receipt that
   `replay_dependency_graph` re-checks.
6. `propose_repository_package_matches`, `propose_publisher_organisation_matches`,
   `review_oss_identity_match`, `revert_oss_identity_match` — reviewed
   identity; `packages_by_organisation` lists organisation publishers only.
7. `package_advisories` and `compare_inventory_with_graph` — advisories cited
   from `technology.vulnerabilities`; a pinned technical inventory beside the
   graph with the pins that were yanked or deprecated on the inventory date.
8. `create_package_monitor` / `run_package_monitor` / `poll_package_monitor` —
   subscriptions following a package, a graph root or an organisation.

## Composition

`packs/oss-ecosystems/pack.json` (v1) with `composition.json` binds
`oss.registries`, `oss.dependency-graphs` and `oss.licences` plus
`technology.core`, `technology.vulnerabilities`, `platform.entity-identity`,
`platform.subscriptions` and `platform.source-runtime`. The optional features
`oss-deps-dev` (deps.dev published graphs) and `oss-software-heritage`
(`oss.archive`) default to off. The tracker named them `oss_deps_dev` and
`oss_software_heritage`; the composition contract only allows lower-case,
hyphenated feature ids, so they carry hyphens.

JSON Schemas: `contracts/schemas/jsonschema/noesis-oss-ecosystem-record-v1.json`
and `noesis-oss-ecosystem-answer-v1.json`, registered in the schema registry by
`register_oss_schemas`.
