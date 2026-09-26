"""Generated provider descriptors and overlays for migrated bundles (C09.2-C09.4).

The ownership table below names, for every migrated bundle and source-pack
projector, the provider that owns its records, the tables it owns, the source
packs it draws on, and the pack that contributes it. Capabilities come from
the declared capability strings (``tests/fixtures/composition/capability_map.json``,
verified against the catalog in C01.1) or, for projectors without declared
strings, from explicit tool groups. Each capability binds the catalog tools
that implement it, split by effect class, and declares the catalog's own data
labels and scopes, so migration changes no preserved identifier.

Declared strings whose behavior only runs inside another operation (no tool of
their own) are not bound: they stay listed under
``metadata.unbound_capability_labels`` with the reason.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
CAPABILITY_MAP = REPO_ROOT / "tests/fixtures/composition/capability_map.json"
PROVIDER_DIR = REPO_ROOT / "config/composition/providers"
IO_CONTRACT = {"name": "noesis-mcp-tool-io-v1", "version": "1.0.0"}
KE = "noesis-knowledge-engine."

# Labels served by capabilities that already exist in hand-written descriptors.
SHARED_ALIASES = {
    "native-wfs-2.0.0-geojson-acquisition": "sources.acquire",
    "bounded-geojson-featurecollection-import": "spatial.feature-import",
    "provenance-preserving-feature-revisions": "spatial.feature-inspect",
    "durable-spatial-projection": "spatial.geometry-store",
    "points-inside-boundary-query": "spatial.points-within-boundary",
    "reviewable-boundary-name-resolution": "spatial.boundary-resolution",
}
UNBOUND_REASON = ("runs inside source-pack execution or another operation and has no tool of its own; "
                  "not bound")


def _tables(*modules: str, prefixes: Iterable[str] = ()) -> list[str]:
    found: set[str] = set()
    for module in modules:
        for path in sorted(REPO_ROOT.glob(module)):
            found |= set(re.findall(r"CREATE TABLE IF NOT EXISTS ([a-z_]+)", path.read_text(encoding="utf-8")))
    wanted = tuple(prefixes)
    return sorted(t for t in found if not wanted or t.startswith(wanted))


# provider id -> ownership. ``bundle`` is the pack whose declared strings the
# provider serves (or None for explicit tool groups); ``contributor`` is the
# pack that contributes the provider (None: a deployment built-in).
OWNERSHIP: dict[str, dict[str, Any]] = {
    "noesis.legal": {"bundle": "legal", "prefix": "legal", "contributor": "legal",
                     "record_kind": "legal_record", "tables": lambda: _tables("src/kb/legal.py"),
                     "source_packs": ["legal-research"], "identity": "src.kb.legal"},
    "noesis.economics": {"bundle": "economics", "prefix": "economics", "contributor": "economics",
                         "record_kind": "economic_record", "tables": lambda: _tables("src/domains/economic/*.py"),
                         "source_packs": ["economic-statistics-and-filings"], "identity": "src.domains.economic"},
    "noesis.lei": {"tools": ["company_source_contracts", "inspect_lei_entity", "lei_parents_as_of",
                             "propose_lei_registry_links", "link_company_identity"],
                   "prefix": "lei", "group": "company-identities", "contributor": "economics",
                   "record_kind": "lei_record", "tables": lambda: _tables("src/kb/lei.py"),
                   "source_packs": ["economic-statistics-and-filings"], "identity": "src.kb.lei"},
    "noesis.political": {"bundle": "political", "prefix": "political", "contributor": "political",
                         "record_kind": "political_record",
                         "tables": lambda: _tables("src/domains/political/*.py"),
                         "source_packs": ["official-political-records"], "identity": "src.domains.political"},
    "noesis.technology": {"bundle": "technology", "prefix": "technology", "contributor": "technology",
                          "record_kind": "technical_record",
                          "tables": lambda: _tables("src/domains/technical/*.py"),
                          "source_packs": ["technical-software-knowledge"], "identity": "src.domains.technical"},
    "noesis.standards": {"tools": ["standards_source_contracts", "inspect_standard_edition", "inspect_certificate",
                                   "import_certificates", "propose_certificate_product_links",
                                   "review_certificate_product_link"],
                         "prefix": "standards", "group": "editions-and-certificates", "contributor": "technology",
                         "record_kind": "standard_record", "tables": lambda: _tables("src/kb/standards.py"),
                         "source_packs": ["technical-software-knowledge"], "identity": "src.kb.standards"},
    "noesis.market": {"bundle": "market", "prefix": "market", "contributor": "market",
                      "record_kind": "market_record", "tables": lambda: _tables("src/domains/market/*.py"),
                      "source_packs": [], "identity": "src.domains.market"},
    "noesis.products": {"bundle": "products", "prefix": "products", "contributor": "products",
                        "record_kind": "product_record", "tables": lambda: _tables("src/kb/products.py"),
                        "source_packs": ["products-displays"], "identity": "src.kb.products"},
    "noesis.osint-investigation": {"bundle": "osint", "prefix": "osint", "contributor": "osint",
                                   "record_kind": None, "tables": lambda: [],
                                   "source_packs": ["bounded-public-osint"], "identity": "src.osint"},
    "noesis.scholarly": {"bundle": "science", "prefix": "scholarly", "contributor": "science",
                         "labels": ["bounded-scholarly-source-acquisition", "study-methodology-records",
                                    "study-replication-graph", "paper-family-versions",
                                    "openreview-round-comparison", "systematic-review-screening",
                                    "preserved-citation-snapshots"],
                         "record_kind": "scholarly_record",
                         "tables": lambda: _tables("src/kb/methodology_provenance.py", "src/kb/systematic_review*.py",
                                                   "src/kb/citation_*.py", "src/domains/research/paper_families.py",
                                                   "src/domains/research/openreview_rounds.py"),
                         "source_packs": ["primary-scientific-evidence", "openreview-research"],
                         "identity": "src.kb.methodology_provenance+src.domains.research"},
    "noesis.cultural": {"bundle": "science", "prefix": "cultural", "contributor": "science",
                        "labels": ["cultural-primary-source-records", "rights-gated-cultural-assets",
                                   "primary-source-research-links", "cultural-object-place-projection"],
                        "record_kind": "cultural_object", "tables": lambda: _tables("src/kb/cultural.py"),
                        "source_packs": ["primary-scientific-evidence"], "identity": "src.kb.cultural"},
    "noesis.mathematics": {"bundle": "science", "prefix": "math", "contributor": "science",
                           "labels": ["math-literature-records", "oeis-sequence-records", "formal-library-snapshots",
                                      "formal-proof-dependencies", "formal-revision-comparison",
                                      "math-source-objects", "math-cross-source-links"],
                           "record_kind": "math_record", "tables": lambda: _tables("src/kb/mathematics.py"),
                           "source_packs": ["research-discovery"], "identity": "src.kb.mathematics"},
    "noesis.patents": {"tools": ["patent_source_contracts", "inspect_patent_publication", "patent_family_members",
                                 "patent_links", "propose_patent_link", "review_patent_link"],
                       "prefix": "patents", "group": "publications", "contributor": "science",
                       "record_kind": "patent_record", "tables": lambda: _tables("src/kb/patents.py"),
                       "source_packs": ["research-discovery"], "identity": "src.kb.patents"},
    "noesis.transit": {"tools": ["transit_source_contracts", "transit_feed_versions", "transit_departures",
                                 "transit_stops_in_bbox"],
                       "prefix": "transit", "group": "schedules", "contributor": "geospatial",
                       "record_kind": "transit_record", "tables": lambda: _tables("src/kb/transit.py"),
                       "source_packs": ["geospatial-berlin"], "identity": "src.kb.transit"},
    "noesis.research-analytics": {"tools": ["noesis-research.citation_graph", "noesis-research.literature_claims",
                                            "noesis-research.venues"],
                                  "prefix": "research", "group": "literature-analytics", "contributor": "research",
                                  "record_kind": None, "tables": lambda: [], "source_packs": [],
                                  "identity": "src.domains.research.analytics"},
    "noesis.funding": {"tools": ["funding_provider_contracts", "acquire_funding_source", "list_funding_opportunities",
                                 "inspect_funding_opportunity", "create_funding_profile", "update_funding_profile",
                                 "inspect_funding_profile", "assess_funding_eligibility", "build_funding_shortlist",
                                 "inspect_funding_shortlist", "create_funding_workspace", "inspect_funding_workspace",
                                 "update_funding_workspace_items", "refresh_funding_workspace",
                                 "record_funding_workspace_outcome", "draft_funding_application",
                                 "export_funding_application_draft", "create_funding_monitor",
                                 "run_funding_monitor", "poll_funding_monitor"],
                       "prefix": "funding", "group": "grants", "contributor": "funding-grants",
                       "record_kind": "funding_record",
                       "tables": lambda: _tables("src/kb/funding_*.py"), "source_packs": [],
                       "identity": "src.kb.funding_*"},
    "noesis.research-projects": {"tools": ["create_research_project", "inspect_research_project",
                                           "revise_research_project", "list_research_projects",
                                           "reserve_research_project_budget", "record_research_project_expenditure",
                                           "settle_research_project_budget", "inspect_research_project_budget"],
                                 "prefix": "research", "group": "projects", "contributor": None,
                                 "record_kind": "research_project", "tables": lambda: _tables("src/kb/research_projects.py"),
                                 "source_packs": [], "identity": "src.kb.research_projects"},
    "noesis.reports": {"tools": ["create_authored_report", "inspect_authored_report", "revise_authored_report",
                                 "export_authored_report"],
                       "prefix": "reports", "group": "authored", "contributor": None,
                       "record_kind": "authored_report", "tables": lambda: _tables("src/kb/authored_reports.py"),
                       "source_packs": [], "identity": "src.kb.authored_reports"},
    "noesis.quantitative": {"tools": ["register_quantitative_unit", "register_quantitative_metric",
                                      "record_quantitative_observation", "read_quantitative_series",
                                      "evaluate_quantitative_formula", "replay_quantitative_calculation"],
                            "prefix": "quantitative", "group": "calculations", "contributor": None,
                            "record_kind": "quantitative_record", "tables": lambda: _tables("src/kb/quantitative.py"),
                            "source_packs": [], "identity": "src.kb.quantitative"},
    "noesis.subscriptions": {"tools": ["noesis-subscriptions.create_subscription",
                                       "noesis-subscriptions.inspect_subscription",
                                       "noesis-subscriptions.poll_subscription",
                                       "noesis-subscriptions.pending_subscription_deliveries"],
                             "prefix": "subscriptions", "group": "events", "contributor": None,
                             "record_kind": "knowledge_subscription",
                             "tables": lambda: _tables("src/kb/subscriptions.py", prefixes=("knowledge_subscription",)),
                             "source_packs": [], "identity": "src.kb.subscriptions"},
}

# Source-pack projectors (src/ingestion/source_pack_runtime.py PROJECTORS):
# mapping target schema -> the one provider that owns the records it writes.
PROJECTOR_OWNERS = {
    "noesis-geospatial-feature-v1": "noesis.geospatial",
    "noesis-product-record-v1": "noesis.products",
    "noesis-legal-record-v1": "noesis.legal",
    "noesis-cultural-object-v1": "noesis.cultural",
    "noesis-patent-part-v1": "noesis.patents",
    "noesis-lei-part-v1": "noesis.lei",
    "noesis-standard-catalogue-v1": "noesis.standards",
    "noesis-transit-feed-v1": "noesis.transit",
    "noesis-math-record-v1": "noesis.mathematics",
}


def _source_pack_configs() -> dict[str, str]:
    configs = {}
    for path in sorted((REPO_ROOT / "config/source_packs").glob("*.json")):
        pack_id = json.loads(path.read_text(encoding="utf-8")).get("pack_id")
        if pack_id:
            configs[pack_id] = str(path.relative_to(REPO_ROOT))
    return configs


# Bundles whose overlays the generator owns entirely.
GENERATED_OVERLAYS = ("economics", "energy", "legal", "market", "political", "products", "technology")
# Hand-written overlays the generator merges contributions and aliases into.
MERGED_OVERLAYS = ("geospatial", "osint", "science", "funding-grants")
CODE_OVERLAYS = {"research": "src/domains/research/composition.json"}


def _catalog() -> dict[str, dict[str, Any]]:
    from src.composition.bindings import CATALOG_ARTIFACT

    return {t["id"]: t for t in json.loads(CATALOG_ARTIFACT.read_text(encoding="utf-8"))["tools"]}


def _effect(tool: Mapping[str, Any]) -> str:
    if tool["mutability"] != "write":
        return "read-only"
    name = tool["name"]
    if name.startswith(("acquire_", "harvest_", "ingest_", "sync_", "fetch_")) or name == "run_source_pack_execution":
        return "acquisition"
    return "local-mutation"


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")


def _capabilities(capability_base: str, tool_ids: list[str], catalog: Mapping[str, Any],
                  semantics: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One capability per effect class and data prerequisite set.

    Read-only keeps the base id. Tools of one effect that need different
    catalog data labels are split so every capability declares exactly the
    data its tools need: the largest group keeps the id, the others get a
    ``-via-<label>`` suffix naming the first label the base group lacks.
    """

    groups: dict[str, dict[tuple[str, ...], list[str]]] = {}
    for tool_id in tool_ids:
        if tool_id in catalog:
            tool = catalog[tool_id]
            groups.setdefault(_effect(tool), {}).setdefault(tuple(tool["required_data"]), []).append(tool_id)
    suffix = {"read-only": "", "local-mutation": "-changes", "acquisition": "-acquisition",
              "external-publication": "-publication"}
    result = []
    for effect in ("read-only", "local-mutation", "acquisition"):
        by_data = groups.get(effect, {})
        if not by_data:
            continue
        ordered = sorted(by_data.items(), key=lambda item: (-len(item[1]), sorted(item[1])[0]))
        base_data = set(ordered[0][0])
        used: set[str] = set()
        for index, (required_data, tools) in enumerate(ordered):
            capability = capability_base + suffix[effect]
            if index:
                extra = [label for label in required_data if label not in base_data] or list(required_data)
                capability += f"-via-{_slug(extra[0])}"
                if capability in used:
                    capability += f"-{index}"
            used.add(capability)
            tools = sorted(tools)
            scopes = sorted({s for t in tools for s in catalog[t]["required_scopes"]})
            result.append({
                "capability": capability,
                "version": "1.0.0",
                "input_contract": dict(IO_CONTRACT),
                "output_contract": dict(IO_CONTRACT),
                "semantics": dict(semantics),
                "effect": effect,
                "idempotent": effect == "read-only",
                "execution_receipt": False,
                "required_scopes": scopes,
                "required_context": ["namespace", "principal"],
                "record_kinds": [],
                "bindings": [{"kind": "mcp-tool", "id": t} for t in tools],
                "readiness": {"probe": "catalog.required-data", "required_data": list(required_data)},
            })
    return result


def build() -> dict[str, Any]:
    """Descriptors, aliases and unbound labels for every owned provider."""

    capability_map = json.loads(CAPABILITY_MAP.read_text(encoding="utf-8"))["capabilities"]
    catalog = _catalog()
    configs = _source_pack_configs()
    descriptors: dict[str, dict[str, Any]] = {}
    aliases: dict[str, dict[str, str]] = {}
    unbound: dict[str, dict[str, str]] = {}
    contributions: dict[str, list[str]] = {}
    for provider_id, own in OWNERSHIP.items():
        capabilities: list[dict[str, Any]] = []
        if own.get("tools"):
            tool_ids = [t if "." in t and t.split(".", 1)[0].startswith("noesis-") else KE + t for t in own["tools"]]
            capabilities += _capabilities(f"{own['prefix']}.{own['group']}", tool_ids, catalog,
                                          {"io": "catalog-declared-tool-schemas"})
        bundle = own.get("bundle")
        if bundle:
            labels = own.get("labels") or sorted(
                label for label, info in capability_map.items()
                if f"packs/{bundle}" in info["declaring_bundles"]
                and not any(label in (o.get("labels") or []) for o in OWNERSHIP.values() if o is not own))
            for label in labels:
                if label in SHARED_ALIASES:
                    aliases.setdefault(bundle, {})[label] = SHARED_ALIASES[label]
                    continue
                tools = capability_map[label]["tools"]
                if any(t.endswith(".run_source_pack_execution") for t in tools):
                    aliases.setdefault(bundle, {})[label] = "sources.acquire"
                    continue
                produced = _capabilities(f"{own['prefix']}.{label}", tools, catalog,
                                         {"io": "catalog-declared-tool-schemas", "declared_as": label})
                if not produced:
                    unbound.setdefault(bundle, {})[label] = UNBOUND_REASON
                    continue
                capabilities += produced
                aliases.setdefault(bundle, {})[label] = produced[0]["capability"]
        stores = []
        tables = own["tables"]()
        if own["record_kind"] and tables:
            stores.append({"record_kind": own["record_kind"], "tables": tables, "revision_addressing": "immutable"})
        if not capabilities:
            continue
        descriptor = {
            "contract": "noesis-capability-provider-v1",
            "provider_id": provider_id,
            "version": "1.0.0",
            "description": f"Generated by src/composition/bundles.py from the verified capability map; "
                           f"owns {own['identity']} records.",
            "implementation": {"identity": own["identity"], "version": "1.0.0"},
            "capabilities": capabilities,
            "stores": stores,
        }
        if own["source_packs"]:
            descriptor["source_packs"] = [{"pack_id": p, "range": "^1.0.0", "config": configs[p]}
                                          for p in own["source_packs"]]
        descriptors[provider_id] = descriptor
        if own["contributor"]:
            contributions.setdefault(own["contributor"], []).append(provider_id)
    # Bundles without generated label capabilities (geospatial): shared
    # aliases, source-pack execution, or an explicit unbound marker.
    for label, info in sorted(capability_map.items()):
        for declaring in info["declaring_bundles"]:
            bundle = declaring.split("/", 1)[1]
            if not declaring.startswith("packs/") or label in aliases.get(bundle, {}) \
                    or label in unbound.get(bundle, {}):
                continue
            if label in SHARED_ALIASES:
                aliases.setdefault(bundle, {})[label] = SHARED_ALIASES[label]
            elif any(t.endswith(".run_source_pack_execution") for t in info["tools"]):
                aliases.setdefault(bundle, {})[label] = "sources.acquire"
            else:
                unbound.setdefault(bundle, {})[label] = UNBOUND_REASON
    return {"descriptors": descriptors, "aliases": aliases, "unbound": unbound, "contributions": contributions}


def _json(value: Any) -> str:
    return json.dumps(value, indent=2) + "\n"


def _hand_written_capabilities() -> set[str]:
    capabilities: set[str] = set()
    for path in sorted(PROVIDER_DIR.glob("*.json")):
        descriptor = json.loads(path.read_text(encoding="utf-8"))
        if descriptor["provider_id"] not in OWNERSHIP:
            capabilities |= {c["capability"] for c in descriptor["capabilities"]}
    return capabilities


def render() -> dict[Path, str]:
    """Every generated or merged file with its expected content.

    Merged overlays keep their hand-written parts; everything the generator
    owns in them (generated providers, requirements on generated
    capabilities, aliases to generated capabilities) is replaced on each run.
    """

    built = build()
    hand_written = _hand_written_capabilities()
    generated = {c["capability"] for d in built["descriptors"].values() for c in d["capabilities"]}
    files: dict[Path, str] = {}
    for provider_id, descriptor in built["descriptors"].items():
        files[PROVIDER_DIR / f"{provider_id.split('.', 1)[1]}.json"] = _json(descriptor)
    for bundle in (*GENERATED_OVERLAYS, *MERGED_OVERLAYS, *CODE_OVERLAYS):
        if bundle in CODE_OVERLAYS:
            path = REPO_ROOT / CODE_OVERLAYS[bundle]
            from src.composition.identifiers import code_registered_packs

            base = {"pack_format": "noesis-pack-v2", "name": bundle, "version": "0.0.0",
                    "description": code_registered_packs()[bundle].description,
                    "requires": [], "contributes": {}}
        else:
            path = REPO_ROOT / "packs" / bundle / "composition.json"
            v1 = json.loads((path.parent / "pack.json").read_text(encoding="utf-8"))
            if bundle in MERGED_OVERLAYS and path.exists():
                base = json.loads(path.read_text(encoding="utf-8"))
                own = {c["capability"] for pid in built["contributions"].get(bundle, [])
                       for c in built["descriptors"][pid]["capabilities"]}
                base["requires"] = [r for r in base.get("requires", [])
                                    if r["capability"] in hand_written
                                    or (r["capability"] in generated and r["capability"] not in own)]
                base.setdefault("contributes", {})["providers"] = [
                    p for p in base["contributes"].get("providers", []) if p["provider_id"] not in OWNERSHIP]
                labels = {k: v for k, v in base.get("aliases", {}).get("capability_labels", {}).items()
                          if v in hand_written and k not in built["aliases"].get(bundle, {})}
                base.pop("aliases", None)
                if labels:
                    base["aliases"] = {"capability_labels": labels}
                base.get("metadata", {}).pop("unbound_capability_labels", None)
                if base.get("metadata") == {}:
                    base.pop("metadata")
            else:
                base = {"pack_format": "noesis-pack-v2", "name": v1["name"], "version": v1["version"],
                        "description": v1.get("description", ""), "requires": [], "contributes": {}}
        contributes = dict(base.get("contributes", {}))
        providers = {(p["provider_id"], p["version"]) for p in contributes.get("providers", [])}
        providers |= {(pid, "1.0.0") for pid in built["contributions"].get(bundle, [])}
        contributes.pop("providers", None)
        if providers:
            contributes["providers"] = [{"provider_id": p, "version": v} for p, v in sorted(providers)]
        base["contributes"] = contributes
        requires = list(base.get("requires", []))
        required = {r["capability"] for r in requires}
        for provider_id in built["contributions"].get(bundle, []):
            for offered in built["descriptors"][provider_id]["capabilities"]:
                if offered["capability"] not in required:
                    requires.append({"capability": offered["capability"], "range": "^1.0.0"})
                    required.add(offered["capability"])
        base["requires"] = requires
        merged_aliases = dict(built["aliases"].get(bundle, {}))
        merged_aliases.update(base.get("aliases", {}).get("capability_labels", {}))
        if merged_aliases:
            base["aliases"] = {"capability_labels": dict(sorted(merged_aliases.items()))}
        if built["unbound"].get(bundle):
            base.setdefault("metadata", {})["unbound_capability_labels"] = dict(sorted(built["unbound"][bundle].items()))
        files[path] = _json(base)
    return files


def _orphans(rendered: Mapping[Path, str]) -> list[Path]:
    """Generated provider files no longer produced by the ownership table."""

    return [path for path in sorted(PROVIDER_DIR.glob("*.json")) if path not in rendered
            and "Generated by src/composition/bundles.py" in path.read_text(encoding="utf-8")]


def stale() -> list[str]:
    rendered = render()
    changed = [path for path, text in rendered.items()
               if not path.exists() or path.read_text(encoding="utf-8") != text]
    return sorted(str(path.relative_to(REPO_ROOT)) for path in [*changed, *_orphans(rendered)])


def write() -> list[str]:
    written = []
    rendered = render()
    for path in _orphans(rendered):
        path.unlink()
        written.append(str(path.relative_to(REPO_ROOT)))
    for path, text in rendered.items():
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            written.append(str(path.relative_to(REPO_ROOT)))
    return sorted(written)


__all__ = ["OWNERSHIP", "PROJECTOR_OWNERS", "SHARED_ALIASES", "build", "render", "stale", "write"]
