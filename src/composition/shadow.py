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

# Known, reviewed disagreements: (tool, field) -> why the composition answer differs.
ANNOTATIONS: dict[tuple[str, str], str] = {}


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


def report(disagreements: list[Mapping[str, Any]], view: CompositionView) -> dict[str, Any]:
    bundles: dict[str, list[dict[str, Any]]] = {}
    for item in sorted(disagreements, key=lambda d: (d["bundle"], d["tool"], d["field"])):
        note = ANNOTATIONS.get((item["tool"], item["field"]), "unreviewed")
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
