"""Bulk-release acquisition: free bulk alternatives to capped or paid APIs (FA01).

Resolve an adapter with :func:`get_bulk_adapter` and run it with
:class:`~src.ingestion.bulk.runner.BulkRunner`, or use the CLI::

    python -m src.ingestion.bulk list
    python -m src.ingestion.bulk run bls-flat-files --params '{"survey": "cu", "series": ["CUSR0000SA0"]}' --dry-run
"""
from __future__ import annotations

import importlib
from typing import Dict, Type

from src.ingestion.bulk.base import BulkAdapter

# name -> "module:Class" (imported lazily so optional dependencies stay optional)
ADAPTERS: Dict[str, str] = {
    "bls-flat-files": "src.ingestion.bulk.adapters.bls:BlsFlatFiles",
    "companies-house-snapshot": "src.ingestion.bulk.adapters.companies_house:CompaniesHouseSnapshot",
}


def get_bulk_adapter(name: str) -> BulkAdapter:
    try:
        target = ADAPTERS[name]
    except KeyError:
        raise KeyError(f"unknown bulk adapter {name!r}; known: {', '.join(sorted(ADAPTERS))}") from None
    module, cls = target.split(":")
    adapter_cls: Type[BulkAdapter] = getattr(importlib.import_module(module), cls)
    return adapter_cls()
