"""Unit tests for the ui-spec-v1 document model (src/genui/spec.py)."""

import json
from pathlib import Path

import pytest

from src.genui.catalog import PANEL_CATALOG, PANEL_TYPES, get_panel_def, panel_catalog_dict
from src.genui.planner import plan
from src.genui.spec import (
    MAX_PANELS,
    SPEC_VERSION,
    spec_from_dict,
    validate_spec,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = REPO_ROOT / "contracts" / "schemas" / "jsonschema" / "ui-spec-v1.json"
EXAMPLES_DIR = REPO_ROOT / "contracts" / "examples" / "ui-spec-v1"

VALID_FIXTURES = sorted((EXAMPLES_DIR / "valid").glob("*.json"))
INVALID_FIXTURES = sorted((EXAMPLES_DIR / "invalid").glob("*.json"))


def _load(path):
    with open(path) as fh:
        return json.load(fh)


def make_panel(**overrides):
    panel = {
        "id": "p1",
        "type": "note",
        "title": "Plan",
        "span": 12,
        "priority": 1.0,
        "rationale": "",
        "endpoint": None,
        "params": {},
        "body": "how this canvas was assembled",
    }
    panel.update(overrides)
    return panel


def make_spec(**overrides):
    spec = {
        "spec_version": SPEC_VERSION,
        "intent": "",
        "title": "Test Canvas",
        "subtitle": "",
        "generated_by": "heuristic",
        "facets": ["overview"],
        "topic": None,
        "source_type": None,
        "panels": [make_panel()],
    }
    spec.update(overrides)
    return spec


class TestValidateSpecAccepts:
    @pytest.mark.parametrize(
        "intent",
        [
            "",
            "compare outlet framing on climate policy this month",
            "who disagrees about AI regulation",
            "sentiment in blogs last 14 days",
        ],
    )
    def test_planner_output_validates(self, intent):
        assert validate_spec(plan(intent).to_dict()) == []

    @pytest.mark.parametrize("path", VALID_FIXTURES, ids=lambda p: p.name)
    def test_valid_contract_fixtures(self, path):
        assert validate_spec(_load(path)) == []

    def test_synthetic_baseline_spec_validates(self):
        assert validate_spec(make_spec()) == []

    def test_endpoint_matching_catalog_is_accepted(self):
        pdef = get_panel_def("articles")
        spec = make_spec(
            panels=[make_panel(type="articles", title="Docs", span=6, endpoint=pdef.endpoint)]
        )
        assert validate_spec(spec) == []


class TestValidateSpecRejects:
    @pytest.mark.parametrize("path", INVALID_FIXTURES, ids=lambda p: p.name)
    def test_invalid_contract_fixtures(self, path):
        assert validate_spec(_load(path)) != []

    @pytest.mark.parametrize("bad", [None, [], "spec", 42])
    def test_non_dict_spec(self, bad):
        assert validate_spec(bad) == ["spec must be an object"]

    def test_wrong_spec_version(self):
        errors = validate_spec(make_spec(spec_version="ui-spec-v2"))
        assert any("spec_version" in e for e in errors)

    def test_empty_panels(self):
        errors = validate_spec(make_spec(panels=[]))
        assert errors == ["panels must be a non-empty list"]

    def test_panels_not_a_list(self):
        errors = validate_spec(make_spec(panels="nope"))
        assert errors == ["panels must be a non-empty list"]

    def test_too_many_panels(self):
        panels = [
            make_panel(id=f"p{i}", type="articles", title="Docs", span=6, endpoint=None)
            for i in range(MAX_PANELS + 1)
        ]
        errors = validate_spec(make_spec(panels=panels))
        assert any(f"at most {MAX_PANELS}" in e for e in errors)

    def test_unknown_panel_type(self):
        spec = make_spec(panels=[make_panel(type="hologram")])
        errors = validate_spec(spec)
        assert any("not in the panel catalog" in e for e in errors)

    @pytest.mark.parametrize("span", [2, 13, 0, -1, "6", 6.0, None])
    def test_span_out_of_range(self, span):
        spec = make_spec(panels=[make_panel(span=span)])
        errors = validate_spec(spec)
        assert any("span" in e for e in errors)

    @pytest.mark.parametrize("priority", [-0.1, 1.5, "high", None])
    def test_priority_out_of_range(self, priority):
        spec = make_spec(panels=[make_panel(priority=priority)])
        errors = validate_spec(spec)
        assert any("priority" in e for e in errors)

    def test_priority_boundaries_accepted(self):
        spec = make_spec(
            panels=[make_panel(id="p1", priority=0), make_panel(id="p2", priority=1.0)]
        )
        assert validate_spec(spec) == []

    def test_duplicate_panel_ids(self):
        spec = make_spec(panels=[make_panel(id="p1"), make_panel(id="p1")])
        errors = validate_spec(spec)
        assert any("duplicated" in e for e in errors)

    @pytest.mark.parametrize("pid", ["", None, 7])
    def test_bad_panel_id(self, pid):
        spec = make_spec(panels=[make_panel(id=pid)])
        errors = validate_spec(spec)
        assert any(".id" in e for e in errors)

    def test_panel_entry_not_a_dict(self):
        errors = validate_spec(make_spec(panels=["junk"]))
        assert errors == ["panels[0] must be an object"]

    def test_endpoint_mismatching_catalog(self):
        spec = make_spec(
            panels=[make_panel(type="articles", title="Docs", span=6, endpoint="/wrong/path")]
        )
        errors = validate_spec(spec)
        assert any("does not match the catalog" in e for e in errors)

    def test_endpoint_not_string_or_null(self):
        spec = make_spec(panels=[make_panel(endpoint=42)])
        errors = validate_spec(spec)
        assert any("endpoint must be null or a string" in e for e in errors)

    def test_params_not_a_dict(self):
        spec = make_spec(panels=[make_panel(params="topic=x")])
        errors = validate_spec(spec)
        assert any("params must be an object" in e for e in errors)

    @pytest.mark.parametrize("generated_by", ["robot", "", None])
    def test_bad_generated_by(self, generated_by):
        errors = validate_spec(make_spec(generated_by=generated_by))
        assert any("generated_by" in e for e in errors)

    @pytest.mark.parametrize("facets", [["overview", "vibes"], "overview", 42])
    def test_bad_facets(self, facets):
        errors = validate_spec(make_spec(facets=facets))
        assert any("facets" in e for e in errors)

    def test_bad_source_type(self):
        errors = validate_spec(make_spec(source_type="carrier-pigeon"))
        assert any("source_type" in e for e in errors)

    def test_null_source_type_is_valid(self):
        assert validate_spec(make_spec(source_type=None)) == []

    def test_overlong_intent(self):
        errors = validate_spec(make_spec(intent="x" * 501))
        assert any("500" in e for e in errors)

    def test_non_string_intent(self):
        errors = validate_spec(make_spec(intent=42))
        assert any("intent must be a string" in e for e in errors)

    @pytest.mark.parametrize("title", ["", None])
    def test_bad_title(self, title):
        errors = validate_spec(make_spec(title=title))
        assert any("title" in e for e in errors)

    @pytest.mark.parametrize("title", ["", None])
    def test_bad_panel_title(self, title):
        errors = validate_spec(make_spec(panels=[make_panel(title=title)]))
        assert any("panels[0].title" in e for e in errors)


class TestSpecFromDict:
    def test_missing_ids_get_positional_defaults(self):
        spec = spec_from_dict(
            {"panels": [{"type": "note", "title": "Plan"}, {"type": "articles"}]}
        )
        assert [p.id for p in spec.panels] == ["p1", "p2"]

    def test_junk_panels_skipped(self):
        spec = spec_from_dict(
            {"panels": ["junk", 42, None, {"type": "articles", "title": "Docs"}]}
        )
        assert len(spec.panels) == 1
        assert spec.panels[0].type == "articles"
        # id defaults come from the raw list position, not the kept position
        assert spec.panels[0].id == "p4"

    def test_non_dict_params_replaced_with_empty_dict(self):
        spec = spec_from_dict({"panels": [{"type": "claims", "params": "topic=x"}]})
        assert spec.panels[0].params == {}

    def test_top_level_defaults(self):
        spec = spec_from_dict({})
        assert spec.intent == ""
        assert spec.generated_by == "heuristic"
        assert spec.spec_version == SPEC_VERSION
        assert spec.facets == []
        assert spec.panels == []

    def test_roundtrip_of_planner_output_still_validates(self):
        original = plan("compare outlet framing on climate policy").to_dict()
        rebuilt = spec_from_dict(original).to_dict()
        assert validate_spec(rebuilt) == []
        assert rebuilt["intent"] == original["intent"]
        assert [p["type"] for p in rebuilt["panels"]] == [
            p["type"] for p in original["panels"]
        ]


class TestCatalog:
    def test_panel_types_unique_and_match_catalog(self):
        assert len(PANEL_TYPES) == len(set(PANEL_TYPES))
        assert PANEL_TYPES == tuple(p.type for p in PANEL_CATALOG)
        assert "note" in PANEL_TYPES

    def test_get_panel_def(self):
        assert get_panel_def("articles").endpoint == "/api/v1/news/articles"
        assert get_panel_def("hologram") is None

    def test_panel_catalog_dict_shape(self):
        catalog = panel_catalog_dict()
        assert [entry["type"] for entry in catalog] == list(PANEL_TYPES)
        for entry in catalog:
            assert set(entry) == {
                "type",
                "title",
                "description",
                "endpoint",
                "facets",
                "tables",
                "ui_flag",
                "default_span",
                "topic_param",
                "source_type_param",
                "days_param",
                "max_days",
            }
            assert isinstance(entry["facets"], list)
            assert isinstance(entry["tables"], list)


class TestJsonschemaParity:
    def test_python_validator_agrees_with_draft7_on_all_fixtures(self):
        jsonschema = pytest.importorskip("jsonschema")
        validator = jsonschema.Draft7Validator(_load(SCHEMA_PATH))
        fixtures = VALID_FIXTURES + INVALID_FIXTURES
        assert fixtures, "no contract fixtures found"
        for path in fixtures:
            data = _load(path)
            schema_ok = validator.is_valid(data)
            python_ok = validate_spec(data) == []
            assert schema_ok == python_ok, path.name
