"""The Linguistics bundle: optional features and per-provider readiness (LG11, #2189).

Composed under the pack/workflow composition contracts
(``docs/architecture/pack-workflow-composition.md``). ``packs/linguistics``
contains:

* the v1 manifest ``pack.json``;
* the overlay ``composition.json``;
* three provider descriptors: ``linguistics.lexicon`` (the record owner),
  ``linguistics.languoids`` and ``linguistics.typology``.

The bundle composes the shared ``platform.cross-language`` (aliases,
translations, search), ``platform.entity-identity``,
``platform.subscriptions``, ``platform.source-acquisition``,
``geospatial.place-resolution`` and ``science.literature-claims`` providers.
It adds no scheduler, permission ledger, alias store, translation store,
entity store or spatial store.

Two optional features are off by default: ``linguistics-wiktionary``
(Wiktionary content, gated by CC BY-SA share-alike duties) and
``linguistics-typology`` (WALS values). Composition feature ids cannot contain
underscores, so the issue's ``linguistics_wiktionary`` and
``linguistics_typology`` are written with hyphens. A feature is a selection
change through the lifecycle coordinator. :func:`feature_enabled` reads the
active plan, and an unreadable plan never enables a feature.
"""

from __future__ import annotations

import json
from typing import Any

from src.ingestion.linguistics_sources import LIVE_VERIFICATION, PROVIDER_CONTRACTS
from src.kb.linguistics_records import READ_SCOPE, authorize
from src.kb.linguistics_store import LinguisticsStore

BUNDLE_ID = "linguistics"
FEATURES = ("linguistics-wiktionary", "linguistics-typology")
FEATURE_PROVIDERS = {
    "linguistics-wiktionary": ("kaikki-wiktextract",),
    "linguistics-typology": ("wals",),
}
EXCLUSIONS = (
    "machine-translated or model-generated definitions, glosses or etymologies presented as sourced",
    "speaker personal data (recordings of identifiable speakers, informant names, contributor profiles)",
    "language-status, endangerment or correctness verdicts beyond quoting the source",
    "mirroring of full dumps beyond the bounded, licensed subsets",
)


def feature_enabled(conn: Any, feature: str) -> bool:
    """Whether a Linguistics optional feature is selected in the active composition plan (default off)."""
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_name IN "
                "('composition_authority', 'composition_active', 'composition_generations', 'composition_plans')"
            ).fetchall()
        }
        if len(tables) < 4:
            return False
        managed = conn.execute(
            "SELECT authority FROM composition_authority WHERE bundle=?", [BUNDLE_ID]
        ).fetchone()
        if not managed or managed[0] != "composition":
            return False
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g "
            "ON g.generation_id=a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1"
        ).fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return False
    return feature in ((plan.get("features") or {}).get(BUNDLE_ID) or [])


def readiness(conn: Any, namespace: str, *, scopes: Any) -> dict[str, Any]:
    """Per-provider acquisition state, the selected features and the live-verification status (offline only)."""
    authorize(namespace, set(scopes), READ_SCOPE)
    store = LinguisticsStore(conn, initialize=False)
    acquired = store.providers(namespace)
    providers = {}
    for provider, contract in PROVIDER_CONTRACTS.items():
        record_provider = {
            "wikidata-lexemes": ("wikidata-lexemes", "wikidata-items")
        }.get(provider, (provider,))
        state = [acquired[p] for p in record_provider if p in acquired]
        providers[provider] = {
            "access_decision": contract["access_decision"],
            "live_verification": LIVE_VERIFICATION[provider]["status"],
            "state": "acquired"
            if state
            else (
                "not_acquired"
                if contract["access_decision"] == "unverified-live"
                else contract["access_decision"]
            ),
            "sightings": sum(s["sightings"] for s in state),
            "releases": sorted({r for s in state for r in s["revisions"]})[-5:],
        }
    snapshot = store.snapshot(namespace)
    return {
        "bundle": BUNDLE_ID,
        "namespace": namespace,
        "features": {feature: feature_enabled(conn, feature) for feature in FEATURES},
        "feature_sources": {k: list(v) for k, v in FEATURE_PROVIDERS.items()},
        "providers": providers,
        "snapshot_id": snapshot["snapshot_id"],
        "n": snapshot["sightings"],
        "exclusions": list(EXCLUSIONS),
        "evidence": {
            "offline": "tests/unit/domains/test_linguistics_acceptance.py",
            "live": "docs/development/linguistics-evidence/ (none recorded yet)",
        },
    }
