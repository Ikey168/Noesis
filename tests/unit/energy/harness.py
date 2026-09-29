"""Offline harness for the Energy Systems tests: acquire authored fixtures through the real adapters."""

from __future__ import annotations

from src.ingestion import energy_sources as es
from tests.unit.energy import fixture_builder as fb

NS = "energy"
SCOPES = {"knowledge:energy:read", "knowledge:energy:write", "knowledge:energy:review", f"namespace:{NS}:read",
          f"namespace:{NS}:write", "knowledge:subscriptions:read", "knowledge:subscriptions:write",
          f"namespace:{NS}:admin"}
SELECTIONS = {
    "energy-entsoe": ("entsoe", fb.ENTSOE_SELECTION),
    "energy-eia-fuel-type": ("eia", fb.EIA_SELECTIONS["fuel-type-data"]),
    "energy-eia-region": ("eia", fb.EIA_SELECTIONS["region-data"]),
    "energy-eia-interchange": ("eia", fb.EIA_SELECTIONS["interchange-data"]),
    "energy-eia-capacity": ("eia", fb.EIA_SELECTIONS["operating-generator-capacity"]),
    "energy-ember-generation": ("ember", fb.EMBER_SELECTIONS["electricity-generation/monthly"]),
    "energy-ember-demand": ("ember", fb.EMBER_SELECTIONS["electricity-demand/monthly"]),
    "energy-ember-capacity": ("ember", fb.EMBER_SELECTIONS["installed-capacity/yearly"]),
    "energy-eurostat-balances": ("eurostat", fb.EUROSTAT_SELECTION),
    "energy-charts-public-power": ("energy-charts", fb.ENERGY_CHARTS_SELECTION),
    "entsoe_revision_2": ("entsoe", fb.ENTSOE_SELECTION),
    "eia_fuel_type_revised": ("eia", fb.EIA_SELECTIONS["fuel-type-data"]),
    "ember_generation_release_2": ("ember", {**fb.EMBER_SELECTIONS["electricity-generation/monthly"],
                                             "release": {"label": "Ember monthly electricity data 2026-09",
                                                         "published_on": "2026-09-26"}}),
    "eurostat_balances_later": ("eurostat", fb.EUROSTAT_SELECTION),
}


def acquire(conn, name, *, namespace=NS, scopes=SCOPES, secret=es.FIXTURE_SECRET, selection=None):
    provider, default = SELECTIONS[name]
    fixture = fb.load(name)
    return es.acquire(conn, provider, selection or default, namespace=namespace, scopes=scopes, principal_id="analyst",
                      fetch=es.fixture_transport(fixture["native_pages"]), secret=secret,
                      now=es.fixture_clock(fixture))


def acquire_all(conn, names=None, **kwargs):
    names = names or [n for n in SELECTIONS if n.startswith("energy-")]
    return {name: acquire(conn, name, **kwargs) for name in names}
