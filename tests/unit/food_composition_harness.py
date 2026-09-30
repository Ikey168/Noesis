"""Offline harness for the Products food feature (#2216): synthetic fixtures through the real runtime.

Reuses the Products safety harness deployment (the ``products-displays`` pack
installed with licences accepted) and runs the three food sources with
operation ``food``; RASFF notices come from the pinned safety fixture.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from src.kb.food_composition import FoodCompositionStore
from tests.unit import product_safety_harness as safety

ROOT = Path(__file__).resolve().parents[2]
NS = safety.NS
READ = safety.READ
WRITE = safety.WRITE
REVIEW = safety.REVIEW
ALL = safety.ALL
FOOD_SOURCES = ["off-food-products", "fdc-foods", "ciqual-composition"]
FIXTURES = {
    "off-food-products": ROOT / "tests/fixtures/source_packs/products-food-off.json",
    "fdc-foods": ROOT / "tests/fixtures/source_packs/products-food-fdc.json",
    "ciqual-composition": ROOT / "tests/fixtures/source_packs/products-food-ciqual.json",
}
KEBAB, YOGHURT, BAR, UNKNOWN = "4000000000105", "4000000000150", "0071000000208", "4000000000204"


def pages(source_id: str) -> list[dict]:
    return copy.deepcopy(json.loads(FIXTURES[source_id].read_text())["native_pages"])


def source(source_id: str) -> dict:
    return safety.source(source_id)


class Env(safety.Env):
    def __init__(self, conn=None) -> None:
        super().__init__(conn)
        self.food = FoodCompositionStore(self.conn, now=lambda: next(self.clock))

    def run_food(self, key: str = "food-1", *, adapters=None, source_ids=None, fault=None) -> dict:
        return self.run(key, operation="food", source_ids=source_ids or FOOD_SOURCES, adapters=adapters,
                        fault=fault)

    def compiled_food(self, source_id: str, native_pages: list[dict]):
        from src.ingestion.food_composition_sources import fixture_transport

        installed = self.runtime._manifest(self.value["pack_id"])[0]
        return self.runtime.factory.compile(safety.source(source_id, installed),
                                            transport=fixture_transport(native_pages),
                                            secret="fixture-credential")

    def food_id(self, provider: str, key: str) -> str:
        return self.food.resolve_food(NS, f"{provider}:{key}")
