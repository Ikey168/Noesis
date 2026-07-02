"""Unit tests for the Stage 0 catalog codegen (src/genui/codegen.py).

The two *_is_current tests are the staleness gate: they fail whenever
src/genui/catalog.py changes without rerunning scripts/genui/codegen.py,
so drift between the catalog and its generated mirrors is a test failure
before it is a CI failure.
"""

import json
from pathlib import Path

import pytest

from src.genui.catalog import FACETS, PANEL_CATALOG, PANEL_TYPES
from src.genui.codegen import (
    CATALOG_TS,
    REPO_ROOT,
    UI_SPEC_SCHEMA,
    _fmt_json,
    generated_files,
    main,
    render_catalog_ts,
    render_contract_schema,
    stale_files,
    write_files,
)


# ---------------------------------------------------------------------------
# Staleness gate: generated files on disk must match a fresh render.
# ---------------------------------------------------------------------------


def test_generated_catalog_ts_is_current():
    on_disk = (REPO_ROOT / CATALOG_TS).read_text(encoding="utf-8")
    assert on_disk == render_catalog_ts(), (
        "apps/web/src/genui/catalog.gen.ts is stale — "
        "run: python scripts/genui/codegen.py"
    )


def test_generated_contract_schema_is_current():
    on_disk = (REPO_ROOT / UI_SPEC_SCHEMA).read_text(encoding="utf-8")
    assert on_disk == render_contract_schema(on_disk), (
        "contracts/schemas/jsonschema/ui-spec-v1.json enums are stale — "
        "run: python scripts/genui/codegen.py"
    )


def test_repo_is_not_stale():
    assert stale_files() == []


# ---------------------------------------------------------------------------
# TypeScript rendering.
# ---------------------------------------------------------------------------


def test_ts_contains_every_panel_type_and_facet():
    ts = render_catalog_ts()
    for panel_type in PANEL_TYPES:
        assert f'| "{panel_type}"' in ts
    for facet in FACETS:
        assert f'| "{facet}"' in ts


def test_ts_marks_itself_generated():
    assert "GENERATED FILE" in render_catalog_ts().splitlines()[0]


def test_note_panel_excluded_from_client_catalog():
    ts = render_catalog_ts()
    catalog_block = ts.split("PANEL_CATALOG: PanelDef[] = [")[1]
    assert 'type: "note"' not in catalog_block
    # Every other panel is present exactly once.
    for panel in PANEL_CATALOG:
        if panel.type == "note":
            continue
        assert catalog_block.count(f'type: "{panel.type}"') == 1


def test_param_flags_rendered_only_when_set():
    lines = {
        line.split("type: ")[1].split('"')[1]: line
        for line in render_catalog_ts().splitlines()
        if line.startswith("  { type:")
    }
    assert "topicParam: true" in lines["claims"]
    assert "sourceTypeParam: true" in lines["claims"]
    assert "daysParam" not in lines["claims"]
    assert "daysParam: true" in lines["trending"]
    assert "maxDays: 30" in lines["trending"]
    for flag in ("topicParam", "sourceTypeParam", "daysParam", "maxDays"):
        assert flag not in lines["kpi_row"]


# ---------------------------------------------------------------------------
# Contract schema rendering.
# ---------------------------------------------------------------------------


def test_schema_enums_refreshed_and_rest_preserved():
    original = (REPO_ROOT / UI_SPEC_SCHEMA).read_text(encoding="utf-8")
    corrupted = json.loads(original)
    corrupted["properties"]["facets"]["items"]["enum"] = ["bogus"]
    corrupted["properties"]["panels"]["items"]["properties"]["type"]["enum"] = ["bogus"]

    rendered = json.loads(render_contract_schema(json.dumps(corrupted)))
    assert rendered["properties"]["facets"]["items"]["enum"] == list(FACETS)
    panel_type = rendered["properties"]["panels"]["items"]["properties"]["type"]
    assert panel_type["enum"] == list(PANEL_TYPES)

    # Everything not derived from the catalog passes through untouched.
    baseline = json.loads(original)
    assert rendered["properties"]["source_type"] == baseline["properties"]["source_type"]
    assert rendered["required"] == baseline["required"]
    assert rendered["additionalProperties"] is False


def test_schema_render_is_idempotent():
    original = (REPO_ROOT / UI_SPEC_SCHEMA).read_text(encoding="utf-8")
    once = render_contract_schema(original)
    assert render_contract_schema(once) == once


def test_schema_shape_change_is_a_loud_error():
    with pytest.raises(ValueError, match="expected shape"):
        render_contract_schema('{"properties": {}}')


def test_fmt_json_house_style():
    value = {
        "scalars": ["a", 1, True, None],
        "empty": {},
        "nested": [{"k": ["x", "y"]}],
    }
    assert _fmt_json(value) == (
        '{\n'
        '  "scalars": ["a", 1, true, null],\n'
        '  "empty": {},\n'
        '  "nested": [\n'
        '    {\n'
        '      "k": ["x", "y"]\n'
        '    }\n'
        '  ]\n'
        '}'
    )


# ---------------------------------------------------------------------------
# File writing and the CLI, against a throwaway repo root.
# ---------------------------------------------------------------------------


def make_repo(tmp_path: Path) -> Path:
    schema = (REPO_ROOT / UI_SPEC_SCHEMA).read_text(encoding="utf-8")
    (tmp_path / UI_SPEC_SCHEMA).parent.mkdir(parents=True)
    (tmp_path / UI_SPEC_SCHEMA).write_text(schema, encoding="utf-8")
    (tmp_path / CATALOG_TS).parent.mkdir(parents=True)
    return tmp_path


def test_stale_write_roundtrip(tmp_path):
    root = make_repo(tmp_path)
    # Missing TS file: stale. Schema copied verbatim: current.
    assert stale_files(root) == [root / CATALOG_TS]

    written = write_files(root)
    assert written == [root / CATALOG_TS]
    assert stale_files(root) == []
    assert generated_files(root)[root / CATALOG_TS] == (root / CATALOG_TS).read_text(
        encoding="utf-8"
    )

    # Rewriting a current tree touches nothing.
    assert write_files(root) == []

    # Hand-editing a generated file makes it stale again.
    (root / CATALOG_TS).write_text("// hand edit\n", encoding="utf-8")
    assert stale_files(root) == [root / CATALOG_TS]


def test_main_check_fails_then_passes(tmp_path, capsys):
    root = make_repo(tmp_path)
    assert main(["--check", "--repo-root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "stale:" in out and "codegen.py" in out

    assert main(["--repo-root", str(root)]) == 0
    assert "wrote:" in capsys.readouterr().out

    assert main(["--check", "--repo-root", str(root)]) == 0
    assert "current" in capsys.readouterr().out

    # A second write run reports there was nothing to do.
    assert main(["--repo-root", str(root)]) == 0
    assert "already current" in capsys.readouterr().out
