# Climate and Environment pack guide

From a place to a cited environmental dossier, offline or live. Architecture and
module ownership: [subsystem page](../subsystems/climate-environment.md).

## The observation / model / forecast boundary

| Kind | Meaning | Examples in this pack | Shown as |
| --- | --- | --- | --- |
| `observation` | measured or reported by the publisher | OpenAQ and UBA station values, DWD station values, ENTSO-E/SMARD realised generation and actual load, E-PRTR releases, ETS verified emissions | dossier section `observations`, grid records with kind `observation`, facility releases |
| `model` | model output, including reanalysis | Open-Meteo ERA5 archive | section `model_output` with model name and grid resolution |
| `forecast` | forecast with an issue time | Open-Meteo ICON-D2, ENTSO-E day-ahead load, SMARD forecast load | section `forecasts` / grid records with `kind_notice` and issue time |

Kind is mandatory at record construction and cannot be defaulted; a dataset that
only publishes model output or forecasts cannot produce an observation; a stored
series never changes kind; thresholds are compared with observations only.
Umweltatlas layers are map products (some computed, e.g. noise and climate
maps) and are listed as layers, not observations.

## Journey

1. **Contracts and live state.** `environment_provider_contracts` lists every
   source's terms, auth, limits, kinds, identifiers, units, CRS and
   `LIVE_VERIFICATION`.
2. **Install sources.** Install `packs/climate-environment/source_packs/climate-environment.json`
   with `install_source_pack` and accept each source's terms. Credentialed
   sources need `NOESIS_OPENAQ_API_KEY` and `NOESIS_ENTSOE_SECURITY_TOKEN`
   (preflight reports `credential_missing` otherwise). Apply the Umweltatlas
   layers as the `geospatial-berlin` upgrade:
   `preview_source_pack_upgrade_impact` with
   `packs/climate-environment/source_packs/geospatial-berlin-1.2.0.json`, then
   `apply_source_pack_upgrade` with the preview/impact hashes and the three
   new sources in `accepted_license_sources`.
3. **Acquire.** Run the packs through the source-pack runtime
   (`run_source_pack_execution`; schedules and the maintenance orchestrator
   refresh them), or acquire one explicit selection with
   `acquire_environment_source` (DurableHTTP budget, receipts).
4. **Register the place** (or use an existing one) with the geospatial tools,
   then `build_environment_place_dossier` with `place_id` (an ambiguous
   `mention` is returned as `place_unresolved` with candidates).
5. **Read the dossier.** Each item has source URL, kind and as-of (vintage id,
   release/retrieval clocks, status). `export_environment_dossier` renders cited
   Markdown; `replay_environment_dossier` recomputes the spatial receipts.
6. **Revisions.** `compare_environment_vintages` explains re-publications
   (provisional → validated, corrected ETS emissions, revised totals).
   `inspect_environment_dossier` reports `stale` when a pinned vintage or
   revision has been superseded; build a new dossier to adopt it.
7. **Operators and obligations.** `propose_environment_operator_links`, then a
   second principal runs `review_environment_operator_link`.
   `attach_environment_obligation_evidence` shows a release beside a policy
   obligation recorded by the policy monitor; there is never a determination.
8. **Monitor.** `create_environment_monitor` (user thresholds; optional cited
   source), `run_environment_monitor` at committed watermarks,
   `poll_environment_monitor`.

## Offline reproduction

```bash
python -m pytest tests/unit/environment tests/unit/domains/test_climate_environment_acceptance.py -p no:cacheprovider
python scripts/environment_demo.py --output docs/examples/environment-place-dossier-demo.md
python -m tests.unit.environment.fixture_builder   # after editing a raw fixture
```

All offline evidence uses authored fixtures (`tests/fixtures/environment/README.md`).

## Per-provider live state

| Provider | State | Checked | Evidence |
| --- | --- | --- | --- |
| OpenAQ | blocked (no credential; keyless probe `ConnectError`) | 2026-09-27 | `docs/development/environment-evidence/live-check-2026-09-27.json` |
| Umweltbundesamt | blocked (`provider_failed`, `ConnectError`) | 2026-09-27 | same report |
| ENTSO-E | blocked (no credential; keyless probe `ConnectError`) | 2026-09-27 | same report |
| SMARD | blocked (`ConnectError`) | 2026-09-27 | same report |
| EEA Industrial Emissions | blocked (`ConnectError`) | 2026-09-27 | same report |
| EU ETS Union Registry | blocked (`ConnectError`) | 2026-09-27 | same report |
| Berlin Umweltatlas WFS | blocked (`ConnectError` on `GetCapabilities`) | 2026-09-27 | same report |
| Open-Meteo archive / forecast | blocked (`ConnectError`) | 2026-09-27 | same report |
| DWD | blocked (`ConnectError`) | 2026-09-27 | same report |
| Copernicus/CAMS | not implemented | — | access decision in `PROVIDER_CONTRACTS` |

Offline and live evidence are reported separately: the tests and demo never
count as live coverage, and the live report never contains fixture results.
Rerun `scripts/environment_live_check.py` from a network that reaches the hosts
in `PROVIDER_HOSTS` before changing any state above.
