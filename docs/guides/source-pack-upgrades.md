# Reviewed source-pack upgrades

Use `preview_source_pack_upgrade` to see the semantic difference between the
installed immutable manifest and a complete candidate. It reports added and
removed sources, connector and mapping changes, domain scope, credential
declarations, license terms, and network destinations. The preview writes
nothing. The candidate must use a new semantic version; changing content under
an installed version is rejected.

Call `preview_source_pack_upgrade_impact` with the same candidate to inspect
saved templates, projects, schedules, and report source revisions. The result
contains the semantic preview, a distinct `impact_hash`, paginated effects,
and retained-version status. Template and project pins remain on their old
version. Report citations remain on their original document revisions. Future
executions require review of changed source selection, mappings, credentials,
and terms. A report can show `version_unrecorded`, `missing_retained_version`,
`unresolved_source_dependencies`, or `unsupported_dependency_types`; these
are explicit limits on reproducibility. Only dependents within the caller's
current project, report, namespace, domain, and document access are disclosed.

For example, after installing `config/source_packs/research.json`, copy it to
a candidate, change `version` to `1.3.0`, and change the `crossref-works`
mapping version to `2.0.0`. Call:

```json
{"tool":"preview_source_pack_upgrade_impact","arguments":{"candidate":{"pack_id":"research-discovery","version":"1.3.0","description":"...","domains":["research"],"defaults":{"...":"..."},"sources":[{"...":"..."}]},"limit":100,"offset":0}}
```

The candidate above is abbreviated for display; send the complete manifest.
Save `preview.preview_hash` and `impact_hash`, then call
`apply_source_pack_upgrade` with that exact candidate, both hashes, a unique
`apply_key`, and any source IDs whose terms you explicitly accept:

```json
{"tool":"apply_source_pack_upgrade","arguments":{"candidate":{"...":"complete candidate manifest"},"preview_hash":"<preview hash>","impact_hash":"<impact hash>","apply_key":"research-1.3.0-reviewed","accepted_license_sources":["crossref-works"],"migrate_schedule":false}}
```

Application requires an operator. It reruns offline fixture conformance and
the existing credential, license, and public HTTPS preflight before committing
the new current version, audit, and receipt atomically. It makes no provider
request. Missing credentials or unaccepted terms fail explicitly. The default
keeps the existing schedule; `migrate_schedule=true` selects the candidate's
bounded default schedule. An old preview fails if the installed version or
dependent impact changed. Retrying the same `apply_key` and request returns
the original receipt; using that key for different content fails.

Use `inspect_source_pack_upgrade_receipt` with `pack_id` and `apply_key` to
read the retained hashes and preflight result. Old manifests and document
revisions remain available for pinned replay. If a candidate cannot run,
correct its credentials, accepted terms, or manifest and perform a new
reviewed upgrade. Do not edit a retained version in place. Fixture tests
exercise this path; they do not establish live provider availability.
