"""
Noesis generative-UI engine (issue: adaptive, generative UI).

Turns a natural-language intent into a validated ``ui-spec-v1`` document —
a JSON layout of panels the web frontend renders from its component
registry. Planning is heuristic by default (always available, no model or
API key required) and can be upgraded to an LLM planner when a provider
key is configured; the spec shape is identical either way.

The spec is adaptive on three axes:

* **data availability** — panels whose warehouse tables are empty are
  dropped or demoted (``adaptivity.data_availability``);
* **domain packs** — panels gated by a pack ``ui_flag`` disappear when the
  pack is disabled (``adaptivity.merged_ui_flags``);
* **usage signals** — client-reported pins/dismissals/interaction weights
  re-rank and filter panels (``adaptivity.apply_signals``).
"""

from src.genui.catalog import PANEL_CATALOG, PANEL_TYPES, panel_catalog_dict
from src.genui.spec import PanelSpec, UISpec, SPEC_VERSION, validate_spec
from src.genui.planner import plan
from src.genui.adaptivity import (
    apply_signals,
    data_availability,
    merged_ui_flags,
)

__all__ = [
    "PANEL_CATALOG",
    "PANEL_TYPES",
    "panel_catalog_dict",
    "PanelSpec",
    "UISpec",
    "SPEC_VERSION",
    "validate_spec",
    "plan",
    "apply_signals",
    "data_availability",
    "merged_ui_flags",
]
