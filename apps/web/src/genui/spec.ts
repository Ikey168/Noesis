// ui-spec-v1 — the wire format between the backend planner (src/genui) and
// this renderer. Mirrors contracts/schemas/jsonschema/ui-spec-v1.json.
//
// The panel/facet unions and the client catalog are generated from
// src/genui/catalog.py into catalog.gen.ts — edit the Python catalog and run
// `python scripts/genui/codegen.py`, never the generated file.

import type { Facet, PanelType } from "./catalog.gen";

export type { Facet, PanelDef, PanelType } from "./catalog.gen";
export { PANEL_CATALOG, PANEL_DEFS } from "./catalog.gen";

export type GeneratedBy = "heuristic" | "llm" | "client";

export interface PanelParams {
  topic?: string;
  source_type?: string;
  days?: number;
  [key: string]: string | number | boolean | undefined;
}

export interface PanelSpec {
  id: string;
  type: PanelType;
  title: string;
  span: number;
  priority: number;
  rationale?: string;
  endpoint?: string | null;
  params?: PanelParams;
  body?: string;
}

export interface UISpec {
  spec_version: "ui-spec-v1";
  intent: string;
  title: string;
  subtitle?: string;
  generated_by: GeneratedBy;
  facets?: Facet[];
  topic?: string | null;
  source_type?: string | null;
  panels: PanelSpec[];
}
