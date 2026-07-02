"""
Stage 0 codegen — kill the hand-written mirrors (MCP rearchitecture plan, R0).

``catalog.py`` is the single source of truth for panel types, facets and
per-panel layout/param metadata. This module renders every artifact that
previously mirrored it by hand:

* ``apps/web/src/genui/catalog.gen.ts`` — the ``PanelType``/``Facet``
  unions and the client ``PANEL_CATALOG`` used by the offline planner;
* ``contracts/schemas/jsonschema/ui-spec-v1.json`` — only the panel-type
  and facet enums; the rest of the schema (including the ``source_type``
  enum, which comes from the planner's vocabulary, not the catalog) is
  hand-maintained and preserved verbatim.

Run ``python scripts/genui/codegen.py`` after editing the catalog. CI runs
the same script with ``--check``, which exits non-zero when a generated
file is stale, and ``tests/unit/genui/test_codegen.py`` enforces the same
inside the test gate.

Stdlib-only on purpose, like the rest of src/genui.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.genui.catalog import FACETS, PANEL_CATALOG, PANEL_TYPES, PanelDef

REPO_ROOT = Path(__file__).resolve().parents[2]

CATALOG_TS = Path("apps/web/src/genui/catalog.gen.ts")
UI_SPEC_SCHEMA = Path("contracts/schemas/jsonschema/ui-spec-v1.json")

_TS_HEADER = """\
// GENERATED FILE — DO NOT EDIT.
//
// Rendered from src/genui/catalog.py (the single source of truth for panel
// types, facets and layout/param metadata) by src/genui/codegen.py.
// Regenerate with:  python scripts/genui/codegen.py
// CI fails while this file is stale (tests/unit/genui/test_codegen.py).
"""


def _ts_union(name: str, values) -> str:
    """Render a TS string-literal union, one member per line."""
    lines = [f"export type {name} ="]
    lines.extend(f'  | "{v}"' for v in values)
    lines[-1] += ";"
    return "\n".join(lines)


def _ts_entry(panel: PanelDef) -> str:
    """Render one client-catalog entry (param flags only when set)."""
    parts = [
        f"type: {json.dumps(panel.type)}",
        f"title: {json.dumps(panel.title)}",
        f"facets: [{', '.join(json.dumps(f) for f in panel.facets)}]",
        f"defaultSpan: {panel.default_span}",
    ]
    if panel.topic_param:
        parts.append("topicParam: true")
    if panel.source_type_param:
        parts.append("sourceTypeParam: true")
    if panel.days_param:
        parts.append("daysParam: true")
    if panel.max_days is not None:
        parts.append(f"maxDays: {panel.max_days}")
    return "  { " + ", ".join(parts) + " },"


def render_catalog_ts() -> str:
    """Render apps/web/src/genui/catalog.gen.ts from the backend catalog."""
    entries = "\n".join(_ts_entry(p) for p in PANEL_CATALOG if p.type != "note")
    return f"""{_TS_HEADER}
{_ts_union("PanelType", PANEL_TYPES)}

{_ts_union("Facet", FACETS)}

// Client-side view of the backend panel catalog: which facets each panel
// serves and how it takes its filters. Used by the offline fallback planner
// and to render pinned panels the spec omitted. The "note" panel is composed
// by the planners directly and is never selected from the catalog.
export interface PanelDef {{
  type: PanelType;
  title: string;
  facets: Facet[];
  defaultSpan: number;
  topicParam?: boolean;
  sourceTypeParam?: boolean;
  daysParam?: boolean;
  // Upper bound of the endpoint's days validator (mirrors catalog.py).
  maxDays?: number;
}}

export const PANEL_CATALOG: PanelDef[] = [
{entries}
];

export const PANEL_DEFS: Partial<Record<PanelType, PanelDef>> = Object.fromEntries(
  PANEL_CATALOG.map((p) => [p.type, p]),
);
"""


def _is_scalar(value: Any) -> bool:
    return not isinstance(value, (dict, list))


def _fmt_json(value: Any, indent: int = 0) -> str:
    """Dump JSON in the contract house style: 2-space indent, arrays of
    scalars kept on one line (matching the hand-maintained schema files, so
    regeneration only touches lines whose content actually changed)."""
    pad = "  " * indent
    child = "  " * (indent + 1)
    if isinstance(value, dict):
        if not value:
            return "{}"
        items = [
            f"{child}{json.dumps(k, ensure_ascii=False)}: {_fmt_json(v, indent + 1)}"
            for k, v in value.items()
        ]
        return "{\n" + ",\n".join(items) + f"\n{pad}}}"
    if isinstance(value, list):
        if all(_is_scalar(v) for v in value):
            return "[" + ", ".join(json.dumps(v, ensure_ascii=False) for v in value) + "]"
        items = [f"{child}{_fmt_json(v, indent + 1)}" for v in value]
        return "[\n" + ",\n".join(items) + f"\n{pad}]"
    return json.dumps(value, ensure_ascii=False)


def render_contract_schema(schema_text: str) -> str:
    """Return the ui-spec-v1 contract with its catalog-derived enums
    refreshed; every other part of the schema passes through untouched."""
    schema = json.loads(schema_text)
    try:
        schema["properties"]["facets"]["items"]["enum"] = list(FACETS)
        panel_props = schema["properties"]["panels"]["items"]["properties"]
        panel_props["type"]["enum"] = list(PANEL_TYPES)
    except (KeyError, TypeError) as exc:
        raise ValueError(
            f"ui-spec-v1 schema no longer has the expected shape ({exc}); "
            "update src/genui/codegen.py alongside the schema"
        ) from exc
    return _fmt_json(schema) + "\n"


def generated_files(repo_root: Optional[Path] = None) -> Dict[Path, str]:
    """Map of generated file path -> expected content."""
    root = repo_root or REPO_ROOT
    schema_path = root / UI_SPEC_SCHEMA
    return {
        root / CATALOG_TS: render_catalog_ts(),
        schema_path: render_contract_schema(schema_path.read_text(encoding="utf-8")),
    }


def stale_files(repo_root: Optional[Path] = None) -> List[Path]:
    """Generated files whose on-disk content is missing or out of date."""
    return [
        path
        for path, content in generated_files(repo_root).items()
        if not path.exists() or path.read_text(encoding="utf-8") != content
    ]


def write_files(repo_root: Optional[Path] = None) -> List[Path]:
    """Write stale generated files; returns the paths that changed."""
    written = []
    for path, content in generated_files(repo_root).items():
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8")
            written.append(path)
    return written


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate the genui catalog mirrors from src/genui/catalog.py",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if any generated file is stale, without writing",
    )
    parser.add_argument("--repo-root", type=Path, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.check:
        stale = stale_files(args.repo_root)
        if stale:
            for path in stale:
                print(f"stale: {path}")
            print("Regenerate with: python scripts/genui/codegen.py")
            return 1
        print("generated genui artifacts are current")
        return 0

    written = write_files(args.repo_root)
    for path in written:
        print(f"wrote: {path}")
    if not written:
        print("generated genui artifacts already current")
    return 0
