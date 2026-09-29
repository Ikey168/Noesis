# Corporate Ownership live-coverage evidence

Live checks only. Offline fixture evidence lives in `tests/unit/ownership` and
`tests/unit/domains/test_corporate_ownership_acceptance.py` and is never
recorded here.

`live-check-<date>.json` is written by `scripts/ownership_live_check.py`. It
runs one bounded acquisition per provider through the real `SourcePackRuntime`
with `network=live` (GLEIF and SEC EDGAR for Apple Inc., Companies House for
BP P.L.C.) and records the run status, per-source failure code (preflight or
transport), page and record counts, and a connectivity probe per host.
Credentials come from the environment only (`NOESIS_COMPANIES_HOUSE_API_KEY`,
`NOESIS_SEC_CONTACT`); without them the runtime preflight stops the source and
nothing is sent to that provider.

| Run | Result |
| --- | --- |
| `live-check-2026-09-27.json` | **No provider verified.** GLEIF: `transport:source_unavailable` (the session's egress proxy refused the CONNECT, 403). Companies House: `preflight:credential_missing` (no API key configured). SEC EDGAR: `preflight:credential_missing` (no fair-access contact configured). Open Ownership BODS: not attempted (no dataset path verified). Host probes for all four hosts: `URLError: Tunnel connection failed: 403 Forbidden`. `LIVE_VERIFICATION` therefore stays `unverified-live` for every provider. Rerun from a network that can reach api.gleif.org, api.company-information.service.gov.uk, data.sec.gov/www.sec.gov and bods-data.openownership.org, with an API key and a real SEC contact configured. |
