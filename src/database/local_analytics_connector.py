"""
Local analytics warehouse connector (DuckDB).

A zero-dependency, file-based replacement for the Snowflake analytics
warehouse used by the API routes. DuckDB is an in-process analytical database
(a local "data warehouse" alternative to Snowflake) that supports the same
analytical SQL the routes rely on (``SPLIT_PART``, ``DATE_TRUNC``, window
functions, ``ILIKE`` …), so the existing queries run unchanged.

The connector mirrors the small surface the routes use:

    db = LocalAnalyticsConnector()
    db.connect()
    rows = await db.execute_query("SELECT ... WHERE x = %s", [value])
    db.disconnect()

``execute_query`` returns a list of tuples (positional rows), matching what the
routes expect (``row[0]``, ``row[1]`` …). ``%s`` placeholders (psycopg2 /
Snowflake style) are translated to DuckDB's ``?`` automatically.

The database file lives at ``$NEURONEWS_DB_PATH`` (default
``<repo>/data/neuronews.duckdb``) and is seeded with realistic sample news
articles on first use, so the API serves real rows out of the box with no
external services.
"""

from __future__ import annotations

import logging
import os
import stat
import threading
from pathlib import Path
from typing import Any, List, Optional, Sequence

import duckdb

from src.database.local_warehouse_seed import ensure_schema_and_seed

logger = logging.getLogger(__name__)

# Process-wide singleton connection. DuckDB connections are cheap but a single
# shared handle avoids re-opening the file (and racing the seed) on every
# request. Access is serialized with a lock since a DuckDB connection object is
# not safe for concurrent use.
_CONNECTION: Optional[duckdb.DuckDBPyConnection] = None
_LOCK = threading.Lock()


def _default_db_path() -> str:
    repo_root = Path(__file__).resolve().parents[2]
    return str(repo_root / "data" / "neuronews.duckdb")


def _enforce_db_permissions(path: str) -> None:
    """Set 0600 on the DB file and warn if it was wider than that."""
    try:
        mode = os.stat(path).st_mode & 0o777
        if mode & 0o077:  # group or other bits set
            logger.warning(
                "Database file %s has permissions %s — expected 0600. "
                "Tightening to 0600 to prevent unauthorised reads.",
                path,
                oct(mode),
            )
        os.chmod(path, 0o600)
    except OSError as exc:
        logger.debug("Could not check/set DB file permissions: %s", exc)


def get_shared_connection() -> duckdb.DuckDBPyConnection:
    """Return the process-wide DuckDB connection, creating + seeding it once."""
    global _CONNECTION
    if _CONNECTION is None:
        with _LOCK:
            if _CONNECTION is None:
                from src.config.env import warehouse_path
                path = warehouse_path(_default_db_path())
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                logger.info("Opening local analytics warehouse at %s", path)
                conn = duckdb.connect(path)
                ensure_schema_and_seed(conn)
                _enforce_db_permissions(path)
                _CONNECTION = conn
    return _CONNECTION


class LocalAnalyticsConnector:
    """DuckDB-backed analytics connector (drop-in for the warehouse routes)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Accept and ignore Snowflake-style kwargs (account/user/password/…) so
        # this is a drop-in replacement for the previous connector signature.
        self._connected = False

    def connect(self) -> bool:
        """Ensure the shared warehouse connection exists."""
        get_shared_connection()
        self._connected = True
        return True

    def disconnect(self) -> None:
        """No-op: the shared connection is kept alive for reuse."""
        self._connected = False

    # Some callers used `close()`; keep an alias for compatibility.
    def close(self) -> None:
        self.disconnect()

    def is_connected(self) -> bool:
        return self._connected

    async def execute_query(
        self, query: str, params: Optional[Sequence[Any]] = None
    ) -> List[tuple]:
        """Run a query and return rows as positional tuples.

        ``%s`` placeholders are rewritten to DuckDB's ``?`` form.
        """
        conn = get_shared_connection()
        duck_query = query.replace("%s", "?")
        bound = list(params) if params else []
        with _LOCK:
            cur = conn.execute(duck_query, bound)
            return cur.fetchall()
