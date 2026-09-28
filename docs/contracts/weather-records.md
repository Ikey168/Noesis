# Weather record contract (`noesis-weather-record-v1`)

JSON Schema: [`contracts/schemas/jsonschema/noesis-weather-record-v1.json`](../../contracts/schemas/jsonschema/noesis-weather-record-v1.json).
Constructors and validation: `src/kb/weather_records.py`. Store: `src/kb/weather_store.py`.
The schema is registered in the shared schema registry as `weather-record@1.0.0`
(`weather_records.register_schemas`), and the store owner is `weather.observations`.

The contract extends the Climate & Environment records (`noesis-environment-record-v1`)
and never duplicates them. Stations are environment `station` records, referenced
as `{"provider", "native_id"}`. A station the environment owner does not know yet is
registered **through** `EnvironmentStore.apply` (providers `dwd`, `dwd-mosmix`,
`aviationweather`), never in a Weather table.

| Record type | Key | Reference time | What it holds |
| --- | --- | --- | --- |
| `station_location_vintage` | station + `valid_from` | `valid_from` (date) | latitude, longitude, elevation, name and `valid_from`/`valid_to` as published. An open vintage has `open_ended: true` and no `valid_to` |
| `observation_report` | station + report type + `observed_at` | `observed_at` | parameters with native unit, the value as published (absent when missing, with the source's `missing` marker), QC scheme and native flag, report type, `correction` marker and raw METAR text |
| `forecast_issuance` | provider + product + model + location + `issued_at` | `issued_at` | the run as published: `run_id`, `generated_at`, model, originator, location (station, or grid cell/point with its geometry and the station declared for it) and `elements` |
| `forecast_element` (inside an issuance) | issuance + parameter + `valid_time` | — | `valid_time`, `lead_time_s` (= valid − issued), value or `missing`, native unit, `kind` (`value` or `probability`) |
| `warning` | CAP sender + identifier | `sent` | `msg_type` (`Alert`/`Update`/`Cancel`), references, event, severity, urgency, certainty, areas (warncell/UGC codes and polygons), onset, effective, expires and the issuer's own headline, description and instruction, quoted as issued |
| `verification_pair` (produced by `src/kb/weather_verification.py`) | element revision + observation revision | — | the published forecast value, the recorded observation value, the match rule and the tolerance |

Every record carries `provider`, `issuer`, `attribution`, `source_record_id`,
`reference_time`, `locator` (URL plus pointer) and `unknowns`. The store adds
per revision: `revision_id`, `seq`, `retrieved_at` (the acquisition time), the
source time and its basis, the precedence tier, and `change_kind` (`initial`,
`correction`, `qc_change`, `late_history`).

Rules the validators enforce:

* Times are UTC ISO instants (`YYYY-MM-DDTHH:MM:SSZ`) or dates. A source time
  without an offset is rejected rather than assumed to be UTC.
* Values are exact decimal text. Floats and the string `"None"` are rejected.
* No record holds a Noesis-produced, blended, bias-corrected, downscaled or
  re-forecast value. The provider is always an external publisher, and the
  issuer is that publisher's name.
* An `Update` or `Cancel` must name the messages it references. Chains are
  threaded at read time, so arrival order does not matter.
