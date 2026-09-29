# Technology vulnerabilities guide

Tracking: #1913. The optional `vulnerabilities` feature of the Technology
bundle (default off) adds software vulnerability and supply-chain advisory
evidence: for a component, the advisories that name it, their affected ranges
per ecosystem, weakness classification, dated exploitation evidence and
published scores, each cited to a source revision. Sources are never merged,
and nothing here is an exploitability or risk verdict, patch or remediation
advice, or a severity where the source states none.

## Pieces

| Piece | Where |
| --- | --- |
| Source audit and access decisions (all `unverified-live`) | `docs/roadmaps/technology-vulnerabilities-source-audit.md`, `PROVIDER_CONTRACTS` in `src/ingestion/vulnerability_sources.py` |
| Acquisition (`vulnerability-feed` connector) | `nvd-cve-api`, `nvd-cve-history`, `osv-api`, `github-advisory-database`, `cisa-kev`, `first-epss`, `cve-services`, `nvd-cpe-dictionary`, `cwe-downloads` in `config/source_packs/technical.json` (1.2.0) |
| Record owner (`noesis-vulnerability-record-v1`) | `src/kb/vulnerabilities.py` |
| CWE / CPE ontology modules, reviewed crosswalk | `src/kb/vulnerability_reference.py` (`technology-cwe`, `technology-cpe`, `technology-components-cpe.<ns>`) |
| Cross-source reading, as-of answers | `src/kb/vulnerability_queries.py`, `kb_technical` `query_type="advisory"` |
| Component identity review | `src/kb/vulnerability_identity.py` (entity identity decisions, reversible) |
| Affected-as-of through impact tools | `assess_inventory(..., vulnerability_namespace=...)`, `ImpactReportStore.create(..., vulnerability_namespace=...)` |
| Monitors | `src/kb/vulnerability_monitoring.py` (knowledge subscriptions) |
| MCP tools | `tools/knowledge_engine_mcp/vulnerabilities.py` |

## Journey

1. Run the sources through the source-pack runtime (`run_source_pack`). The NVD
   API key, if any, is the `NOESIS_NVD_API_KEY` secret; runs are paced to the
   keyed or unkeyed limit. EPSS runs can take `parameters.cve_ids` from
   `store_cve_ids(conn, "global")`. Re-running an unchanged window adds no
   revision.
2. `inspect_vulnerability(namespace, "CVE-…", as_of)` shows every source's
   revision side by side, aliases with the sources asserting them, ranges with
   an agree/disagree note, CWE with its list version, CVSS per publisher, EPSS
   by score date and model version, KEV listings, and unknowns.
3. `propose_vulnerability_component_matches(namespace, inventory_id)` proposes
   candidates on equal canonical coordinates; CPE products link to packages or
   product records only through explicit identifiers or a reviewer's
   `propose_vulnerability_component_link`. Review with
   `review_vulnerability_component_match`; `revert_…` undoes it.
4. `create_technical_impact_report` with `vulnerability_namespace` pins the
   vulnerability revisions next to the inventory hash; comparing two reports
   names the advisory revision that changed a finding.
5. `create_vulnerability_monitor` follows a package coordinate or a CVE; runs
   at committed watermarks deliver new advisories, KEV additions, EPSS score or
   model-version changes and withdrawals/rejections once each.

## Evidence

Offline acceptance runs on authored fixtures (`tests/fixtures/vulnerabilities`,
fictional CVEs) in `tests/unit/domains/test_vulnerability_acceptance.py`. No
provider has a dated live run yet; live validation and a cited demo are
tracked separately (#2025).
