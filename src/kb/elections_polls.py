"""Public-opinion poll series for the Political elections feature (#1908, L05).

A poll publisher's own release (a file or table the operator supplies with its
URL) is parsed by the existing polls connector
(:func:`src.ingestion.connectors.dataset.poll_source.parse_poll_csv` with the
``poll-release-csv`` column map) and, when the publisher's terms allow the
figures, stored in the dataset observation store through
:func:`~src.ingestion.connectors.dataset.poll_source.harvest_polls` and
``poll_to_series`` (``provider='poll'``), so ``check_opinion`` keeps answering
opinion claims from them unchanged.

This module keeps the election view of the same readings: each reading keeps
its publisher, commissioning client, method, fieldwork start and end, sample
size and population, and cites the release it came from (file digest, URL,
publication date). A reading is typed as a poll series and never as a result
vintage. A series whose terms do not confirm redistribution is stored as
link-only metadata without figures. Readings are never averaged, weighted or
combined: two publishers are two series.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import Any

from src.ingestion.election_sources import day_ms, slug
from src.kb.elections import (
    CONTRACT,
    READ_SCOPE,
    WRITE_SCOPE,
    ElectionError,
    _load,
    authorize,
    canonical,
    digest,
    table_exists,
)

REDISTRIBUTION = ("allowed", "link-only", "unknown")
FIXED_COLUMNS = (
    "publisher",
    "client",
    "fieldwork_start",
    "fieldwork_end",
    "published_on",
    "sample_size",
    "method",
    "population",
    "question",
    "margin_of_error",
)
_DDL = """
CREATE TABLE IF NOT EXISTS election_poll_releases (
  namespace TEXT NOT NULL, release_id TEXT NOT NULL, publisher TEXT NOT NULL, election_id TEXT NOT NULL,
  format TEXT NOT NULL, source_url TEXT NOT NULL, file_sha256 TEXT NOT NULL, published_on TEXT NOT NULL,
  redistribution TEXT NOT NULL, terms_url TEXT, reading_count INTEGER NOT NULL, imported_by TEXT NOT NULL,
  sequence INTEGER NOT NULL, retrieved_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, release_id)
);
CREATE TABLE IF NOT EXISTS election_poll_readings (
  namespace TEXT NOT NULL, reading_id TEXT NOT NULL, reading_key TEXT NOT NULL, revision_no INTEGER NOT NULL,
  series_id TEXT NOT NULL, dataset_series_id TEXT, publisher TEXT NOT NULL, client TEXT, election_id TEXT NOT NULL,
  question TEXT, option TEXT NOT NULL, value DOUBLE, fieldwork_start TEXT, fieldwork_end TEXT NOT NULL,
  sample_size INTEGER, population TEXT, method TEXT, margin_of_error DOUBLE, published_on TEXT NOT NULL,
  redistribution TEXT NOT NULL, content_hash TEXT NOT NULL, release_id TEXT NOT NULL, retrieved_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, reading_id)
);
"""


class ElectionPolls:
    def __init__(
        self,
        conn: Any,
        *,
        initialize: bool = True,
        now: Callable[[], int] | None = None,
    ) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "election_poll_readings")

    def import_release(
        self,
        namespace: str,
        *,
        publisher: str,
        election_id: str,
        source_url: str,
        csv_text: str,
        redistribution: str,
        principal_id: str,
        scopes: Iterable[str],
        terms_url: str | None = None,
        format_id: str = "poll-release-csv",
        retrieved_at_ms: int | None = None,
    ) -> dict[str, Any]:
        """Import one publisher release; unchanged re-imports add nothing, a changed figure is a new reading revision."""
        from src.ingestion.connectors.dataset.poll_source import (
            PROVIDER_COLUMN_MAPS,
            harvest_polls,
            parse_poll_csv,
            with_options,
        )
        from src.ingestion.connectors.dataset.store import ObservationStore

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if redistribution not in REDISTRIBUTION:
            raise ElectionError(
                "invalid_terms", f"redistribution is one of {REDISTRIBUTION}"
            )
        if (
            not str(source_url).startswith("https://")
            or not str(publisher).strip()
            or not election_id
        ):
            raise ElectionError(
                "invalid_release",
                "a poll release names its publisher, election and publisher URL (https)",
            )
        if format_id not in PROVIDER_COLUMN_MAPS:
            raise ElectionError("invalid_release", f"unknown poll layout {format_id!r}")
        header = csv_text.splitlines()[0].split(",") if csv_text.strip() else []
        missing = [
            c
            for c in ("publisher", "fieldwork_end", "sample_size", "question")
            if c not in header
        ]
        options = [
            c.strip() for c in header if c.strip() and c.strip() not in FIXED_COLUMNS
        ]
        if missing or not options:
            raise ElectionError(
                "schema_drift",
                f"poll release lacks columns {missing or ['one answer option']}",
            )
        cmap = replace(
            with_options(PROVIDER_COLUMN_MAPS[format_id], {o: o for o in options}),
            topic="",
        )
        readings = parse_poll_csv(csv_text, column_map=cmap, topic=election_id)
        if any(r.methodology.house != publisher for r in readings):
            raise ElectionError(
                "invalid_release", "every row names the importing publisher"
            )
        file_sha = hashlib.sha256(csv_text.encode()).hexdigest()
        stated = sorted(
            {r.methodology.published_on for r in readings if r.methodology.published_on}
        )
        published = stated[-1] if stated else None
        if published is None:
            raise ElectionError(
                "schema_drift", "poll release states no publication date"
            )
        release_id = (
            "election-poll-release:"
            + digest([namespace, publisher, file_sha, redistribution])[:24]
        )
        if self.conn.execute(
            "SELECT 1 FROM election_poll_releases WHERE namespace=? AND release_id=?",
            [namespace, release_id],
        ).fetchone():
            return {
                "release_id": release_id,
                "status": "unchanged",
                "readings": 0,
                "revised": 0,
            }
        retrieved = int(retrieved_at_ms if retrieved_at_ms is not None else self.now())
        dataset_ids: dict[str, str] = {}
        if redistribution == "allowed":
            # The figures go to the observation store as poll series through the existing polls connector.
            harvest_polls(
                source_url,
                ObservationStore(self.conn),
                fetch=lambda _url: csv_text,
                column_map=cmap,
                topic=election_id,
                poll_id=lambda r: (
                    f"{slug(publisher)}-{r.methodology.fieldwork_end or r.period}"
                ),
                as_of_for=lambda r: day_ms(r.methodology.published_on or published),
                on_stored=lambda r, record: dataset_ids.__setitem__(
                    canonical(self._reading_identity(r, publisher, election_id)),
                    record.series_id,
                ),
            )
        counts = {"readings": 0, "revised": 0}
        latest = self.conn.execute(
            "SELECT max(sequence) FROM election_poll_releases WHERE namespace=?",
            [namespace],
        ).fetchone()
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO election_poll_releases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    release_id,
                    publisher,
                    election_id,
                    format_id,
                    source_url,
                    file_sha,
                    published,
                    redistribution,
                    terms_url,
                    len(readings),
                    principal_id,
                    int(latest[0] or 0) + 1,
                    retrieved,
                ],
            )
            for reading in readings:
                change = self._observe(
                    namespace,
                    reading,
                    publisher,
                    election_id,
                    redistribution,
                    release_id,
                    published,
                    retrieved,
                    dataset_ids.get(
                        canonical(
                            self._reading_identity(reading, publisher, election_id)
                        )
                    ),
                )
                if change:
                    counts[change] += 1
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {
            "release_id": release_id,
            "status": "applied",
            "published_on": published,
            "redistribution": redistribution,
            "link_only": redistribution != "allowed",
            **counts,
        }

    @staticmethod
    def _reading_identity(reading, publisher: str, election_id: str) -> list[Any]:
        m = reading.methodology
        return [
            publisher,
            m.client,
            election_id,
            m.question,
            reading.option,
            m.fieldwork_start,
            m.fieldwork_end,
        ]

    def _observe(
        self,
        namespace,
        reading,
        publisher,
        election_id,
        redistribution,
        release_id,
        published,
        retrieved,
        dataset_series_id,
    ):
        m = reading.methodology
        identity = self._reading_identity(reading, publisher, election_id)
        reading_key = "election-poll-reading:" + digest([namespace, *identity])[:24]
        series_id = (
            "election-poll-series:"
            + digest([namespace, publisher, election_id, m.question, reading.option])[
                :24
            ]
        )
        value = reading.support_pct if redistribution == "allowed" else None
        content = {
            "value": value,
            "sample_size": m.sample_n,
            "population": m.population,
            "method": m.mode,
            "margin_of_error": m.margin_of_error,
            "redistribution": redistribution,
        }
        # A republished, unchanged reading is the same observation whatever its new publication date.
        content_hash = digest(content)
        if self.conn.execute(
            "SELECT 1 FROM election_poll_readings WHERE namespace=? AND reading_key=? AND content_hash=?",
            [namespace, reading_key, content_hash],
        ).fetchone():
            return None
        number = 1 + int(
            self.conn.execute(
                "SELECT coalesce(max(revision_no), 0) FROM election_poll_readings WHERE namespace=? AND reading_key=?",
                [namespace, reading_key],
            ).fetchone()[0]
        )
        self.conn.execute(
            "INSERT INTO election_poll_readings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                reading_key + f"#{number}",
                reading_key,
                number,
                series_id,
                dataset_series_id,
                publisher,
                m.client,
                election_id,
                m.question,
                reading.option,
                value,
                m.fieldwork_start,
                m.fieldwork_end,
                m.sample_n,
                m.population,
                m.mode,
                m.margin_of_error,
                m.published_on or published,
                redistribution,
                content_hash,
                release_id,
                retrieved,
            ],
        )
        return "readings" if number == 1 else "revised"

    # ------------------------------------------------------------------ views

    def _release(self, namespace: str, release_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT release_id, publisher, source_url, file_sha256, published_on, redistribution, terms_url, "
            "retrieved_at_ms FROM election_poll_releases WHERE namespace=? AND release_id=?",
            [namespace, release_id],
        ).fetchone()
        return {
            "release_id": row[0],
            "provider": row[1],
            "source_id": None,
            "file_sha256": row[3],
            "published_on": row[4],
            "url": row[2],
            "evidence_origin": "operator-import",
            "retrieved_at_ms": row[7],
            "redistribution": row[5],
            "terms_url": row[6],
        }

    _COLUMNS = (
        "reading_id",
        "reading_key",
        "revision_no",
        "series_id",
        "dataset_series_id",
        "publisher",
        "client",
        "election_id",
        "question",
        "option",
        "value",
        "fieldwork_start",
        "fieldwork_end",
        "sample_size",
        "population",
        "method",
        "margin_of_error",
        "published_on",
        "redistribution",
        "release_id",
    )

    def _reading(self, namespace: str, row) -> dict[str, Any]:
        view = dict(zip(self._COLUMNS, row))
        view["source_revision"] = self._release(namespace, view["release_id"])
        if view["redistribution"] != "allowed":
            view["value"] = None
            view["note"] = (
                "link-only: the publisher's terms do not confirm redistribution of the figures"
            )
        return {
            "contract": CONTRACT,
            "record_type": "poll_reading",
            "typed_as": "poll",
            "namespace": namespace,
            **view,
        }

    def readings(
        self,
        namespace: str,
        *,
        scopes: Iterable[str],
        election_id: str | None = None,
        series_id: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        current_only: bool = True,
    ) -> list[dict[str, Any]]:
        """Readings by fieldwork end date; with ``current_only`` the latest revision of each by publication date."""
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self.ready():
            return []
        rows = self.conn.execute(
            f"SELECT {', '.join(self._COLUMNS)} FROM election_poll_readings WHERE namespace=? AND "
            "(? IS NULL OR election_id=?) AND (? IS NULL OR series_id=?) AND (? IS NULL OR fieldwork_end>=?) AND "
            "(? IS NULL OR fieldwork_end<=?) ORDER BY series_id, fieldwork_end, reading_key, published_on, revision_no",
            [
                namespace,
                election_id,
                election_id,
                series_id,
                series_id,
                date_from,
                date_from,
                date_to,
                date_to,
            ],
        ).fetchall()
        views = [self._reading(namespace, r) for r in rows]
        if current_only:
            latest: dict[str, dict[str, Any]] = {}
            for view in (
                views
            ):  # ordered by publication date, then revision: the last one is current
                latest[view["reading_key"]] = view
            views = [v for v in views if latest[v["reading_key"]] is v]
        return views

    def series(
        self,
        namespace: str,
        *,
        scopes: Iterable[str],
        election_id: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict[str, Any]]:
        """Poll series (one per publisher, question and option), readings kept apart - never averaged."""
        readings = self.readings(
            namespace,
            scopes=scopes,
            election_id=election_id,
            date_from=date_from,
            date_to=date_to,
        )
        grouped: dict[str, dict[str, Any]] = {}
        for reading in readings:
            item = grouped.setdefault(
                reading["series_id"],
                {
                    "contract": CONTRACT,
                    "record_type": "poll_series",
                    "typed_as": "poll",
                    "namespace": namespace,
                    "series_id": reading["series_id"],
                    "publisher": reading["publisher"],
                    "client": reading["client"],
                    "election_id": reading["election_id"],
                    "question": reading["question"],
                    "option": reading["option"],
                    "redistribution": reading["redistribution"],
                    "readings": [],
                    "note": "a poll series, never a result; readings of different publishers are never combined",
                },
            )
            item["readings"].append(reading)
            if reading["redistribution"] != "allowed":
                item["redistribution"] = reading["redistribution"]
        return list(grouped.values())

    def reading(
        self, namespace: str, reading_id: str, *, scopes: Iterable[str]
    ) -> dict[str, Any]:
        authorize(namespace, set(scopes), READ_SCOPE)
        row = self.conn.execute(
            f"SELECT {', '.join(self._COLUMNS)} FROM election_poll_readings WHERE namespace=? AND reading_id=?",
            [namespace, reading_id],
        ).fetchone()
        if row is None:
            raise ElectionError(
                "not_found", "poll reading is not visible in this namespace"
            )
        return self._reading(namespace, row)


def dataset_metadata(conn: Any, dataset_series_id: str) -> dict[str, Any] | None:
    """The observation store's header for a poll reading (``provider='poll'``), when its figures were stored."""
    if not table_exists(conn, "dataset_series"):
        return None
    row = conn.execute(
        "SELECT provider, metadata FROM dataset_series WHERE series_id=?",
        [dataset_series_id],
    ).fetchone()
    return None if row is None else {"provider": row[0], "metadata": _load(row[1], {})}


__all__ = ["ElectionPolls", "REDISTRIBUTION", "dataset_metadata"]
