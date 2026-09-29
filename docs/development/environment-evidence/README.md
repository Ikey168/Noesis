# Climate and Environment live-coverage evidence

Live checks only. Offline fixture evidence lives in `tests/unit/environment`
and `tests/unit/domains/test_climate_environment_acceptance.py` and is never
recorded here.

`live-check-<date>.json` is written by `scripts/environment_live_check.py`. It
holds one bounded acquisition per provider under its access contract, with the
DurableHTTP request receipts (URL, HTTP status or failure code and type,
observation time, `last-modified`/`etag` where sent), parsed record counts and
kinds, or the failure. Credentialed providers without a configured credential
get one keyless reachability probe and are reported as `credential_missing`.
Umweltatlas services get one WFS `GetCapabilities` request each. Copernicus/CAMS
is reported as `not_implemented` without a request.

| Run | Result |
| --- | --- |
| `live-check-2026-09-27.json` | No provider was reachable. UBA, SMARD, EEA Discodata, EU ETS (climate.ec.europa.eu), Open-Meteo archive and forecast, DWD and the Umweltatlas WFS services failed with `provider_failed` (`ConnectError`): the build environment's egress policy rejects the provider hosts. OpenAQ and ENTSO-E had no credential (`credential_missing`) and their keyless probes also failed with `ConnectError`. Copernicus/CAMS: `not_implemented`. Coverage is **not** validated; `LIVE_VERIFICATION` records every provider as `blocked` (CAMS `not implemented`). Rerun from a network that can reach the hosts in `PROVIDER_HOSTS`, with `NOESIS_OPENAQ_API_KEY` and `NOESIS_ENTSOE_SECURITY_TOKEN` set. |

What a successful rerun must still check by hand before any provider is marked
live-verified: the Discodata table and column names, the Union Registry export
link and columns (the Commission publishes XLSX; the adapter reads the CSV form),
the Umweltatlas service/type names in `GetCapabilities`, the UBA timestamp
convention (CET) and the SMARD filter identifiers.
