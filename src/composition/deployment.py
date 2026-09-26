"""The reference deployment's composition candidates, selection and shadow report.

This module performs I/O (it reads packs, provider descriptors and contract
schemas); the resolver it feeds does not.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from src.composition import contracts as c
from src.composition.resolver import resolve

REPO_ROOT = Path(__file__).resolve().parents[2]
SHADOW_REPORT = REPO_ROOT / "tests/fixtures/composition/shadow_diff.json"
SHADOW_ANNOTATIONS = REPO_ROOT / "tests/fixtures/composition/shadow_annotations.json"


def code_registered_manifests() -> list[dict[str, Any]]:
    """Code-registered packs that have no distributable manifest of their own.

    A ``composition.json`` beside the pack's module (``src/domains/<name>/``)
    supplies its composition fields, as it does for distributable packs.
    """

    from src.composition.identifiers import code_registered_packs

    distributable = {path.parent.name for path in (REPO_ROOT / "packs").glob("*/pack.json")}
    manifests = []
    for name, pack in sorted(code_registered_packs().items()):
        if name in distributable:
            continue
        adapted = c.adapt_domain_pack(pack)
        overlay = code_overlay_path(name)
        if overlay.exists():
            adapted = c.merge_overlay(adapted, json.loads(overlay.read_text(encoding="utf-8")))
        manifests.append(adapted)
    return manifests


def code_overlay_path(name: str) -> Path:
    """Where a code-registered pack keeps its composition overlay."""

    return REPO_ROOT / "src/domains" / name / "composition.json"


def candidates() -> dict[str, Any]:
    """The explicit candidate set shipped with this checkout."""

    providers = c.load_providers()
    known = c.provided_capabilities(providers)
    manifests = c.load_all_packs(known_capabilities=known) + code_registered_manifests()
    return {
        "manifests": manifests,
        "providers": providers,
        "contracts": c.contract_candidates(),
    }


def reference_roots(manifests: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Every shipped bundle selected at its exact version."""

    return [{"name": m["name"], "range": m["version"]} for m in manifests]


def reference_plan(candidate_set: Mapping[str, Any] | None = None) -> dict[str, Any]:
    candidate_set = candidate_set or candidates()
    result = resolve(
        roots=reference_roots(candidate_set["manifests"]),
        manifests=candidate_set["manifests"],
        providers=candidate_set["providers"],
        contracts=candidate_set["contracts"],
    )
    if result["status"] != "resolved":
        raise c.CompositionError(
            "reference_unresolved", "the shipped bundles do not resolve", result=result
        )
    return result["plan"]


def shadow_report(**catalog_overrides: Any) -> dict[str, Any]:
    """Legacy-versus-composition disagreements for the reference catalog (C04.4)."""

    from src.composition.identifiers import build_reference_catalog
    from src.composition.readiness import assess

    candidate_set = candidates()
    plan = reference_plan(candidate_set)
    scopes = catalog_overrides.get("granted_scopes", {"public", "knowledge:read", "operator"})
    readiness = assess(
        plan, candidate_set["providers"], conn=catalog_overrides.get("conn"),
        scopes=scopes, observed_at_ms=0,
    )
    sink: list[dict[str, Any]] = []
    build_reference_catalog(
        composition={"plan": plan, "providers": candidate_set["providers"], "readiness": readiness},
        composition_mode="shadow",
        shadow_sink=sink,
        **catalog_overrides,
    )
    by_bundle: dict[str, list[dict[str, Any]]] = {}
    for diff in sorted(sink, key=lambda d: (d["tool"], d["field"])):
        consumers = sorted({
            b["consumer"].split("@", 1)[0] for b in plan["bindings"]
            if any(t["id"] == diff["tool"] for t in b.get("bindings", []))
        })
        for bundle in consumers or ["unattributed"]:
            by_bundle.setdefault(bundle, []).append(diff)
    return {
        "contract": "noesis-composition-shadow-report-v1",
        "plan_digest": plan["digest"],
        "bundles": dict(sorted(by_bundle.items())),
        "disagreements": len(sink),
    }


def unannotated(report: Mapping[str, Any], annotations: Mapping[str, str]) -> list[str]:
    """Disagreements with no committed annotation (``tool|field`` keys)."""

    missing = []
    for diffs in report["bundles"].values():
        for diff in diffs:
            key = f"{diff['tool']}|{diff['field']}"
            if key not in annotations:
                missing.append(key)
    return sorted(set(missing))


def load_annotations() -> dict[str, str]:
    if not SHADOW_ANNOTATIONS.exists():
        return {}
    return dict(json.loads(SHADOW_ANNOTATIONS.read_text(encoding="utf-8"))["annotations"])


__all__ = [
    "SHADOW_ANNOTATIONS",
    "SHADOW_REPORT",
    "candidates",
    "code_registered_manifests",
    "load_annotations",
    "reference_plan",
    "reference_roots",
    "shadow_report",
    "unannotated",
]
