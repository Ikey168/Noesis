# Intake cross-repo authority and identity map

This map states, for each kind of object in the ten information intake modes,
which system is authoritative, which identifier and contract carry it, and how
Noesis and Modulo identities are reconciled. It is derived from the Noesis code
and contracts on this branch (see the file references). The Modulo side is
described only as far as Noesis contracts constrain it; Modulo adapters,
signed-in journeys and plugin-record writes are tracked in
[#1781](https://github.com/Ikey168/Noesis/issues/1781) and
[Modulo #474](https://github.com/Ikey168/Modulo/issues/474).

It answers [#2465](https://github.com/Ikey168/Noesis/issues/2465). For which
behaviour is tested, see the
[milestone verification](../development/intake-milestone-verification.md).

## Principle

| Modulo is authoritative for | Noesis is authoritative for |
| --- | --- |
| Signed-in workspace and account, plugin installation and plugin records (feed items, bookmarks, notes, tasks, journal entries, SOPs, runbooks, flashcards and their review logs and schedules), user edits to those records, planning and calendar blocks, and consent for any action Modulo takes (publishing, Blueprint runs, writes to plugin records) | Sources and evidence Noesis acquired (feed items, captured pages, documents and their revisions), evidence cards, claims, run provenance (sessions, receipts, assessments), and Noesis-authored artifacts and their versions (Decision Records, authored reports, Research Bundles, playbooks, practice packs and reviews, skills, Creation projects, Maintenance findings) |

The v3 handoff states this split in its `authority` field
(`src/kb/intake_modes.py`, `modulo_handoff`):
`modulo: [workspace, account, plugin records, user edits, planning]`,
`noesis: [acquired sources, evidence, claims, run provenance, authored artifacts]`.

Neither system copies the other's authoritative object. The rule is
**versioned links, not untracked copies**:

- A Noesis object is referenced as `{kind, id, namespace, version, locator?}`
  (`_reference` in `src/kb/intake_modes.py`). `version` is the exact
  authoritative revision; `locator` (`url`, `page`, `start`, `end`,
  `section`) points into that revision.
- A Modulo object is referenced either as a legacy `workspace_links` entry
  `{system: "modulo", workspace_id, kind, id, version}` with `kind` one of
  `intake_item`, `session`, `artifact`, `project`, `note`, `task`, or as a
  dedicated-plugin `plugin_links` entry
  `{workspace_id, account_id, plugin_id, collection, record_id, authoritative_version, representation, authority, noesis_reference?, source_locator?}`
  (`_plugin_links` in `src/kb/intake_modes.py`).
- `representation` says what the link is: `linked_projection` (render the
  authoritative object; never edit it here), `intentional_snapshot` (a pinned,
  read-only copy with its own identity), or `editable_copy` (a new object in
  the other store, with its own identity and history). `authority` is
  `modulo` or `noesis`; a Noesis-authoritative plugin link must carry an exact
  `noesis_reference`.

## Per object kind

| Object | Authority | Noesis identifier and contract | Modulo identity carried | How identity is reconciled |
| --- | --- | --- | --- | --- |
| Intake session (any mode) | Noesis (run provenance) | `intake:<32 hex>` from namespace, owner and `request_key`; integer `revision`; [`noesis-intake-session-v1`](../../contracts/schemas/jsonschema/noesis-intake-session-v1.json); export `noesis-intake-session-export-v1` | `workspace_links`, `plugin_links` on the session | Replaying the `request_key` returns the same session; each command needs `expected_revision` and a `command_key`. Modulo stores the session ID and revision. |
| Mode transition | Noesis | `origin {session_id, revision, mode, reason}`; v3 `transition_id` `intake-transition:<24 hex>` | Inherited `workspace_links` / `plugin_links` | A child session inherits the parent's links and references unless replaced. The same request key cannot create a second transition. |
| Handoff projection | Noesis (projection only) | [`noesis-modulo-intake-handoff-v1`](../../contracts/schemas/jsonschema/noesis-modulo-intake-handoff-v1.json), [v2](../../contracts/schemas/jsonschema/noesis-modulo-intake-handoff-v2.json), [v3](../../contracts/schemas/jsonschema/noesis-modulo-intake-handoff-v3.json); `correlation_key` = session ID | v3 `plugin_links`, `plugin_access_state: not_checked_by_noesis` | Re-export after any revision or correction. Noesis rechecks its own access on every export; Modulo must recheck each plugin record. |
| Feed subscription and feed item (Awareness) | Noesis for the acquired item and its content revisions; Modulo for its own inbox record and triage display | `subscription:<32 hex>`; `feed:<32 hex>` from the original URL or GUID; item content revisions; reference kind `intake_feed_item` | `workspace_links` kind `intake_item` or `plugin_links` for `feeds-reading-inbox` | A refresh of the same GUID or URL keeps the item ID and its decision. Promotion carries the exact item revision and annotation references. |
| Annotation on a feed item | Noesis | `annotation:<32 hex>`, immutable, pinned to an item revision | — | Idempotent per request key; historical inspection shows the annotations of that revision. |
| Captured page (Exploration source) and visit | Noesis | `explore:<32 hex>` from namespace, owner and URL; historical versions; `visit:<32 hex>`; reference kind `exploration_source` | `plugin_links` for `bookmark-read-later` or `web-archive-read-later` | A recapture of the same URL is a new version of the same source ID. Modulo bookmark IDs stay Modulo's. |
| Research project and topic | Noesis | `project:<32 hex>`; [`noesis-research-project-v1`](../../contracts/schemas/jsonschema/noesis-research-project-v1.json); pins exact source revisions | `workspace_links` kind `project` | `start_intake_research_topic` creates the session and project atomically; pinned sources report `current`, `superseded` or `unavailable`. |
| Research Bundle, Evidence Cards, claims, concepts, brief, map | Noesis | `research-bundle:<32 hex>` per project; [`noesis-intake-research-bundle-v1`](../../contracts/schemas/jsonschema/noesis-intake-research-bundle-v1.json); card and concept IDs inside a bundle revision; reference kinds `research_bundle`, `concept` | Modulo note or canvas may hold an `intentional_snapshot` | A Modulo edit never rewrites a bundle. A changed source makes the bundle unready until saved again. Export digests verify the revision chain. |
| Decision Record and comparative matrix | Noesis for the record and receipts; Modulo for its journal entry and edits | `decision:<32 hex>`; `decision-sensitivity:` receipts; [`noesis-decision-comparative-matrix-v1`](../../contracts/schemas/jsonschema/noesis-decision-comparative-matrix-v1.json); reference kind `decision` | `plugin_links` for `decision-journal` | Decision Support completion resolves the current, accessible decision revision and matches the selected option and rationale. Revisions append; earlier revisions stay readable. |
| Problem trail and action | Noesis | Session ID plus typed steps; `problem-action:<hex>` proposal and receipt ([`noesis-problem-action-v1`](../../contracts/schemas/jsonschema/noesis-problem-action-v1.json)) | `plugin_links` for `evidence-reproducibility`, `executable-runbooks`, `todo-lists` | Consent recorded in Noesis covers only actions Noesis runs through its own configured adapters; the receipt keeps the plugin links and correlation key. |
| Playbook, checklist, template, default configuration, automation rule; guided run | Noesis | `playbook:<32 hex>` with revisions and `trust_state`; `playbook-run:<32 hex>` pinned to one revision; `automation:<hex>` receipts; reference kind `procedure` | `plugin_links` for `personal-sops`, `executable-runbooks` | A revision resets trust to draft. A Modulo SOP or Blueprint keeps its own ID and version; the link says which is authoritative. |
| Practice pack, card and review | Noesis for the native pack, schedule and review history | `practice-pack:<32 hex>`, `card-<n>`, `practice-review:<32 hex>`; exports with a SHA-256 digest | `plugin_links`; imported cards keep `plugin_id`, `collection`, `record_id`, `authoritative_version`, `content_sha256`, source review logs and schedule ([`noesis-intake-modulo-flashcard-import-v1`](../../contracts/schemas/jsonschema/noesis-intake-modulo-flashcard-import-v1.json)) | Import is hash-matched to a migration preview. Source schedules are kept as provenance with `status: review_required`; FSRS state is never translated into the native schedule. |
| Skill | Noesis | `skill:<32 hex>`; independent assessments separate from self-reports | — | Links practice cards and procedures by their Noesis IDs. |
| Creation project and authored report | Noesis for the Noesis-authored report and the project; Modulo for its manuscripts and drafts | `creation:<32 hex>`; `report:<32 hex>` with revisions; export carries `publication_authorized: false` | `workspace_links`, `plugin_links` for `writing-manuscripts`, `notes-editor` | A newer report revision makes the attached snapshot stale until reattached and reviewed. Publishing needs Modulo consent and receipts. |
| Iteration cycle | Noesis for the cycle and for accepted revisions of Noesis objects | Session ID; contracts [`noesis-intake-iteration-decision-v1`](../../contracts/schemas/jsonschema/noesis-intake-iteration-decision-v1.json), `-report-v1`, `-concept-v1`, `-modulo-note-v1` | A Modulo-owned note is pinned as one `plugin_links` entry (`notes-editor`, `authority: modulo`) and a hashed `source_snapshot` | Accepted revisions of Noesis objects are written into their own history. For a Modulo note Noesis stores only a local candidate with `modulo_access_state: not_checked_by_noesis` and `writeback_state: not_written`; Modulo must apply it. |
| Maintenance finding and action | Noesis | `finding:<32 hex>`; [`noesis-intake-maintenance-impact-v1`](../../contracts/schemas/jsonschema/noesis-intake-maintenance-impact-v1.json), [`noesis-intake-maintenance-action-v1`](../../contracts/schemas/jsonschema/noesis-intake-maintenance-action-v1.json) | — | Noesis never deletes Modulo data; actions run only on Noesis objects, bound to a reviewed preview hash. Modulo dependents are not scanned. |
| Cadence and calendar block | Modulo | Session `cadence {interval_days, anchor_at_ms}` is recorded only | Modulo calendar block and reminder | Noesis records the requested interval and measures elapsed time within a session; it installs no schedule. |
| Plugin record link check | Modulo | `recheck_modulo_plugin_link` result: `current`, `version_changed`, `revoked`, `missing` or `unavailable` | Exact workspace, account, plugin, collection, record ID and version | Returns identity and versions only, never content. Unavailable without a server-configured provider. |
| Migration preview | Modulo records; Noesis preview | `modulo-migration:<24 hex>`; [`noesis-modulo-intake-migration-preview-v1`](../../contracts/schemas/jsonschema/noesis-modulo-intake-migration-preview-v1.json) | Per-record plugin, collection, record ID, authoritative version, schema, content hash, relations, attachments, locator, `persistence` (`authenticated_plugin_state` or `legacy_local`) | Versions are never inferred from timestamps. Duplicate identities fail. Unsupported fields and metadata gaps require review. |
| Browser-local reconciliation report | Modulo records; Noesis report | `modulo-reconciliation:<24 hex>`; [`noesis-modulo-intake-reconciliation-v1`](../../contracts/schemas/jsonschema/noesis-modulo-intake-reconciliation-v1.json) | Both previews' record identities and hashes | See below. |

`noesis-intake-session-export-v1` is registered by the Knowledge Engine server
but has no JSON Schema file in `contracts/schemas/jsonschema/`.

## Reconciling browser-local records with durable plugin state

Some Modulo plugins held intake records in browser-local storage before the
authenticated plugin-state API existed. Cross-device replacement cannot be
claimed until those records are reconciled with the durable plugin state. The
Noesis side of that procedure is:

1. **Preview the browser-local records.** Modulo (or a caller) submits the
   metadata of each local collection to `preview_modulo_intake_migration` with
   `persistence: "legacy_local"`. No content is sent; each record carries its
   plugin, collection, record ID, authoritative version and SHA-256 content
   hash.
2. **Preview the durable plugin state.** Submit the same plugins' durable
   records with `persistence: "authenticated_plugin_state"`. Over MCP such a
   preview is labelled `caller_supplied_plugin_state` or `fixture`. Only the
   server-side callback reader (`ModuloStateInventoryClient` in
   `src/kb/modulo_state_inventory.py`) produces an `authenticated_plugin_state`
   preview with a read receipt; it is not exposed as an MCP tool and needs
   deployment-held Modulo credentials, so a deployment must call it in process.
3. **Reconcile.** `reconcile_modulo_intake_migration(namespace, request_key,
   legacy_preview_id, plugin_state_preview_id)` matches records by plugin ID and
   record ID and reports, per browser-local record:
   - `already_durable`: the durable record has the same content hash. Action:
     `link_durable_record`.
   - `conflict`: the durable record has a different content hash. Action:
     `owner_resolution_in_modulo`. Both versions and hashes are shown.
   - `import_required`: no durable record has that identity. Action:
     `plugin_import_with_owner_review`.
   It also flags a different collection name and duplicated local content, and
   counts durable records with no local counterpart (`durable_only`).
4. **Read the verdict.** `cross_device_replacement` is one of
   `blocked_by_unreconciled_records` (a conflict or import remains),
   `unverified_inventory_source` (everything matched, but the durable preview
   was not an authenticated read), or `pending_modulo_migration_receipt`
   (everything matched on an authenticated read). It is **never** "established",
   and `cross_device_replacement_claimed` is always `false`.
5. **Resolve in Modulo.** Imports and conflict resolution happen through each
   plugin's own path in Modulo, which keeps originals. Then repeat steps 1–4
   with new request keys until no conflict or import remains.

The report is owner scoped, stored, and idempotent by request key;
`inspect_modulo_intake_reconciliation` reads it back under current access to
both previews. It records `remote_mutations: 0` and `originals_deleted: false`.
It is tested only with fixture and caller-supplied previews
(`tests/unit/kb/test_intake_modulo_migration.py`,
`tests/unit/tools/test_intake_milestone_gaps_mcp.py`).

## What remains for #1781 (Modulo side)

- Per-plugin adapters that read and write the dedicated Knowledge plugin
  records with version-checked mutations and send the exact identity fields
  above.
- A runtime inventory of the user's installed plugins, and a reconciliation
  report on real browser-local and durable data. No such report exists yet, so
  **cross-device replacement is not claimed**.
- Importing `import_required` records and resolving `conflict` records in each
  plugin, keeping originals.
- A deployment path that runs the authenticated callback inventory for a
  signed-in owner, so the reconciliation can reach
  `pending_modulo_migration_receipt` on real data.
- A production `PerCallerModuloPluginLinkProvider` so
  `recheck_modulo_plugin_link` can report real access.
- Writing an accepted Modulo-note Iteration candidate back to the note, and
  returning Noesis results to the same plugin record for every mode.
- Signed-in journeys: second device, offline Modulo, Noesis restart,
  duplicates, conflicting edits, source correction and access revocation.
- Consent and receipts for Modulo-side actions (publishing, Blueprint runs).
