# What to deploy for the knowledge pipeline

Copying only `src/` to a host is not enough. The claim, linking and Jev
review code imports shared models from `services/`, and the pinned NLI
backend reads its weights pin from the repository's `models/` folder.

## Minimum set

| Path | Why |
|---|---|
| `src/` | Pipeline, KB, linking, Jev review, OSINT |
| `services/` | Shared data models. `services.ingest.common.document_model` (the `Document` type) is imported by about 60 modules in `src/`, including `src/ingestion/document_store.py` and `src/argument_mining/evidence.py`. `services.ingest.common.series_model`, `services.embeddings.provider` and `services.rag.*` are also used |
| `contracts/` | JSON Schemas and generated catalogs that `src/` validates against |
| `scripts/` | Operator entry points: `kb_brief.py`, `exclude_boilerplate_claims.py`, `apply_jev_link_verdicts.py`, `validate_jev_link_batching.py` |
| `models/pins.lock.json` | Model pins. `src/argument_mining/model_registry.py` resolves `LOCK_PATH` as `<repo root>/models/pins.lock.json`, and the NLI backend refuses to start without it |
| `pyproject.toml` | Dependency set for `pip install .` or `uv pip install` |

The pinned model weights themselves are fetched separately (`make models`,
see [model pins](../subsystems/model-pins.md)) into the cache directory the
pins name. They are not part of the deploy set.

## Installing as a package

`pip install .` packages `src`, `services`, `contracts` and `tools`
(`[tool.setuptools.packages.find]` in `pyproject.toml`). It does **not**
include `models/pins.lock.json`, because the registry reads it relative to
the repository root. Either deploy from a repository checkout, or copy
`models/pins.lock.json` to `<install root>/models/` next to the installed
`src` package.

## Quick check on a deployed host

```bash
python -c "import services.ingest.common.document_model, src.ingestion.document_store; print('imports ok')"
```
```bash
python -c "from src.argument_mining.model_registry import LOCK_PATH; print(LOCK_PATH, LOCK_PATH.exists())"
```
