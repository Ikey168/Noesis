# Clinical Evidence live-coverage evidence

Live checks only. Offline fixture evidence lives in `tests/unit/clinical` and
`tests/unit/domains/test_clinical_evidence_acceptance.py` and is never recorded
here.

`live-check-<date>.json` is written by `scripts/clinical_live_check.py`. It
installs the `clinical-evidence` source pack in a throwaway warehouse, runs each
source once through the real runtime with small budgets (`network: live`) and
sends one plain HTTPS probe per provider host with the same transport policy, so
each failure keeps its cause. An openFDA key is used only when
`NOESIS_OPENFDA_API_KEY` is set and never appears in the report.

| Run | Result |
| --- | --- |
| `live-check-2026-09-27.json` | Every source failed with `source_unavailable` (classification `transient-availability`); every probe failed with `Tunnel connection failed: 403 Forbidden`. The build environment's egress proxy refused CONNECT to clinicaltrials.gov, euclinicaltrials.eu, www.clinicaltrialsregister.eu, api.fda.gov, www.ema.europa.eu and www.crd.york.ac.uk. Coverage is **not** validated; `LIVE_VERIFICATION` stays `unverified-live`. Rerun from a network that can reach those hosts. |
