# Pack and workflow composition architecture

Status: implemented, 2026-09-26 (C01–C09, [#1788](https://github.com/Ikey168/Noesis/issues/1788)).

Implemented surfaces:
- contracts and adapter (`src/composition/contracts.py`, `adapter.py`);
- resolver (`resolver.py`);
- catalog discovery and readiness (`readiness.py`, `src/mcp_host/catalog.py`);
- lifecycle coordinator (`lifecycle.py`);
- source integration (`src/ingestion/source_pack_upgrades.py`, `source_pack_runtime.py`);
- workflow bindings and authorized dispatch (`workflows.py`, `local_adapters.py`).

All twelve bundles and all nine source-pack projectors are composition-managed.
[Composition migration](composition-migration.md) records the ownership
statements and the legacy paths intentionally kept, each with its reason and
owner. Live provider availability and the quality of conclusions remain outside
what the implementation proves.

## Purpose and architectural decision

Introduce an explicit composition contract between existing packs, capabilities,
sources, and workflows while their integration boundaries are still tractable.
Users can enable useful bundles such as OSINT, Science/Research, or Music without
forcing each bundle to own independent stores, connectors, or workflow engines.

A pack is a versioned distribution and enablement bundle. It can contribute or
reference capabilities, source packs, ontology modules, profiles, and workflow
templates. These contributions may be used by several packs. A profile selects
vocabulary, sources, query defaults, and templates for a subject or task; it does
not become a storage boundary. Intake mode, subject, and capability are separate
choices: Deep Research can investigate music, mathematics, or company ownership.

OSINT remains a useful user-facing bundle of investigative capabilities, sources,
and templates. It can consume Geospatial alongside Science/Research. Geospatial
owns its spatial implementation; neither consumer owns the other consumer's data.
Existing names need not be renamed to fit an exclusive taxonomy.

## Current foundation and gaps

The detailed ownership map, preserved identifiers and version rules are in the
[composition inventory](pack-composition-inventory.md); activation-journal
storage and startup reconciliation are settled in
[ADR-003](decisions/ADR-003-composition-activation-journal.md).

This is a bounded inspection of the following implementation surfaces, not a
claim that every subsystem has been audited.

| Current surface | Observed behavior | Composition gap or reuse |
| --- | --- | --- |
| [DomainPack](../../src/domains/base.py) and [registry](../../src/domains/registry.py) | Bundles routes, enrichers, flags, capability names, and ontology metadata; registration and enablement are process-local. | Capability strings do not resolve providers, dependencies, or compatible contracts. |
| [Manifest](../../src/domains/pack_format.py) and [installer](../../src/domains/pack_install.py) | Declarative v1 manifests; installation enables a pack, replacement uninstalls the old registration first. Panels and planner keywords are now advisory. | Need staged replacement, explicit contribution ownership, and shared dependency lifecycles. |
| [Source packs](../../src/ingestion/source_packs.py) and [runtime](../../src/ingestion/source_pack_runtime.py) | Durable versions, enablement, acquisition preflight, budgets, cursors, schedules, and receipts. | Reuse execution and persistence; connect pack dependencies to these authoritative records. |
| [Source upgrades](../../src/ingestion/source_pack_upgrades.py) | Inspect dependent projects, templates, schedules, and reports before applying a version change. | Extend impact analysis to compositions; preserve existing source upgrade controls. |
| [MCP catalog](../../src/mcp_host/catalog.py) | Discovers actual tools and evaluates authorization, backend, data, and transport state; some pack mapping is hard-coded. | Add explicit capability-to-provider bindings and composition explanations to this catalog. |
| [Schema registry](../../src/kb/schema_registry.py) and [ontology](../../src/kb/ontology.py) | Versioned modules, compatibility/dependency logic, and evidence-backed crosswalks. | Reuse contract identities and version semantics; avoid a parallel ontology registry. |
| [KB domains](kb-domains.md) | Content views can overlap; namespace backings have separate lifecycles. | Preserve this distinction. A pack or profile is not a namespace or authorization grant. |
| [Intake sessions](../subsystems/intake-modes.md) and [investigation templates](../guides/investigation-workflows.md) | Coordinate existing artifacts; templates pin source packs. | Reference a resolved composition alongside existing session/project state. |
| [Research recipes](../guides/research-recipes.md) | Python runner invokes supplied adapters. Public MCP runner records caller-supplied fixtures with `actions_executed: false`. | Real authorized dispatch is a separate required integration stage, not a capability unlocked by manifest changes. |

## Composition model

| Element | Responsibility | Example |
| --- | --- | --- |
| Capability contract | Stable behavior, input/output contracts, required context, and side-effect classification. | Spatial containment or scholarly citation lookup. |
| Provider binding | Concrete registered implementation of a capability, with version, contracts, scopes, and readiness probe. | Existing Geospatial service exposed through its MCP tools. |
| Source pack | Acquisition declarations, mappings, source revisions, and runtime configuration. | Existing Berlin vector source configuration. |
| Ontology module | Versioned concepts and explicit mappings onto canonical records. | Mathematical theorem or music recording vocabulary. |
| Workflow template | Parameters, required capabilities, ordered work, and expected artifacts. | Investigate an event's location or review place-related literature. |
| Profile | Optional reusable selection of sources, vocabulary, and workflow defaults. | Mathematics within Science/Research. |
| Pack | Installable bundle of the above contributions and references. | OSINT or Science/Research. |
| Composition plan | Exact resolved versions, provider choices, dependencies, and reasons for one selection. | OSINT and Research sharing one spatial provider. |

Profiles may be inline pack configuration until reuse warrants an independently
versioned artifact. Workflow templates reference the existing recipe/session
machinery. No new generic scheduler or parallel research-project ledger is needed.

```mermaid
flowchart TD
    O[OSINT pack] --> C[Composition resolver]
    R[Science / Research pack] --> C
    P[Selected profiles and workflow template] --> C
    C --> L[Version-pinned composition plan]
    L --> G[Shared Geospatial provider]
    L --> S[Existing source-pack runtime]
    L --> W[Existing intake and recipe execution]
    G --> E[Authoritative records and revision references]
    S --> E
    W --> E
```

This is a logical architecture. It does not require splitting the process,
adding a database, or creating one MCP server per capability or pack.

## Record ownership and cross-pack exchange

Each record type has one authoritative service/store. A composition references
records through that owner's API, even when the implementation remains in its
current directory. Logical ownership should be established before code moves.

- Places and geometry stay in the existing Geospatial stores. Acquired features
  retain links to their native source revisions and spatial projections.
- Documents, evidence, entities, and temporal records retain existing identities.
  An item can participate in several profiles without being ingested again merely
  because a second pack is enabled.
- Sessions, projects, decisions, and reports retain their current owners. Modulo
  keeps its planning and note state; Noesis composition references do not migrate it.
- Domain-specific objects may have typed extensions and dedicated stores when
  their invariants require them. The architecture does not force a proof
  dependency, recording, and legal case into one generic entity payload.

Cross-pack references identify owner capability, native record kind/ID, namespace,
and immutable revision where available. Compatibility adapters preserve existing
reference contracts; unsupported revision addressing is explicit. Resolve current
access at read/export time. Store source assertions and derived links with their
evidence, rather than copying another store's mutable rows into a pack-owned truth.

Identity sharing is bounded by namespace and source entitlement. Two owners may
legitimately retain separate records. Matching content does not authorize merging
private data, treating conflicting assertions as identical, or publishing it.

## Proposed contracts and resolution rules

The five artifacts below are **specified** (C02): each has a JSON Schema under
`contracts/schemas/jsonschema/`, a runtime validator in
`src/composition/contracts.py`, a built-in identity in the schema registry, and
cases in the shared fixture corpus (`tests/fixtures/composition/corpus.json`).

| Artifact | Contract | Schema-registry identity |
| --- | --- | --- |
| Pack composition manifest | `noesis-pack-composition-v1` (a new `pack_format` next to `noesis-pack-v1`) | `schema:pack-composition@1.0.0` |
| Provider descriptor | `noesis-provider-descriptor-v1` | `schema:provider-descriptor@1.0.0` |
| Resolved composition | `noesis-composition-plan-v1`, canonical digest `plan_digest()` (sorted keys, arrays as sets, SHA-256, `digest` excluded) | `schema:composition-plan@1.0.0` |
| Readiness assessment | `noesis-composition-readiness-v1` | `schema:composition-readiness@1.0.0` |
| Activation receipt | `noesis-composition-activation-receipt-v1` | `schema:composition-activation-receipt@1.0.0` |

Existing v1 bundles are read through a read-only adapter
(`src/composition/adapter.py`). Anything v1 cannot express is filled with
defaults listed in `adapter.supplied_defaults`.

1. **Pack composition manifest:** immutable ID/version/hash; contributed modules;
   required capability IDs and contract ranges; optional features; source,
   ontology, profile, and workflow references; compatibility aliases. Extend the
   v1 format through a versioned successor and explicit v1 adapter. Unrecognized
   critical requirements fail validation rather than silently disappearing.
2. **Provider descriptor:** implementation identity/version; capabilities and
   exact input/output contract references; registered tool/service bindings;
   store ownership; required scopes/context; side-effect, idempotency, and
   readiness declarations. Manifests cannot name arbitrary code to execute.
3. **Resolved composition:** root selections; complete dependency graph; exact
   manifest/schema/provider hashes; selected optional features; binding decisions;
   output contracts; compatibility aliases; canonical digest and resolver version.
4. **Readiness assessment:** caller/namespace context; assessed plan digest;
   observation time; per-operation prerequisites, blockers, and optional omissions.
   Dynamic credentials and live health are not immutable facts in the plan.
5. **Activation receipt:** previous/new composition generations; affected
   registrations; existing source upgrade references; applied or failed stages;
   idempotency key and recovery status. Execution receipts stay with their owners.

Resolution is read-only and deterministic for the same manifests and policy:

- Reuse the schema registry's version rules; preserve compatible retained pins.
  Resolve against an explicit candidate set, then lock exact versions and hashes.
- Resolve required dependencies transitively; reject cycles, incompatible ranges,
  missing contracts, conflicting store ownership, and undeclared bindings.
- Choose an explicitly configured provider, or the only compatible provider.
  Multiple compatible providers without a selection yield an ambiguity result;
  registration order is never a tie-breaker.
- Optional features join the graph only when selected. Unavailable optional
  branches are visible omissions; required failures block that workflow.
- A contract range is insufficient proof of compatibility: validate the actual
  input/output bindings and declared semantic constraints. Different spatial
  algorithms, units, or evidence meanings are not interchangeable by name alone.
- Do not resolve `latest` again on resume. A provider or contract change requires
  a new plan and impact assessment; an unavailable historical provider is reported
  as unavailable for replay rather than silently substituted.

The first implementation supports one active version per provider identity in a
deployment. Conflicting major versions block composition. Side-by-side runtime
versions and multi-host coordination are deferred until demonstrated necessary.

## Installation, enablement, and execution

Installed, selected, resolved, authorized, ready, and executed are distinct states.
Installing a pack retains its manifest. Selecting it expresses deployment intent.
Neither implies network acquisition, active schedules, granted scopes, or a
successful end-to-end workflow.

The lifecycle coordinator previews the dependency closure and affected consumers,
stages registrations, verifies them, then publishes a new composition generation.
Use a durable activation journal plus an atomic generation switch for composition
state; do not claim a database transaction makes Python registrations and external
actions atomic. Failed activation preserves the previous generation; restart
reconstructs its bindings and reconciles any staged source-owner operations from
their receipts. No provider requests are performed during activation.

Shared dependencies remain active while any selected root or pinned active run
needs them. Disabling OSINT removes its selection, subscriptions, and future
workflow entry points as applicable; it must not disable the spatial provider
still required by Research. Explicit administrative provider shutdown reports
affected consumers and makes their required operations unavailable.

Schedules need explicit ownership: remove a pack-owned schedule only when that
owner requests it; retain shared schedules with remaining consumers. Acquisition
deduplication keys include the source version, query, namespace/access context,
and mapping. Aggregate provider/account limits across consumers while preserving
per-run budgets. This needs coordination with the existing source runtime, not
another acquisition loop.

Uninstall removes unused registrations and manifests according to retention/pins;
it does not delete evidence. Data deletion remains a separate retention operation.
Provider schema migrations require explicit compatibility and rollback plans;
switching an activation pointer cannot reverse a destructive data migration.

## Workflow integration and authority

An intake session selects a mode, subject profile, and workflow template. The
template declares required behavior. Resolution chooses providers and source
versions; readiness preflight checks the specific operation under the caller's
namespace, permissions, credentials, terms, data, and network policy.

Bind the result to the existing project/session and recipe run. Preserve the
current parameter validation, snapshots, budgets, checkpoints, cancellation, and
artifact ownership. The planned dispatcher invokes only registered bindings,
validates arguments/results, and rechecks authorization at every step and resume.
An installed dependency cannot grant permissions, imply source-term acceptance,
or bypass an OSINT-specific gate through a shared capability.

Read-only lookup, local mutation, acquisition, and external publication require
distinct declared effects. A successful preflight is an observation, not durable
permission to execute future actions. Aggregate output restrictions from actual
referenced evidence and recheck access when exporting or rendering results.

Retry only operations whose owner supports idempotency or a durable execution
receipt. When a worker crashes after an external effect but before recording its
checkpoint, reconcile with that receipt; if the outcome is unknown, stop the
affected step for reconciliation. Do not promise generic exactly-once execution.

The public fixture recipe path remains explicitly identifiable. Live dispatch
must have separate acceptance evidence; changing the reported execution mode
without invoking and verifying the bound operations is not implementation.

## Discovery and readiness

Extend the existing MCP catalog to answer: what provides a capability, which packs
consume it, why it was selected, what version is pinned, what data it needs, and
why a workflow is blocked or degraded. Preserve current tool IDs and aliases.

Readiness is operation-specific. Local spatial queries can work while a remote
source refresh is unavailable. Empty data, inaccessible data, missing credentials,
disabled providers, unverified live access, and failed execution remain distinct.
Dependency diagnostics must not expose inaccessible records, private profiles,
credential values, or other users' workflow selections.

Implementation (C04): `src/composition/readiness.py` holds the plan-fed
`CompositionView`, `assess()` (a readiness document per bound operation, derived
through the catalog's own `_state` priority, adding an `unauthorized` blocker kind)
and caller-scoped explanations. `build_catalog(composition=...)` attributes bound
tools to packs and data prerequisites from the plan and descriptors; unbound tools
keep the legacy tables. `shadow_sink=` returns the legacy catalog unchanged and
collects disagreements; `scripts/composition_shadow_report.py` regenerates the
committed report in `tests/fixtures/composition/shadow-report.json`. The resolver
that produces plans (C03) is `src/composition/resolver.py`. Provider descriptors
ship with bundles as `packs/<bundle>/providers/*.json`.

Implementation (C05, C06):
- The lifecycle coordinator is `src/composition/lifecycle.py`; ADR-003 has the
  tables and the reconciliation boundary.
- Source upgrade impact previews list active and run-pinned composition plans
  that pin the source pack. An active plan whose declared source-pack `range`
  excludes the candidate blocks apply (`composition_incompatible`, naming the
  plan digest).
- Source-pack schedules record their owners in `source_pack_schedule_owners`. A
  shared schedule goes only when its last owner releases it, and legacy
  schedules are never removed by a composition release.
- `SourcePackRuntime.run_shared` deduplicates acquisitions. Its key is the
  source version, the query, the access context and the mapping. Joining
  consumers reference the same receipt.
- Account limits in `source_pack_account_limits` add an aggregate ceiling across
  consumers on top of the per-run budget. Exhaustion is the distinct
  `aggregate_limit_exhausted` blocker, both in shared-run receipts and in
  readiness.

## First composition to prove the design

Status: proven for OSINT + Research + Geospatial only (C08, `test_first_composition.py`). The
composition is proven offline, through real local adapters and captured provider input. Live provider
availability and the quality of conclusions are not established. The remaining bundles were then migrated
with the same procedure (see [composition migration](composition-migration.md)).

Use existing OSINT, Research, and Geospatial surfaces before adding a new subject.

1. Register one spatial provider with explicit contracts for the actual query
   semantics, plus the existing source pack and authoritative stores.
2. Define an OSINT location-investigation template and a Research place-evidence
   template, each consuming the same spatial capability and permitted records.
3. Resolve both selections and prove they bind to one implementation and preserve
   the same record/revision identities. Profiles contribute defaults, not stores.
4. Execute a bounded offline journey through real local adapters: source fixture
   acquisition/projection, spatial query, evidence reference, and session artifact.
   Keep mocked provider input distinct from mocked tool execution.
5. Disable one selection, revise a source/provider, revoke access, and resume an
   interrupted run. Check the other consumer, historical references, and receipts.

The initial proof establishes composition and local execution behavior. Live
provider availability and the quality of investigative conclusions require their
own checks and cannot be inferred from this fixture journey.

## Incremental delivery plan

These slices are tracked in [#1788](https://github.com/Ikey168/Noesis/issues/1788);
each is a GitHub issue broken into sub-issues (C0x.n) with their own
dependencies and acceptance criteria, listed in the
[delivery plan](../roadmaps/pack-composition-delivery-plan.md). Dependencies in
this table refer to slice identifiers.

| Slice | Deliverable and boundary | Depends on | Acceptance |
| --- | --- | --- | --- |
| C01 | Inventory capability providers, authoritative stores, routes/tools, and existing dependency/version rules. | — | OSINT, Research, Geospatial, sources, and intake have an ownership map; unknowns are recorded. |
| C02 | Specify composition manifest, provider, plan, readiness, and receipt schemas plus v1 adapters. | C01 | Valid/invalid examples cover aliases, ranges, conflicting owners, effects, and critical unknown fields. |
| C03 | Build pure dependency and binding resolution over retained manifests. | C02 | Deterministic pins; actionable cycle, ambiguity, and incompatibility failures; no mutations or network. |
| C04 | Extend catalog discovery and operation-specific readiness with the plan. | C03 | Correct explanations for disabled, unauthorized, empty, offline, and optional states without private-data disclosure. |
| C05 | Add durable composition selection, activation journal, generation switch, and dependency retention. | C03, C04 | Failure/restart retains or reconstructs a working prior generation; disabling one consumer preserves another. |
| C06 | Integrate source upgrade impact, schedule ownership, acquisition reuse, and shared budgets. | C05 | Source pins/cursors remain authoritative; incompatible upgrades are blocked; callers cannot multiply account quotas. |
| C07 | Bind intake/templates/recipes to plans and implement authorized execution adapters. | C04, C05 | A real bounded local journey produces owner receipts; revocation, cancellation, retry, and unknown outcomes are handled. |
| C08 | Prove the OSINT + Research + Geospatial composition and migration parity. | C06, C07 | Shared identities, selective disablement, upgrade impact, rollback, and legacy behavior pass the matrix below. |
| C09 | Migrate additional bundles and update source-expansion roadmaps using the proven contracts. | C08 | Each migration names ownership, retained compatibility, and acceptance evidence before retiring its legacy path. |

V1 bundles initially adapt into composition without changing their runtime
behavior. Compare catalog/plan outputs in shadow mode before switching lifecycle
ownership for a bundle. During migration each mutable setting has exactly one
authority: the legacy path until cutover, the composition coordinator afterward.
Legacy API calls after cutover delegate or return a compatibility error; they
must not silently maintain a second enabled-state ledger. A compatibility flag
may roll back bindings while retained source versions and records stay intact.

## Acceptance matrix

| Scenario | Required result | Test (C08.7) |
| --- | --- | --- |
| Two packs consume Geospatial | One selected provider; shared permitted references; no duplicate store or automatic re-ingestion. | `test_one_geospatial_binding_consumed_by_both_roots` in `test_first_composition.py` |
| OSINT disabled, Research active | Research spatial operations still work; retained evidence remains addressable under current access. | `test_disabling_osint_keeps_research_spatial_operations_and_evidence` in `test_first_composition.py` |
| Required provider missing or ambiguous | Workflow blocked before execution with a specific dependency/binding explanation. | `test_required_provider_missing_or_ambiguous_blocks_before_execution` in `test_first_composition.py` |
| Optional acquisition unavailable | Permitted local analysis can run with explicit missing-source coverage. | `test_optional_acquisition_unavailable_runs_local_analysis_with_missing_source_coverage` in `test_first_composition.py` |
| Contract/ontology conflict | No silent overwrite or implicit semantic conversion; the affected composition is rejected. | `test_contract_or_ontology_conflict_rejects_the_composition` in `test_first_composition.py` |
| Upgrade changes a pinned provider/source | Impact preview lists accessible dependents; historical runs retain pins; new execution uses a new plan. | `test_source_revision_preview_blocks_incompatible_and_keeps_historical_pins` in `test_first_composition.py` |
| Crash during activation | Previous active generation survives; staged operations reconcile without duplicate registrations. | `test_crash_during_activation_keeps_the_previous_generation` in `test_first_composition.py` |
| Crash after a workflow mutation | Owner idempotency/receipt reconciles it, or the step reports unknown outcome instead of blind retry. | `test_crash_after_a_workflow_mutation_reconciles_and_resumes_under_the_original_digest` in `test_first_composition.py` |
| Access revoked after preflight | Execution/resume/read/export recheck authority and deny or redact as the owner contract requires. | `test_access_revoked_after_preflight_is_rechecked_everywhere` in `test_first_composition.py` |
| Several consumers acquire from one account | Existing per-run limits and aggregated provider limits both hold. | `test_aggregate_account_limit_holds_across_consumers_and_is_a_distinct_blocker` in `test_sources.py` |
| Legacy manifest/session/report | Existing identity, references, and public behavior remain valid through the adapter. | `test_legacy_manifests_keep_identity_and_public_behavior_through_the_adapter` in `test_first_composition.py` |
| Fixture-only public recipe run | Receipt still declares fixture execution; no claim of tool dispatch or live validation. | `test_fixture_runs_cannot_claim_dispatch_and_dispatch_mode_needs_the_dispatcher` in `test_workflows.py` |

`tests/unit/composition/test_acceptance_matrix.py` holds this mapping and fails
if a row loses its test.

What the proof does not establish:
- Live provider availability. Every journey uses captured fixture input for
  providers, even though tool execution is real.
- The quality of any investigative or scholarly conclusion drawn from the
  results.

The upgrade row is also exercised for the provider side by
`test_provider_revision_requires_a_new_plan_while_history_keeps_its_pin`.

## Effect on the existing expansion backlog

Geospatial work separates reusable spatial behavior from concrete source setup.
Mathematics remains a Science/Research expansion: new sources, ontology, and
formal-proof capabilities can be independently referenced. Cultural collections
compose Research with Geospatial. Patents, registries, standards, and transport
reuse existing owners wherever their records and operations already fit.

Music remains a possible future bundle, not approved implementation scope. Its
identity model and optional audio processing would be contributions under the
same contract. Funding & Grants now has an explicitly requested implementation
backlog in [#1761](https://github.com/Ikey168/Noesis/issues/1761), scoped in the
[funding roadmap](../roadmaps/funding-grants-pack-scope.md). It remains planned,
not implemented; procurement remains a suggestion. This architecture does not
automatically add other proposed subjects to the implementation backlog.

Existing expansion issues remain in place. C09 should audit their delivery state
and add composition dependencies only where necessary; do not assume earlier
proposed roadmaps still describe what has or has not shipped.

## Tradeoffs and deferred decisions

Composition adds manifest and lifecycle complexity. Limit the first resolver to
known registered providers, explicit version rules, and one deployment; validate
it with two consumers before generalizing. Keep provider semantics explicit so
the catalog cannot advertise unsupported interchangeability.

ADR-003 settled the activation journal's storage location and process
startup reconciliation boundary during C01/C02. Extend an existing registry where
its transaction/lifecycle contract fits; otherwise introduce only composition
metadata persistence, not a duplicate source, ontology, or session database.

Defer arbitrary third-party executable plugins, remote installation, automatic
provider substitution, distributed activation, concurrent provider major versions,
and automatic destructive schema migrations. None is required to establish
shared capability ownership and reliable cross-pack workflows.

Checked on 2026-09-26, all still deferred with no implementation in the
repository:
- **Third-party executable plugins:** manifests reject executable references (`executable_reference`).
- **Remote installation:** documents are installed only from the local candidate set.
- **Automatic provider substitution:** ambiguity is an error, and a revised provider needs `upgrade`.
- **Distributed activation:** ADR-003 assumes a single host.
- **Concurrent provider major versions:** a plan pins one version per provider identity (`conflicting_major`).
- **Automatic destructive schema migrations:** switching generations does not reverse a destructive migration (ADR-003 amendment).

Each needs its own issue before any work starts.
