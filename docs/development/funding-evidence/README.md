# Funding & Grants live-coverage evidence

Live checks only. Offline fixture evidence lives in `tests/unit/funding` and is
never recorded here.

`live-check-<date>.json` is written by `scripts/funding_live_check.py`. It holds
one bounded acquisition per provider under its access contract, with request
receipts (URL, HTTP status or failure code, observation time, `last-modified`/
`etag` where sent), the parsed records and rule quotes to check by hand against
the official page.

| Run | Result |
| --- | --- |
| `live-check-2026-09-25.json` | All four providers failed with `ConnectError`. The build environment's egress policy denied the provider hosts. Coverage is **not** validated. Rerun from a network that can reach nlnet.nl, ec.europa.eu, api.tech.ec.europa.eu, www.foerderdatenbank.de and www.exist.de. |
