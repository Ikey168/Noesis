"""M5.1: the planner golden fixture set. Validates that the labeled intents are
well-formed, cover every facet, name only real panel types, and that the current
heuristic planner satisfies them (the baseline the M5.2 scorer measures against)."""

import json
from pathlib import Path

import pytest

from src.genui.catalog import PANEL_TYPES
from src.genui.planner import FACET_KEYWORDS, plan

GOLDEN = Path(__file__).resolve().parents[2] / "data" / "planner_golden.json"


@pytest.fixture(scope="module")
def cases():
    return json.loads(GOLDEN.read_text())["cases"]


def test_fixtures_are_well_formed(cases):
    assert len(cases) >= 10
    for c in cases:
        assert c["intent"] and isinstance(c["intent"], str)
        assert c["facets"] and all(f in FACET_KEYWORDS for f in c["facets"])
        assert c["must_panels"]
        for panel in c["must_panels"]:
            assert panel in PANEL_TYPES, f"unknown panel type {panel!r}"


def test_every_facet_is_covered(cases):
    covered = set()
    for c in cases:
        covered.update(c["facets"])
    missing = set(FACET_KEYWORDS) - covered
    assert not missing, f"facets with no golden intent: {sorted(missing)}"


def test_current_planner_satisfies_the_golden(cases):
    """The shipped heuristic planner selects each case's primary facet and
    produces every must-panel: the baseline for the eval scorer."""
    for c in cases:
        spec = plan(c["intent"])
        top = spec.facets
        panels = {p.type for p in spec.panels if p.type != "note"}
        assert c["facets"][0] in top, f"{c['intent']!r}: {c['facets'][0]} not in {top}"
        missing = [p for p in c["must_panels"] if p not in panels]
        assert not missing, f"{c['intent']!r}: missing panels {missing}"
