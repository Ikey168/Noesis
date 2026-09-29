"""The Open-source Software Ecosystems bundle: declared contributions, features and readiness (OS11).

Composed under the pack/workflow composition contracts
(``docs/architecture/pack-workflow-composition.md``): ``packs/oss-ecosystems/pack.json``
(``noesis-pack-v1``) with its ``composition.json`` overlay binds the
``oss.registries``, ``oss.dependency-graphs`` and ``oss.licences`` providers
plus the consumed ``technology.core``, ``technology.vulnerabilities``,
``platform.entity-identity``, ``platform.subscriptions`` and
``platform.source-runtime`` providers. The optional features ``oss-deps-dev``
(deps.dev published graphs) and ``oss-software-heritage`` (``oss.archive``)
default to off. Enablement is a coordinator selection; the bundle adds no
scheduler, permission ledger, package store, advisory store or entity store.
"""

from __future__ import annotations

import json
from typing import Any

from src.ingestion.oss_ecosystem_sources import LIVE_VERIFICATION, PROVIDER_CONTRACTS
from src.kb.oss_ecosystem_records import READ_SCOPE, RECORD_TYPES
from src.kb.oss_ecosystem_store import OssEcosystemStore, OssStoreError

BUNDLE_ID = "oss-ecosystems"
FEATURES = ("oss-deps-dev", "oss-software-heritage")
FEATURE_SOURCES = {
    "oss-deps-dev": {"deps-dev"},
    "oss-software-heritage": {"software-heritage"},
}
BUNDLE = {
    "bundle": BUNDLE_ID,
    "version": "1.0.0",
    "manifest": "packs/oss-ecosystems/pack.json",
    "composition": "packs/oss-ecosystems/composition.json",
    "contributions": {
        "sources": [
            {
                "source": name,
                "decision": c["decision"],
                "live_verification": LIVE_VERIFICATION.get(name, {}).get(
                    "status", "not applicable"
                ),
            }
            for name, c in PROVIDER_CONTRACTS.items()
        ],
        "source_packs": [
            {
                "pack_id": "oss-ecosystems",
                "version": "1.0.0",
                "manifest": "config/source_packs/oss-ecosystems.json",
            }
        ],
        "ontology": {
            "contract": "noesis-oss-ecosystem-record-v1",
            "record_types": list(RECORD_TYPES),
        },
        "reuses": {
            "packages_and_versions": "src.domains.technical.model (package_object_id, immutable_artifact_id)",
            "registry_adapters": "src.domains.technical.registries (history parsers)",
            "advisories": "technology.vulnerabilities (cited, never copied)",
            "inventories": "src.domains.technical.inventory (observed lockfiles)",
            "identity": "src.kb.entity_history decisions",
            "monitoring": "src.kb.subscriptions",
            "acquisition": "src.ingestion.source_pack_runtime",
        },
    },
    "never": [
        "code-quality, security-posture, health, popularity or trust verdicts or scores",
        "contributor or maintainer profiling",
        "scraping of individual user or account pages",
        "duplicated vulnerability records",
        "licence-compliance or legal advice",
    ],
}


def selection(conn: Any) -> dict[str, Any]:
    """Whether the bundle is selected in the active composition plan, and which features."""

    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_name IN ('composition_authority', "
                "'composition_active', 'composition_generations', 'composition_plans')"
            ).fetchall()
        }
        if len(tables) < 4:
            return {"selected": False, "features": [], "authority": "not composed"}
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g ON g.generation_id="
            "a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1"
        ).fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never selects anything
        return {"selected": False, "features": [], "authority": "unreadable plan"}
    selected = any(p.get("id") == BUNDLE_ID for p in plan.get("packs") or [])
    return {
        "selected": selected,
        "features": sorted((plan.get("features") or {}).get(BUNDLE_ID) or []),
        "authority": "composition-coordinator",
    }


def readiness(conn: Any, namespace: str, *, scopes: set[str]) -> dict[str, Any]:
    """Per source: ready (network run), fixture-only, unavailable; live verification kept separate."""

    if READ_SCOPE not in scopes and "operator" not in scopes:
        raise OssStoreError("unauthorized", f"{READ_SCOPE} is required")
    store = OssEcosystemStore(conn, initialize=False)
    state = store.provider_state(namespace)
    chosen = selection(conn)
    sources = {}
    for name, contract in PROVIDER_CONTRACTS.items():
        if contract["decision"] != "implement":
            sources[name] = {
                "status": contract["decision"],
                "reason": contract.get("reason"),
            }
            continue
        seen = state.get(name)
        status = (
            "unavailable"
            if seen is None
            else "ready"
            if seen["last_execution"] == "network"
            else "fixture-only"
        )
        feature = contract.get("feature")
        sources[name] = {
            "status": status,
            "state": seen or {},
            "live_verification": LIVE_VERIFICATION[name],
            **(
                {"feature": feature, "feature_selected": feature in chosen["features"]}
                if feature
                else {}
            ),
        }
    return {
        "bundle": BUNDLE_ID,
        "namespace": namespace,
        **chosen,
        "features_available": list(FEATURES),
        "sources": sources,
        "generation": store.generation(namespace),
        "evidence": "offline fixture results and live results are reported separately; see LIVE_VERIFICATION",
    }


__all__ = ["BUNDLE", "BUNDLE_ID", "FEATURES", "readiness", "selection"]
