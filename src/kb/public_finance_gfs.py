"""Eurostat government finance statistics as vintaged series (#1909, B06).

GFS series (``gov_10a_main``, ``gov_10a_exp`` by COFOG, ``gov_10dd_edpt1``) are
acquired through the source-pack runtime by the ``public-finance`` connector,
which reads the JSON-stat cube with the existing
:class:`src.ingestion.connectors.dataset.eurostat.EurostatConnector` into a
``dataset-series-v1`` record. This module stores each provider update exactly
once in the existing dataset store (:func:`src.domains.economic.model.register_series`)
and records it as an economic release snapshot
(:class:`src.domains.economic.releases.EconomicReleaseStore`), so revisions
between Eurostat publications are addressable and compared with the existing
vintage comparison. Re-acquiring an unchanged update adds nothing.

Series keep the ESA 2010 accounting basis and their unit, sector and
transaction dimensions as published. They live in the dataset store only and
are never merged with national cash-basis plan or outturn figures.

The SDMX-ML path of :mod:`src.ingestion.connectors.dataset.sdmx` stamps a
vintage with the retrieval time, which would turn every re-acquisition into a
new vintage; the JSON-stat path carries Eurostat's own ``updated`` time and is
used here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from src.kb.public_finance import (
    READ_SCOPE,
    PublicFinanceError,
    PublicFinanceStore,
    authorize,
    canonical,
    table_exists,
)

ECONOMIC_READ = "knowledge:economic:read"
ECONOMIC_WRITE = "knowledge:economic:write"
OWNER = "public-finance"
ACCOUNTING_BASIS = "esa2010"
_DDL = """
CREATE TABLE IF NOT EXISTS public_finance_gfs_vintages (
  namespace TEXT NOT NULL, series_id TEXT NOT NULL, as_of BIGINT NOT NULL, vintage_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL, release_id TEXT NOT NULL, source_id TEXT, dataset TEXT NOT NULL,
  dimensions_json TEXT NOT NULL, published_on TEXT NOT NULL, recorded_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, series_id, as_of)
);
"""


class GovernmentFinanceStatistics:
    def __init__(
        self,
        conn: Any,
        *,
        now: Callable[[], int] | None = None,
        initialize: bool = True,
    ) -> None:
        self.store = PublicFinanceStore(conn, now=now, initialize=initialize)
        self.conn, self.now = conn, self.store.now
        if initialize:
            conn.execute(_DDL)

    def apply_series(
        self,
        namespace: str,
        header: Mapping[str, Any],
        items: Sequence[Mapping[str, Any]],
        *,
        run_id: str,
        source_id: str | None,
    ) -> dict[str, Any]:
        """Store one acquired GFS series update as a dataset vintage and an economic release snapshot."""
        from services.ingest.common.series_model import SeriesRecord
        from src.domains.economic.model import register_series
        from src.domains.economic.releases import EconomicReleaseStore

        if len(items) != 1 or header.get("accounting_basis") != ACCOUNTING_BASIS:
            raise PublicFinanceError(
                "invalid_release", "a GFS release is one ESA 2010 series"
            )
        record = SeriesRecord.from_dict(dict(items[0]))
        retrieved = self.now()
        record.metadata["acquired_at_ms"] = retrieved
        self._check_same_update(record)
        self.conn.execute("BEGIN")
        try:
            release_id, created = self.store._release(
                namespace,
                header,
                source_id=source_id,
                run_id=run_id,
                retrieved=retrieved,
                record_type="series",
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        known = self.conn.execute(
            "SELECT snapshot_id FROM public_finance_gfs_vintages WHERE namespace=? AND series_id=? AND as_of=?",
            [namespace, record.series_id, record.as_of],
        ).fetchone()
        if known:
            # The same provider update (a replay, or the same cube re-serialised): no new vintage.
            return {
                "release_id": release_id,
                "status": "unchanged",
                "series_id": record.series_id,
                "snapshot_id": known[0],
                "vintages": 0,
            }
        dataset = str(record.metadata.get("dataset") or "")
        dimensions = {
            k: v.get("code")
            for k, v in dict(record.metadata.get("selected_dimensions") or {}).items()
        }
        exists = (
            table_exists(self.conn, "economic_vintages")
            and self.conn.execute(
                "SELECT vintage_id FROM economic_vintages WHERE domain='economics' AND series_id=? AND as_of=?",
                [record.series_id, record.as_of],
            ).fetchone()
        )
        if not exists:
            register_series(
                self.conn,
                record,
                domain="economics",
                semantics={
                    "concept": f"government finance {record.series_id}",
                    "canonical_name": f"Eurostat {dataset} {record.geography} "
                    + " ".join(f"{k}={v}" for k, v in sorted(dimensions.items())),
                    "provider_code": dataset,
                    "definition": f"Eurostat {dataset} as published (ESA 2010); dimensions {canonical(dimensions)}",
                    "attributes": {
                        "accounting_basis": ACCOUNTING_BASIS,
                        "dimensions": dimensions,
                        "note": "ESA 2010 national accounts; never netted with cash-basis budget figures",
                    },
                },
            )
        vintage = self.conn.execute(
            "SELECT vintage_id, release_at_ms, retrieved_at_ms FROM economic_vintages WHERE domain='economics' AND "
            "series_id=? AND as_of=?",
            [record.series_id, record.as_of],
        ).fetchone()
        snapshot = EconomicReleaseStore(self.conn, now=self.now).create_snapshot(
            namespace,
            f"public-finance-gfs:{record.series_id}@{record.as_of}",
            vintage[0],
            release_cutoff_ms=int(vintage[1]),
            acquired_cutoff_ms=int(vintage[2]),
            series=[{"series_id": record.series_id, "vintage_id": vintage[0]}],
            principal_id=OWNER,
            scopes={"operator"},
        )
        self.conn.execute(
            "INSERT INTO public_finance_gfs_vintages VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                record.series_id,
                record.as_of,
                vintage[0],
                snapshot["snapshot_id"],
                release_id,
                source_id,
                dataset,
                canonical(dimensions),
                header["published_on"],
                retrieved,
            ],
        )
        return {
            "release_id": release_id,
            "status": "applied",
            "series_id": record.series_id,
            "vintage_id": vintage[0],
            "snapshot_id": snapshot["snapshot_id"],
            "vintages": 1,
        }

    def _check_same_update(self, record: Any) -> None:
        """An already stored provider update must state the same values; a silent change is refused, not dropped."""
        if not table_exists(self.conn, "dataset_observations") or not table_exists(
            self.conn, "public_finance_gfs_vintages"
        ):
            return
        if not self.conn.execute(
            "SELECT 1 FROM public_finance_gfs_vintages WHERE series_id=? AND as_of=?",
            [record.series_id, record.as_of],
        ).fetchone():
            return
        stored = {
            period: value
            for period, value in self.conn.execute(
                "SELECT period, value FROM dataset_observations WHERE series_id=? AND as_of=?",
                [record.series_id, record.as_of],
            ).fetchall()
        }
        if stored != {o.period: o.value for o in record.observations}:
            raise PublicFinanceError(
                "vintage_conflict",
                "the provider changed values without a new updated time; the stored vintage is kept and the "
                "response is refused",
            )

    def series(
        self, namespace: str, *, scopes: Iterable[str], dataset: str | None = None
    ) -> list[dict[str, Any]]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "public_finance_gfs_vintages"):
            return []
        rows = self.conn.execute(
            "SELECT series_id, as_of, vintage_id, snapshot_id, release_id, dataset, dimensions_json, published_on "
            "FROM public_finance_gfs_vintages WHERE namespace=? AND (? IS NULL OR dataset=?) ORDER BY series_id, as_of",
            [namespace, dataset, dataset],
        ).fetchall()
        grouped: dict[str, dict[str, Any]] = {}
        import json

        for (
            series_id,
            as_of,
            vintage_id,
            snapshot_id,
            release_id,
            ds,
            dims,
            published,
        ) in rows:
            entry = grouped.setdefault(
                series_id,
                {
                    "series_id": series_id,
                    "dataset": ds,
                    "dimensions": json.loads(dims),
                    "accounting_basis": ACCOUNTING_BASIS,
                    "vintages": [],
                    "note": "ESA 2010 series from the dataset store; never netted with cash-basis plan or outturn figures",
                },
            )
            entry["vintages"].append(
                {
                    "as_of": as_of,
                    "vintage_id": vintage_id,
                    "snapshot_id": snapshot_id,
                    "published_on": published,
                    "source_revision": self.store.source_revision(
                        namespace, release_id
                    ),
                }
            )
        return list(grouped.values())

    def compare(
        self,
        namespace: str,
        series_id: str,
        *,
        scopes: Iterable[str],
        left_as_of: int | None = None,
        right_as_of: int | None = None,
    ) -> dict[str, Any]:
        """Two vintages of one GFS series through the existing economic release comparison."""
        from src.domains.economic.releases import EconomicReleaseStore

        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        # The comparison is kept as an economic comparison artifact, so it reads and writes economic scope.
        if "operator" not in scopes and not {ECONOMIC_READ, ECONOMIC_WRITE} <= scopes:
            raise PublicFinanceError(
                "unauthorized", f"{ECONOMIC_READ} and {ECONOMIC_WRITE} are required"
            )
        entry = next(
            (
                s
                for s in self.series(namespace, scopes=scopes)
                if s["series_id"] == series_id
            ),
            None,
        )
        if entry is None or not entry["vintages"]:
            raise PublicFinanceError("not_found", "no acquired vintage of this series")
        vintages = entry["vintages"]
        left = (
            vintages[0]
            if left_as_of is None
            else next((v for v in vintages if v["as_of"] == left_as_of), None)
        )
        right = (
            vintages[-1]
            if right_as_of is None
            else next((v for v in vintages if v["as_of"] == right_as_of), None)
        )
        if left is None or right is None:
            raise PublicFinanceError(
                "not_found", "the requested vintage was not acquired"
            )
        # The snapshots belong to the record owner; the caller's access was checked above.
        comparison = EconomicReleaseStore(self.conn, now=self.now).compare(
            namespace,
            f"public-finance-gfs:{left['snapshot_id']}:{right['snapshot_id']}",
            left["snapshot_id"],
            right["snapshot_id"],
            principal_id=OWNER,
            scopes={"operator"},
        )
        return {
            "series_id": series_id,
            "dataset": entry["dataset"],
            "dimensions": entry["dimensions"],
            "accounting_basis": ACCOUNTING_BASIS,
            "left": left,
            "right": right,
            "comparison_id": comparison["comparison_id"],
            "items": comparison["items"],
            "note": "revisions between two Eurostat publications of one ESA 2010 series; nothing is compared "
            "with cash-basis budget figures",
        }
