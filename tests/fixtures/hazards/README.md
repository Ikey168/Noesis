# Natural Hazards fixtures (authored, fictional)

Every file here is authored in the provider's documented shape (GloFAS: a declared notification export
shape, since notifications are partner-distributed) and describes fictional events dated 2099. None of
it is provider data or live coverage.

| File | Shape | Scenario |
| --- | --- | --- |
| `usgs_query_2099-08-10T0330.geojson`, `usgs_query_2099-08-11.geojson` | USGS FDSN event GeoJSON | us7000zz01 automatic mb 5.8 then reviewed Mww 6.1; us7000zz02 later deleted |
| `usgs_detail_us7000zz01_*.geojson` | USGS event detail with `losspager` | PAGER yellow then orange; us7000zz03 becomes a secondary ID |
| `emsc_query_*.json` | EMSC FDSN json | EMSC's own magnitudes and authoring agency (EMSC, then NOA) |
| `gdacs_events_*.geojson` | GDACS geteventlist GeoJSON | EQ episodes 1500001/1500002 (GLIDE), a Red TC episode, a Green WF |
| `nhc_al052099_*.txt` | NHC TCM / TCP text | advisories 11, 12, intermediate 12A and 13 with watches/warnings |
| `effis_burnt_areas_*.geojson` | EFFIS burnt-area GeoJSON | burnt area 99001 revised from 120 ha to 342 ha |
| `glofas_notifications_2099-08-20.json` | declared export | one modelled flood notification with a return-period threshold |

`python -m tests.unit.hazards.harness` rebuilds `tests/fixtures/source_packs/hazards-*.json` from the
latest documents and re-pins `config/source_packs/natural-hazards.json`.
