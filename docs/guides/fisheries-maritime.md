# Fisheries and Maritime Activity

From a vessel (IMO number, RFMO register number, GFW vessel id, list entry or
call sign), a flag state, a fishing area or a species to cited records:
authorised-vessel register entries with their history, IUU listings and
delistings, Global Fishing Watch fishing-effort aggregates and FAO FishStat
catch statistics - each with its source, snapshot or release, and as-of time.

Tracking issue: #2222. Source contracts, licences and bounded coverage:
[source audit](../development/fisheries-evidence/source-audit.md). Evidence:
[live and offline evidence](../development/fisheries-evidence/README.md).

## What it never does

- infer illegal fishing from movement or effort patterns, or derive an
  "illegal", "legal", "compliant" or "IUU" status for any vessel;
- recommend or imply an enforcement action;
- report a vessel without records as legal, compliant or authorised
  elsewhere - it has *none on record in the covered registers*;
- present GFW's apparent fishing effort as confirmed fishing, or combine effort
  and catch into an indicator;
- count a Combined IUU Vessel List entry as an independent confirmation of the
  RFMO listing it cites;
- store per-vessel tracks, positions or events.

## Sources (source pack `fisheries-maritime` 1.0.0)

| Source | Provider | Records | Revision model |
| --- | --- | --- | --- |
| `gfw-vessels-effort` | Global Fishing Watch (token `NOESIS_GFW_API_TOKEN`) | `vessel` identity segments; `effort_aggregate` (LOW grid, monthly, flag and gear) | dataset version |
| `fao-fishstat-capture` | FAO FishStat | `catch_observation` (FAO area, ASFIS species, country, year, quantity, unit, status flags) | release |
| `iccat-vessel-lists`, `wcpfc-vessel-lists`, `iotc-vessel-lists` | ICCAT, WCPFC, IOTC | `authorisation` (register snapshot bounded to flag states); `listing` (IUU list) | dated snapshots; removals by absence |
| `combined-iuu-vessel-list` | Combined IUU Vessel List | `listing` citing its originating RFMO listings | dated snapshot |

Every provider is `unverified-live` until the live validation (#2346).

## Workflow

1. **Acquire** - `acquire_fisheries_sources(namespace, run_key)` runs the
   explicit selection through the source-pack runtime with licence acceptance,
   budgets and receipts. Each list page is a snapshot; its snapshot date (as
   published or `Last-Modified`) and retrieval time are both kept.
2. **Identity** - `propose_fisheries_identities` records IMO matches (check
   digit verified) with evidence and offers name/flag/call-sign coincidences as
   review candidates (`review_fisheries_identity`, `revert_fisheries_identity`).
   Records are never merged; renames and re-flagging read as a time-bounded
   identity history (`fisheries_vessel_identity`). Flags resolve to country
   entities from ISO codes only; natural-person owners are never resolved.
3. **Citations** - `link_fisheries_citations` links sanctions designations
   that state the vessel's IMO (optional `sanctions` feature), published area
   codes and grid cells (optional `geospatial` feature, after
   `project_fisheries_areas`), and records of OSINT vessel movements or
   Agriculture & Food Systems that cite an IMO, ASFIS species or FAO area code.
   Anything not composed reports `provider_unavailable`.
4. **Answers** - `fisheries_vessel_status(query, as_of)` and
   `fisheries_area_aggregates(area | flag | species, period)`; pass
   `evidence_bundle=true` for a `noesis-evidence-bundle-v1` export.
5. **Monitor** - `create_fisheries_monitor(vessels, lists, areas, species)`,
   `run_fisheries_monitor`, `poll_fisheries_monitor`: dated, cited events for
   new authorisations, removals, listings, delistings and new releases,
   evaluated at committed complete source-pack runs.

## Example

```text
fisheries_vessel_status(namespace="global", query="9000027", as_of="2026-09-20")
```

returns (offline fixtures, synthetic vessel): the ICCAT authorisation whose
published period ended on 2024-12-31 (snapshot 2026-09-01), the IOTC IUU
listing of 2025-07-01 with its stated reason quoted verbatim, the Combined IUU
Vessel List entry citing that listing (`independent_confirmation: false`), the
identity history SAMPLE STAR (GHA) -> SAMPLE NOVA (TGO) with each point's
revision, the IMO matches that connected the records, and the registers and
snapshot dates consulted.

```text
fisheries_area_aggregates(namespace="global", area="34", period_from="2021-01-01", period_to="2022-12-31")
```

returns FishStat catch per country, species and year with release, unit and
status flags (e.g. `E` for an FAO estimate), release-to-release changes, and
the GHA/SKJ/2022 cell as *not published* (never zero).

## Composition

`packs/fisheries/pack.json` with `packs/fisheries/composition.json` binds the
`fisheries.core` provider (`packs/fisheries/providers/fisheries.core.json`)
plus `platform.entity-identity`, `platform.subscriptions` and
`platform.source-runtime`; the `sanctions` and `geospatial` features (default
off) bind `legal.sanctions` and `geospatial.core`. `fisheries_bundle_status`
reports each provider's contract status, readiness (ready / fixture-only /
unavailable), optional features and `LIVE_VERIFICATION` separately.
