"""Bulk sink that applies ownership records to the ownership store (#2885).

Rows of an ``ownership_records`` table (see
:mod:`src.ingestion.bulk.adapters.companies_house_psc`) carry one
``noesis-ownership-record-v1`` record each. They are applied through
:meth:`src.kb.ownership_store.OwnershipStore.apply`, which appends a revision
only when a record's content changes: re-acquiring an unchanged snapshot adds
nothing, a ceased or changed entry is a new revision, and nothing is deleted.
The run id names the adapter and the release, so every revision says which
snapshot observed it. Owner-scoped person records belong to ``principal_id``.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from src.ingestion.bulk.sinks import Sink

TABLE = "ownership_records"


class OwnershipSink(Sink):
    def __init__(self, namespace: str, connect: Callable[[], Any], *, principal_id: str = "bulk-runner",
                 close: bool = True, now: Callable[[], int] | None = None):
        self.namespace, self.connect, self.principal, self.close = namespace, connect, principal_id, close
        self.now = now or (lambda: int(time.time() * 1000))
        self.run_id = ""
        self.counts: dict[str, int] = {"inserted": 0, "revised": 0, "unchanged": 0}

    def begin(self, adapter, release, manifest):
        self.run_id = f"bulk:{adapter.name}:{release.release_id}:{manifest['manifest_id']}"

    def write(self, file, table, records):
        if table != TABLE or not records:
            return
        from src.kb.ownership_store import OwnershipStore

        conn = self.connect()
        try:
            counts = OwnershipStore(conn).apply(self.namespace, [row["record"] for row in records], run_id=self.run_id,
                                                observed_at_ms=self.now(), principal_id=self.principal)
        finally:
            if self.close:
                conn.close()
        for key, value in counts.items():
            self.counts[key] = self.counts.get(key, 0) + value

    def finish(self):
        return {"ownership_namespace": self.namespace, "ownership_run_id": self.run_id,
                "ownership_counts": dict(self.counts)}
