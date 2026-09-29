"""The Weather bundle's composition state: optional features and their gates (WX12, #2175).

The composition lifecycle coordinator (``src/composition/lifecycle.py``) is the
only enabled-state authority. This module only reads the active plan:

* ``weather-open-meteo`` (default off) gates Open-Meteo acquisition. The free
  Open-Meteo API is for non-commercial use (WX01). When the Weather bundle is
  composition-managed and the feature is not selected, the projector refuses
  Open-Meteo pages. Outside a composed deployment, the source-pack licence
  acceptance is the gate.
* ``weather-verification`` (default off) gates recording verification runs,
  under the same rule.

Feature ids are hyphenated because composition feature ids forbid underscores.
"""

from __future__ import annotations

import json
from typing import Any

BUNDLE = "weather"
FEATURES = ("weather-open-meteo", "weather-verification")


def _plan(conn: Any) -> tuple[bool, dict[str, Any]]:
    """(composition-managed, active plan) — an unreadable plan is managed-with-no-features."""

    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_name IN "
                "('composition_authority', 'composition_active', 'composition_generations', 'composition_plans')"
            ).fetchall()
        }
        if len(tables) < 4:
            return False, {}
        managed = conn.execute(
            "SELECT authority FROM composition_authority WHERE bundle=?", [BUNDLE]
        ).fetchone()
        if not managed or managed[0] != "composition":
            return False, {}
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g ON g.generation_id="
            "a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1"
        ).fetchone()
        return True, json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return True, {}


def feature_enabled(conn: Any, feature: str) -> bool:
    """Whether the feature is selected in the active composition plan (default off)."""

    managed, plan = _plan(conn)
    return managed and feature in ((plan.get("features") or {}).get(BUNDLE) or [])


def feature_allowed(conn: Any, feature: str) -> bool:
    """Selected in the plan, or the bundle is not composition-managed here (the licence gate then applies)."""

    managed, _ = _plan(conn)
    return feature_enabled(conn, feature) or not managed


def require_feature(conn: Any, feature: str) -> None:
    from src.kb.weather_store import WeatherError

    if not feature_allowed(conn, feature):
        raise WeatherError(
            "feature_disabled",
            f"the optional {feature} feature of the Weather bundle is not selected",
        )
