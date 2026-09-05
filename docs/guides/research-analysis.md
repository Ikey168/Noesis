# Reproducible research analyses

Register an analysis with `register_research_analysis`, then run it with `execute_research_analysis`. Each manifest pins one to twenty dataset release slices, optional metric revision IDs, notebook v4 code, JSON parameters, an immutable local container image ID, and resource limits. Input rows and definitions are hashed at registration and checked again before execution. Missing or changed inputs stop the run; the executor never selects a newer release.

Build the optional runtime with:

```sh
podman build -t localhost/noesis-analysis:development deploy/analysis
podman image inspect localhost/noesis-analysis:development --format '{{.Id}}'
```

Use the returned image ID prefixed with `sha256:`. The checked-in Containerfile pins its Python base image, and `requirements.lock` pins the notebook environment. Rootless Podman with cgroups v2 is required. Unsupported isolation fails explicitly. The application does not pull images while executing. The operator must build a credential-free image; image identity alone does not establish its trustworthiness.

Example manifest (replace IDs with existing dataset objects):

```json
{
  "notebook": {"nbformat": 4, "cells": [{"id": "inspect", "cell_type": "code", "source": "import json\nfrom pathlib import Path\ndata = json.loads(Path('/input/datasets.json').read_text())\nprint(data['datasets']['observations']['slice']['items'])"}]},
  "inputs": [{"name": "observations", "namespace": "economic", "release_id": "release-id", "table_id": "table-id", "offset": 0, "limit": 100}],
  "metrics": [], "parameters": {},
  "environment": {"image_id": "sha256:REPLACE_WITH_64_HEX_DIGITS"},
  "network": "none",
  "budgets": {"cell_timeout_seconds": 10, "run_timeout_seconds": 30, "memory_mb": 256, "cpus": 1, "max_output_bytes": 1048576}
}
```

The runner mounts inputs read-only, uses bounded temporary filesystems for working files and outputs, drops Linux capabilities, blocks networking, and passes no application environment variables or credentials. `nbclient` executes the notebook; Podman provides the separate resource and isolation boundary. Cell failures preserve notebook outputs and an error receipt when readable. Whole-run termination or OOM can leave no notebook output and is reported explicitly. Notebook code remains untrusted, and a successful run does not verify any substantive claim.

Use `inspect_research_analysis_run`, paged `list_research_analysis_runs`, and `cancel_research_analysis_run` for operation. The same request key returns the existing run without recomputation. A staged result can recover artifact publication without rerunning code. If a worker disappears before staging, `recover_research_analysis_run` can mark the outcome interrupted after the hard run deadline plus cleanup grace; it does not assume success or rerun unknown work. Start an intentional new attempt with a different request key.

Code-cell outputs have artifact IDs and dependencies on exact dataset slices and metric revisions. Authored reports can cite these with an `artifact` dependency and cell locator. `compare_research_analysis_runs` compares completed outputs using declared absolute and relative numeric tolerances, reports changed cells, and checks input, code, and environment identities. Numeric tolerance applies to numeric JSON MIME outputs; formatted text and images compare exactly.

`export_research_analysis` returns code, outputs, provenance and currently permitted input bytes or omissions. `export_research_analysis_package` creates a standard offline research package with transitive input/code/output dependencies. It builds in memory to avoid persisting private input copies in the shared component registry. Missing inputs produce a partial package with verified, explicit omissions. Importing a package does not execute its code.

All operations require current ownership, analysis and input namespace access, and dataset-read scope; metrics additionally require quantitative-read scope. Execution, cancellation, and recovery also require analysis-execute scope. Package export requires package-read scope. The MCP read methods use compatible DuckDB connections so they can inspect a concurrently running local executor; they perform no store initialization or mutation.

Validation: unit and public MCP tests cover source changes, access revocation, cancellation, interrupted workers, replay after publication failure, package omissions, and numeric comparisons. The opt-in `tests/integration/kb/test_research_analysis_container.py` runs the actual pinned image and records [runtime evidence](../development/workflow-implementation-evidence/notebook-e2e.json). Set `NOESIS_ANALYSIS_TEST_IMAGE` to enable it and `NOESIS_ANALYSIS_EVIDENCE_PATH` to write a receipt. Its two-row dataset is generated behavior-test data.

Library evaluation: [nbclient execution documentation](https://nbclient.readthedocs.io/en/latest/client.html) describes execution and cell errors; it is used here with a separate container boundary, not as a sandbox.
