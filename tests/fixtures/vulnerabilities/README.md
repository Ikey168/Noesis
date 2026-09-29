# Vulnerability fixtures (authored)

Every file here is hand-written in the documented payload shape of its provider
(NVD CVE API 2.0 and Change History API, NVD Products API 2.0, OSV schema,
GitHub Advisory Database, CISA KEV JSON, FIRST EPSS API, CVE JSON 5, CWE
`Weakness_Catalog` XML). Packages, vendors, products and CVE numbers are
fictional (`CVE-2099-*`, `GHSA-f1x7-*`, `fixture-parser`, `fixturecorp`), and
CWE descriptions are paraphrased. None of it is live evidence. The EPSS rows
carry a `model_version` field to exercise model-version changes; where the live
API carries the model version is still to be verified (see
`docs/roadmaps/technology-vulnerabilities-source-audit.md`).
