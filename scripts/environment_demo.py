#!/usr/bin/env python3
"""Reproducible Climate and Environment place-dossier demo for one Berlin place (Alexanderplatz).

Uses the authored offline fixtures (``tests/fixtures/environment`` and the
pinned source-pack fixtures) through the real adapters, source-pack runtime,
geospatial owners and stores. The output states that it is offline evidence;
live results are recorded separately under ``docs/development/environment-evidence``.
It infers no attribution, projection or compliance determination.

    python scripts/environment_demo.py --output docs/examples/environment-place-dossier-demo.md
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _iso(value):
    return "—" if value is None else datetime.fromtimestamp(int(value) / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


def _values(series, limit=4):
    return ", ".join(f"{v['start'][11:16] or v['start']} {v['value'] if v['value'] is not None else 'n/a'}"
                     for v in series["values"][-limit:])


def build(output):
    from src.kb.environment_places import EnvironmentDossiers
    from tests.unit.environment import harness

    env = harness.world()
    dossiers = EnvironmentDossiers(env.conn, now=env.now)
    dossier = dossiers.build(harness.NS, "demo", principal_id="alice", scopes=harness.SCOPES,
                             place_id=env.alexanderplatz["place_id"])
    replay = dossiers.replay(harness.NS, dossier["dossier_id"], scopes=harness.SCOPES, principal_id="alice")
    sections = dossier["sections"]
    lines = [
        "# Climate and Environment — place dossier demo (Berlin Alexanderplatz)",
        "",
        "> **Evidence: offline authored fixtures only.** Every provider payload comes from `tests/fixtures/environment` "
        "(hand-written in the providers' documented response shapes; identifiers are illustrative and all values are "
        "invented). They are **not** live captures and say nothing about current air quality, grid state, emissions or "
        "compliance. The dated live check (`docs/development/environment-evidence/live-check-2026-09-27.json`) could not "
        "reach any provider, so no live evidence exists yet.",
        ">",
        "> Observations, model output (reanalysis) and forecasts are listed separately and never mixed. No attribution, "
        "climate projection or compliance determination is made; no threshold is asserted.",
        "",
        f"Regenerate with `python scripts/environment_demo.py --output {output.as_posix()}` "
        f"(as of {_iso(dossier['as_of_ms'])}).",
        "",
        f"Place: **{dossier['place']['canonical_name']}** {dossier['place']['coordinates']} (place `{dossier['place']['place_id']}`), "
        f"district boundary **{dossier['boundary']['title']}** (Geoportal Berlin ALKIS fixture), bidding zone "
        f"**{dossier['bidding_zone']['name']}** (authored coarse outline). Dossier status: `{dossier['status']}`; "
        f"replay deterministic: `{replay['deterministic']}` over {len(replay['receipts'])} spatial receipts.",
        "",
        "## Air quality and weather observations (stations within Mitte)",
        "",
        "| Station | Provider | Indicator | Kind | Unit | Latest values (UTC) | Vintage status | Retrieved | Source |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for entry in sections["observations"]:
        station = entry["station"]
        for series in entry["series"]:
            lines.append(f"| {station['title']} | {station['provider']} | {series['indicator']['code']} | {series['kind']} | "
                         f"{series['unit']} | {_values(series)} | {series['as_of']['status']} | "
                         f"{_iso(series['as_of']['retrieved_at_ms'])} | [link]({series['source_url']}) |")
        for link in entry["links"]:
            lines.append(f"| ↳ linked, not merged: {link['basis']} ({link['evidence'].get('distance_m')} m) | | | | | | | | |")
    lines += ["", "## Model output (reanalysis) for the place's grid cell", "",
              "| Series | Model | Grid | Kind | Latest values | Source |", "| --- | --- | --- | --- | --- | --- |"]
    for series in sections["model_output"]:
        model = series["model"]
        lines.append(f"| {series['indicator']['code']} ({series['unit']}) | {model['name']} | {model['grid_resolution_m']} m, "
                     f"{series['grid_cell']['distance_m']:.0f} m away | {series['kind']} | {_values(series)} | [link]({series['source_url']}) |")
    lines += ["", "## Forecasts", "", "| Series | Model | Issue time | Kind | Values | Source |",
              "| --- | --- | --- | --- | --- | --- |"]
    for series in sections["forecasts"]:
        model = series["model"]
        lines.append(f"| {series['indicator']['code']} ({series['unit']}) | {model['name']} | {model['issue_time']} | "
                     f"{series['kind']} | {_values(series)} | [link]({series['source_url']}) |")
    lines += ["", f"## Grid records for bidding zone {dossier['bidding_zone']['name']}", "",
              "| Record | Provider | Type | Kind | Detail | As of | Source |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for event in sections["grid"]:
        if event["event_type"] == "unavailability":
            u = event["unavailability"]
            detail = (f"{u['kind']}, {u['resource'].get('unit_name')} {u['nominal_capacity']} MW nominal, "
                      f"{u['start']} → {u['end']}; reason: {'; '.join((r['text'] or r['code']) for r in u['reason'])}")
        else:
            detail = f"{event.get('resolution')}, {event['unit']}: {', '.join(str(v['value']) for v in event['values'][:3])} …"
        lines.append(f"| {event['title']} | {event['provider']} | {event['event_type']} | {event['kind']} | {detail} | "
                     f"{_iso(event['as_of']['observed_at_ms'])} | [link]({event['source_url']}) |")
    lines += ["", "## Facilities near the place (5 km)", ""]
    for facility in sections["facilities"]:
        lines += [f"### {facility['title']} — {facility['distance_m']:.0f} m", "",
                  f"- Source: [{facility['provider']}]({facility['source_url']}); operator as published: "
                  f"*{facility['operator']['source_name']}* ({facility['operator']['state']})",
                  f"- Activity: {', '.join(a.get('label') or a.get('code') for a in facility['activities'])}",
                  f"- Permits (as published): {', '.join(p.get('url') or p.get('permit_id') for p in facility['permits']) or 'none published'}"]
        for release in facility["releases"]:
            value = release["values"][-1]
            lines.append(f"- Release {release['indicator']['code']} to {release['indicator'].get('medium')}: {value['value']} "
                         f"{value['unit']} ({value['start'][:4]}, method {value['flags'].get('method')}, kind {release['kind']})")
        for linked in facility["linked_records"]:
            for series in linked["series"]:
                value = series["values"][-1]
                compliance = value["flags"].get("compliance") or {}
                lines.append(f"- EU ETS {linked['title']}: {series['indicator']['code']} {value['value']} {value['unit']} "
                             f"({value['start'][:4]}); compliance code as published: {compliance.get('code')}")
        lines += [f"- {facility['notice']}", ""]
    lines += ["## Umweltatlas layers containing the place", "", "| Layer | Feature | Properties (as published) |",
              "| --- | --- | --- |"]
    for layer in sections["layers"]:
        props = {k: v for k, v in layer["properties"].items() if k != "gml_id"}
        lines.append(f"| {layer['collection']} | {layer['title']} | {props} |")
    lines += ["", "## Coverage gaps and not implemented", ""]
    lines += [f"- {gap['section']}: {gap['reason']}" for gap in dossier["coverage_gaps"]] or ["- none in this offline dossier"]
    lines += [f"- {provider}: {reason}" for provider, reason in dossier["not_implemented"].items()]
    lines += ["", "## Provider state (offline vs live)", "", "| Provider | This dossier's evidence | Live verification |",
              "| --- | --- | --- |"]
    from src.ingestion.environment_providers import LIVE_VERIFICATION

    for provider, state in sorted(dossier["providers"].items()):
        live = LIVE_VERIFICATION[provider]
        lines.append(f"| {provider} | injected fixture ({state['last_execution']}) | {live['status']} "
                     f"({live.get('checked_at', '—')}, {live.get('failure_type', live.get('note', ''))[:40]}) |")
    lines.append("")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines))
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(build(parser.parse_args().output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
