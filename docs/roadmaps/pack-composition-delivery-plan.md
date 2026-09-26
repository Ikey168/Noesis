# Pack composition delivery plan

Status: proposed implementation backlog, 2026-09-26. Planning only; creating
issues does not implement the composition resolver, lifecycle coordinator, or
workflow dispatcher.

Tracking: [#1788](https://github.com/Ikey168/Noesis/issues/1788). Architecture:
[pack and workflow composition](../architecture/pack-workflow-composition.md).

Each slice C01–C09 from the architecture's incremental delivery plan is a GitHub
issue with sub-issues. A slice closes when its last sub-issue closes. Dependencies
below refer to sub-issue identifiers; a sub-issue that names only its predecessor
also inherits every dependency of its slice.

## Slice order

C01 → C02 → C03 → C04 → C05 → C06 → C08; C04 + C05 → C07 → C08; C08 → C09.
C06 and C07 proceed in parallel once C05 lands.

## C01 — Inventory ([#1789](https://github.com/Ikey168/Noesis/issues/1789))

Documentation only. C01.1–C01.3 run in parallel.

| Sub-issue | Deliverable | Depends on |
| --- | --- | --- |
| [#1798](https://github.com/Ikey168/Noesis/issues/1798) C01.1 | Capability string → provider code → exposing tools → declaring bundles; duplicates and orphans flagged. | — |
| [#1799](https://github.com/Ikey168/Noesis/issues/1799) C01.2 | Authoritative store per record type with ID scheme, revision addressability, native-revision links, namespace scoping. | — |
| [#1800](https://github.com/Ikey168/Noesis/issues/1800) C01.3 | Version/range/dependency rules per surface, their divergences, and the rule set the resolver adopts. | — |
| [#1801](https://github.com/Ikey168/Noesis/issues/1801) C01.4 | Routes, MCP tool IDs, aliases, and data prerequisites per capability; preserved-identifier list. | C01.1 |
| [#1802](https://github.com/Ikey168/Noesis/issues/1802) C01.5 | Decision record: activation-journal storage and startup reconciliation boundary. | C01.2, C01.3 |
| [#1803](https://github.com/Ikey168/Noesis/issues/1803) C01.6 | `docs/architecture/pack-composition-inventory.md` with unknowns list; closes C01. | C01.1–C01.5 |

## C02 — Contracts ([#1790](https://github.com/Ikey168/Noesis/issues/1790))

Schemas and validators only; no runtime enablement changes.

| Sub-issue | Deliverable | Depends on |
| --- | --- | --- |
| [#1804](https://github.com/Ikey168/Noesis/issues/1804) C02.1 | Composition manifest schema as a new `pack_format` value; unknown critical fields fail. | C01.6 |
| [#1805](https://github.com/Ikey168/Noesis/issues/1805) C02.2 | v1 `PackManifest` and `DomainPack` adapter with round-trip tests. | C02.1 |
| [#1806](https://github.com/Ikey168/Noesis/issues/1806) C02.3 | Provider descriptor schema with side-effect classes, idempotency, readiness probes, semantic constraints. | C01.6 |
| [#1807](https://github.com/Ikey168/Noesis/issues/1807) C02.4 | Resolved composition plan schema and canonical digest. | C02.1, C02.3 |
| [#1808](https://github.com/Ikey168/Noesis/issues/1808) C02.5 | Readiness assessment and activation receipt schemas. | C02.4 |
| [#1809](https://github.com/Ikey168/Noesis/issues/1809) C02.6 | Contract identities in the schema registry; shared valid/invalid fixture corpus; closes C02. | C02.1–C02.5 |

## C03 — Resolver ([#1791](https://github.com/Ikey168/Noesis/issues/1791))

Pure functions; no I/O.

| Sub-issue | Deliverable | Depends on |
| --- | --- | --- |
| [#1810](https://github.com/Ikey168/Noesis/issues/1810) C03.1 | Transitive dependency closure, optional features as visible omissions, cycle detection. | C02.6 |
| [#1811](https://github.com/Ikey168/Noesis/issues/1811) C03.2 | Version and contract compatibility with retained-pin preservation and exact locking. | C03.1 |
| [#1812](https://github.com/Ikey168/Noesis/issues/1812) C03.3 | Provider binding with ambiguity, store-ownership, and semantic-constraint checks. | C03.2 |
| [#1813](https://github.com/Ikey168/Noesis/issues/1813) C03.4 | Canonical digest, resolver version, resume/replay rules; closes C03. | C03.3 |

## C04 — Discovery and readiness ([#1792](https://github.com/Ikey168/Noesis/issues/1792))

Legacy catalog output stays unchanged throughout.

| Sub-issue | Deliverable | Depends on |
| --- | --- | --- |
| [#1814](https://github.com/Ikey168/Noesis/issues/1814) C04.1 | Plan-derived pack and data mappings in the catalog; tool IDs and aliases preserved. | C03.4, C01.4 |
| [#1815](https://github.com/Ikey168/Noesis/issues/1815) C04.2 | Operation-specific readiness extending `_state` / `_data_state`; distinct blocker kinds. | C04.1 |
| [#1816](https://github.com/Ikey168/Noesis/issues/1816) C04.3 | Composition explanations: provider, consumers, reason, pin, data needs, block reason. | C04.2 |
| [#1817](https://github.com/Ikey168/Noesis/issues/1817) C04.4 | Shadow mode and diff report against legacy state. | C04.3 |
| [#1818](https://github.com/Ikey168/Noesis/issues/1818) C04.5 | Diagnostics redacted to the caller's own access; closes C04. | C04.3 |

## C05 — Lifecycle ([#1793](https://github.com/Ikey168/Noesis/issues/1793))

| Sub-issue | Deliverable | Depends on |
| --- | --- | --- |
| [#1819](https://github.com/Ikey168/Noesis/issues/1819) C05.1 | Durable selections with distinct installed, selected, and resolved states. | C03.4, C01.5 |
| [#1820](https://github.com/Ikey168/Noesis/issues/1820) C05.2 | Activation journal, atomic generation switch, startup reconciliation. | C05.1 |
| [#1821](https://github.com/Ikey168/Noesis/issues/1821) C05.3 | Lifecycle coordinator: preview, stage, verify, publish; activation receipts. | C05.2, C04.5 |
| [#1822](https://github.com/Ikey168/Noesis/issues/1822) C05.4 | Shared-dependency retention on disable, administrative provider shutdown, bounded uninstall. | C05.3 |
| [#1823](https://github.com/Ikey168/Noesis/issues/1823) C05.5 | One authority per setting: legacy delegation and compatibility-flag rollback; closes C05. | C05.4 |

## C06 — Source integration ([#1794](https://github.com/Ikey168/Noesis/issues/1794))

Parallel with C07. C06.2 and C06.3 run in parallel.

| Sub-issue | Deliverable | Depends on |
| --- | --- | --- |
| [#1824](https://github.com/Ikey168/Noesis/issues/1824) C06.1 | Upgrade impact preview/apply covers composition dependents; incompatible upgrades blocked. | C05.5 |
| [#1825](https://github.com/Ikey168/Noesis/issues/1825) C06.2 | Explicit schedule ownership on source-pack schedules. | C06.1 |
| [#1826](https://github.com/Ikey168/Noesis/issues/1826) C06.3 | Acquisition deduplication key across consumers in the existing runtime. | C06.1 |
| [#1827](https://github.com/Ikey168/Noesis/issues/1827) C06.4 | Aggregated provider/account limits with per-run budgets kept; closes C06. | C06.3 |

## C07 — Workflow binding and dispatch ([#1795](https://github.com/Ikey168/Noesis/issues/1795))

Parallel with C06.

| Sub-issue | Deliverable | Depends on |
| --- | --- | --- |
| [#1828](https://github.com/Ikey168/Noesis/issues/1828) C07.1 | Workflow template contract bound to investigation templates and intake sessions. | C04.5, C05.5 |
| [#1829](https://github.com/Ikey168/Noesis/issues/1829) C07.2 | Plan digests bound to sessions, projects, and recipe runs; existing run controls preserved. | C07.1 |
| [#1830](https://github.com/Ikey168/Noesis/issues/1830) C07.3 | Dispatcher: registered bindings only, declared effects, validation, authority rechecks. | C07.2 |
| [#1831](https://github.com/Ikey168/Noesis/issues/1831) C07.4 | Retry via owner idempotency and receipts; unknown-outcome stop. | C07.3 |
| [#1832](https://github.com/Ikey168/Noesis/issues/1832) C07.5 | Real local adapters; fixture recipe path stays identifiable; closes C07. | C07.4 |

## C08 — First composition proof ([#1796](https://github.com/Ikey168/Noesis/issues/1796))

| Sub-issue | Deliverable | Depends on |
| --- | --- | --- |
| [#1833](https://github.com/Ikey168/Noesis/issues/1833) C08.1 | Geospatial provider descriptor with the Berlin source pack and authoritative stores. | C06.4, C07.5 |
| [#1834](https://github.com/Ikey168/Noesis/issues/1834) C08.2 | OSINT location-investigation and Research place-evidence templates. | C08.1 |
| [#1835](https://github.com/Ikey168/Noesis/issues/1835) C08.3 | Joint resolution: one spatial binding, shared record identities, ambiguity check. | C08.2 |
| [#1836](https://github.com/Ikey168/Noesis/issues/1836) C08.4 | Bounded offline journey through real local adapters with owner receipts. | C08.3 |
| [#1837](https://github.com/Ikey168/Noesis/issues/1837) C08.5 | Disable, revise, revoke, crash, and resume scenarios. | C08.4 |
| [#1838](https://github.com/Ikey168/Noesis/issues/1838) C08.6 | Migration parity: shadow diff, cutover, compatibility-flag rollback, legacy artifacts. | C08.5 |
| [#1839](https://github.com/Ikey168/Noesis/issues/1839) C08.7 | Acceptance-matrix suite, one test per row; closes C08 and gates C09. | C08.6 |

## C09 — Migration ([#1797](https://github.com/Ikey168/Noesis/issues/1797))

| Sub-issue | Deliverable | Depends on |
| --- | --- | --- |
| [#1840](https://github.com/Ikey168/Noesis/issues/1840) C09.1 | Delivery-state audit of expansion roadmaps; minimal composition dependencies recorded. | C08.7 |
| [#1841](https://github.com/Ikey168/Noesis/issues/1841) C09.2 | Code-registered domain bundles migrated: shadow, cutover, retire. | C09.1 |
| [#1842](https://github.com/Ikey168/Noesis/issues/1842) C09.3 | Source-pack projectors and `packs/` manifests migrated; existing owners reused. | C09.2 |
| [#1843](https://github.com/Ikey168/Noesis/issues/1843) C09.4 | Funding & Grants composed under the contracts ([#1775](https://github.com/Ikey168/Noesis/issues/1775)). | C09.3 |
| [#1844](https://github.com/Ikey168/Noesis/issues/1844) C09.5 | Legacy paths retired; architecture status updated; closes C09 and #1788. | C09.1–C09.4 |

## Boundaries that hold for every sub-issue

- No process split, new database, generic scheduler, parallel research-project
  ledger, or one MCP server per capability or pack.
- A pack or profile is not a namespace or authorization grant. An installed
  dependency cannot grant permissions or bypass an OSINT-specific gate.
- Live dispatch needs separate acceptance evidence. A fixture-only recipe run
  never claims tool dispatch.
- Deferred and not scheduled here: third-party executable plugins, remote
  installation, automatic provider substitution, distributed activation,
  concurrent provider major versions, automatic destructive schema migrations.
