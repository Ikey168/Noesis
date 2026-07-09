"""
Observation store for statistical series (Track A).

Persists ``SeriesRecord``s into two DuckDB tables:

* ``dataset_series`` — one row per series (the header fields), keyed by
  ``series_id``.
* ``dataset_observations`` — one row per ``(series_id, period, as_of)`` so
  revisions (vintages) are retained and a claim check can pin the vintage it
  ran against.

Writes are idempotent: harvesting the same vintage twice upserts the same rows.
Harvesting a newer vintage (a larger ``as_of``) appends new observation rows and
refreshes the series header, leaving prior vintages in place.

The store owns only its own tables and never mutates the shared corpus tables.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

from services.ingest.common.series_model import Observation, SeriesRecord

_SERIES_DDL = """
CREATE TABLE IF NOT EXISTS dataset_series (
    series_id   TEXT PRIMARY KEY,
    provider    TEXT NOT NULL,
    title       TEXT NOT NULL,
    unit        TEXT,
    frequency   TEXT NOT NULL,
    geography   TEXT,
    license     TEXT,
    as_of       BIGINT NOT NULL,
    source_url  TEXT,
    metadata    JSON
)
"""

_OBS_DDL = """
CREATE TABLE IF NOT EXISTS dataset_observations (
    series_id   TEXT NOT NULL,
    period      TEXT NOT NULL,
    as_of       BIGINT NOT NULL,
    value       DOUBLE,
    PRIMARY KEY (series_id, period, as_of)
)
"""


class ObservationStore:
    """DuckDB-backed store for ``dataset-series-v1`` series and observations."""

    def __init__(self, conn: Any):
        """Wrap an open DuckDB connection. The caller owns the connection
        lifecycle (in-memory for tests, a file path in production)."""
        self._conn = conn
        self._ensure_schema()

    @classmethod
    def open(cls, path: str = ":memory:") -> "ObservationStore":
        """Open (or create) a DuckDB database at ``path`` and wrap it."""
        import duckdb

        return cls(duckdb.connect(path))

    def _ensure_schema(self) -> None:
        self._conn.execute(_SERIES_DDL)
        self._conn.execute(_OBS_DDL)

    def upsert(self, record: SeriesRecord) -> int:
        """Persist a series and its observations idempotently.

        Returns the number of observation rows written for this vintage. The
        series header is upserted to the record's values (last writer wins on a
        given ``series_id``); observation rows are keyed by vintage so prior
        vintages survive.
        """
        self._conn.execute(
            """
            INSERT INTO dataset_series
                (series_id, provider, title, unit, frequency, geography, license, as_of, source_url, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (series_id) DO UPDATE SET
                provider = excluded.provider,
                title = excluded.title,
                unit = excluded.unit,
                frequency = excluded.frequency,
                geography = excluded.geography,
                license = excluded.license,
                as_of = excluded.as_of,
                source_url = excluded.source_url,
                metadata = excluded.metadata
            """,
            [
                record.series_id,
                record.provider,
                record.title,
                record.unit,
                record.frequency,
                record.geography,
                record.license,
                record.as_of,
                record.source_url,
                json.dumps(record.metadata or {}),
            ],
        )
        written = 0
        for obs in record.observations:
            self._conn.execute(
                """
                INSERT INTO dataset_observations (series_id, period, as_of, value)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (series_id, period, as_of) DO UPDATE SET
                    value = excluded.value
                """,
                [record.series_id, obs.period, record.as_of, obs.value],
            )
            written += 1
        return written

    def upsert_many(self, records: Iterable[SeriesRecord]) -> int:
        """Upsert a batch of series; returns total observation rows written."""
        return sum(self.upsert(r) for r in records)

    def get_series(self, series_id: str) -> Optional[Dict[str, Any]]:
        """Return the series header row as a dict, or None if absent."""
        row = self._conn.execute(
            """
            SELECT series_id, provider, title, unit, frequency, geography, license, as_of, source_url, metadata
            FROM dataset_series WHERE series_id = ?
            """,
            [series_id],
        ).fetchone()
        if row is None:
            return None
        keys = [
            "series_id", "provider", "title", "unit", "frequency",
            "geography", "license", "as_of", "source_url", "metadata",
        ]
        result = dict(zip(keys, row))
        if isinstance(result.get("metadata"), str):
            result["metadata"] = json.loads(result["metadata"])
        return result

    def list_series(self, provider: Optional[str] = None, geography: Optional[str] = None) -> List[Dict[str, Any]]:
        """List series headers, optionally filtered by provider and/or geography."""
        clauses: List[str] = []
        params: List[Any] = []
        if provider is not None:
            clauses.append("provider = ?")
            params.append(provider)
        if geography is not None:
            clauses.append("geography = ?")
            params.append(geography)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT series_id, provider, title, unit, frequency, geography, license, as_of FROM dataset_series{where} ORDER BY series_id",
            params,
        ).fetchall()
        keys = ["series_id", "provider", "title", "unit", "frequency", "geography", "license", "as_of"]
        return [dict(zip(keys, row)) for row in rows]

    def get_observations(
        self, series_id: str, as_of: Optional[int] = None
    ) -> List[Observation]:
        """Return observations for a series at a given vintage.

        With ``as_of`` omitted, the latest vintage present is used, so callers
        get a coherent single-vintage series rather than a mix of revisions.
        """
        if as_of is None:
            latest = self._conn.execute(
                "SELECT MAX(as_of) FROM dataset_observations WHERE series_id = ?",
                [series_id],
            ).fetchone()
            if latest is None or latest[0] is None:
                return []
            as_of = latest[0]
        rows = self._conn.execute(
            """
            SELECT period, value FROM dataset_observations
            WHERE series_id = ? AND as_of = ?
            ORDER BY period
            """,
            [series_id, as_of],
        ).fetchall()
        return [Observation(period=r[0], value=r[1]) for r in rows]

    def vintages(self, series_id: str) -> List[int]:
        """List the distinct ``as_of`` vintages stored for a series, ascending."""
        rows = self._conn.execute(
            "SELECT DISTINCT as_of FROM dataset_observations WHERE series_id = ? ORDER BY as_of",
            [series_id],
        ).fetchall()
        return [r[0] for r in rows]
