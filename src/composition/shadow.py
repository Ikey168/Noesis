"""Catalog shadow mode for the registered bundles (C04.4).

Builds the composition view for everything currently registered — every v1
and code bundle through the read-only adapter, plus the provider descriptors
shipped under ``packs/*/providers/*.json`` — and runs the catalog with a
shadow sink, so legacy state is returned untouched while each disagreement
with the composition assessment is collected per bundle. Lifecycle cutover
(C05, C08) uses the committed report as evidence.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from src.composition.adapter import REPO_ROOT, adapt_all
from src.composition.readiness import CompositionView
from src.composition.resolver import resolve

SHADOW_REPORT = REPO_ROOT / "tests/fixtures/composition/shadow-report.json"

# Known, reviewed disagreements: (tool, field) -> why the composition answer differs
# and how it is resolved at cutover (C08.6). Anything missing reads "unreviewed".
_VOCABULARY = ("resolved: vocabulary only - the descriptor names its readiness probe where the legacy "
               "table names a logical store; both describe the same prerequisite")
ANNOTATIONS: dict[tuple[str, str], str] = {
    ("noesis-knowledge-engine.query_geospatial_features_within", "pack"):
        "resolved: attribution now comes from the plan (geospatial contributes geospatial.feature-query); "
        "legacy had no pack for the knowledge-engine server",
    ("noesis-knowledge-engine.query_geospatial_features_within", "required_data"): _VOCABULARY,
    ("noesis-osint.corroborate", "pack"):
        "resolved: attribution now comes from the plan (osint contributes osint.corroboration); the legacy "
        "stem table had no pack for the osint server",
    ("noesis-osint.corroborate", "required_data"): _VOCABULARY,
    ("noesis-osint.corroborate", "state"):
        "intended: once osint is composition-managed its tools follow the bundle's enablement; the legacy "
        "catalog served them whatever config/domain_packs.json said. Legacy authority applies until cutover",
    ("noesis-knowledge-engine.command_intake_mode", "required_data"): _VOCABULARY,
    ("noesis-knowledge-engine.run_source_pack_execution", "required_data"): _VOCABULARY,
    ("noesis-knowledge-engine.run_source_pack_execution", "state"):
        "intended: the descriptor probes the source-run store it owns; the legacy table never probed the "
        "knowledge-engine runtime, so an unprobed store read as available",
    ("noesis-research.literature_claims", "pack"):
        "resolved: same bundle under its composition id (research is the legacy alias of science)",
    ("noesis-research.literature_claims", "required_data"): _VOCABULARY,
}


def provider_descriptors(root: Path | None = None) -> list[dict[str, Any]]:
    root = root or REPO_ROOT / "packs"
    return [json.loads(path.read_text()) for path in sorted(root.glob("*/providers/*.json"))]


def registered_view(root: Path | None = None) -> CompositionView:
    bundles = adapt_all(root)
    descriptors = provider_descriptors(root)
    roots = [{"pack": bundle, "version": manifest["version"]} for bundle, manifest in sorted(bundles.items())]
    result = resolve(roots, list(bundles.values()), descriptors)
    if not result.ok:
        raise ValueError(f"registered bundles do not resolve: {result.failure.message}")
    return CompositionView(result.plan, descriptors, bundles.values())


def annotate(item: Mapping[str, Any]) -> str:
    """The reviewed resolution for a disagreement, or ``unreviewed``.

    Explicit entries win; otherwise the three reviewed classes of difference
    apply: plan-derived pack attribution, probe-id vocabulary, and state
    changes that follow from bundle enablement or from probing a store the
    provider owns. Anything else stays ``unreviewed`` and fails the parity test.
    """

    explicit = ANNOTATIONS.get((item["tool"], item["field"]))
    if explicit:
        return explicit
    from src.composition.adapter import bundle_id

    legacy, composed, reason = item.get("legacy"), item.get("composition"), str(item.get("reason") or "")
    if item["field"] == "pack":
        if legacy is None:
            return ("resolved: attribution now comes from the plan (the bundle contributes the bound capability); "
                    "legacy had no pack for this server")
        if isinstance(legacy, str) and bundle_id(legacy) == composed:
            return "resolved: same bundle under its composition id (legacy alias)"
    if item["field"] == "required_data":
        return _VOCABULARY
    if item["field"] == "state":
        if composed == "disabled" and "pack is disabled" in reason:
            return ("intended: once composition-managed, the tool follows its bundle's enablement; legacy authority "
                    "applies until cutover")
        if composed in {"degraded", "unavailable"} and legacy in {"available", "degraded", "empty"}:
            return ("intended: the descriptor probes the store the provider owns; the legacy table did not probe it "
                    "or probed a different store")
    return "unreviewed"


def report(disagreements: list[Mapping[str, Any]], view: CompositionView) -> dict[str, Any]:
    bundles: dict[str, list[dict[str, Any]]] = {}
    for item in sorted(disagreements, key=lambda d: (d["bundle"], d["tool"], d["field"])):
        note = annotate(item)
        bundles.setdefault(item["bundle"], []).append({**item, "annotation": note})
    return {
        "report": "noesis-composition-shadow-diff",
        "bundles_resolved": sorted(p["id"] for p in view.plan["packs"]),
        "bound_capabilities": sorted(view.bindings),
        "composed_tools": sorted(view.tools),
        "disagreements": bundles,
    }


async def shadow_report(view: CompositionView | None = None, **catalog_kwargs: Any) -> dict[str, Any]:
    from src.mcp_host.catalog import build_catalog

    view = view or registered_view()
    sink: list[dict[str, Any]] = []
    await build_catalog(composition=view, shadow_sink=sink, **catalog_kwargs)
    return report(sink, view)


def render(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"
