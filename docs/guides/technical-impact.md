# Technical dependency impact reports

The technical source pack supplies acquired PyPI, npm and OSV records. Import the exact project `package-lock.json` or pinned `requirements.txt` content with `import_technical_inventory`. The import runs offline, stores a SHA-256 inventory hash, keeps direct and transitive entries when the lockfile provides them, and marks unsupported or unpinned entries as unresolved. Use the checked-in `config/investigation_templates/technical-dependency-impact.json` template for a project investigation.

After acquiring package and advisory sources, create a bounded snapshot:

```json
{"tool":"create_technical_impact_report","arguments":{"namespace":"research","request_key":"impact-2026-09-23","inventory_id":"technical-inventory:REPLACE","project_id":"project:REPLACE","limit":100,"offset":0}}
```

The report pins the inventory hash and committed source document revisions. Each finding is `affected`, `unaffected_under_assessed_ranges`, or `unknown`. The second label applies only to ranges actually assessed. Missing ranges, unavailable source revisions, unsupported ecosystems and withdrawn advisories remain unknown. Newer releases appear as review candidates; creating or exporting a report does not change a project dependency.

`inspect_technical_impact_report` rechecks inventory ownership, project access and every retained source revision. `compare_technical_impact_reports` shows finding changes alongside both inventory hashes and source snapshot hashes. Create a second report with a new request key after acquiring a new advisory, then compare it with the first. Repeating a request key with the same inventory and page returns the original report.

`export_technical_impact_report` returns cited affected, unaffected-under-assessed-ranges and unknown findings. Its `authored_report_content` can be passed to `AuthoredReportStore.create`; its `project_link` can be added with `ResearchProjectStore.revise` after reviewing the assessment. The exported source dependencies include exact document and revision IDs. The report retains upgrade suggestions separately from executed changes.

Each call handles at most 100 inventory entries. Reports with source documents that lack a committed revision explicitly downgrade those advisory conclusions to unknown. The tests use generated package and advisory records. Live provider coverage, package safety and independent human review require separate verification.
