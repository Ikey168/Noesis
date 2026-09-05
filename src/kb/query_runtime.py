"""Process-wide bounded workers and isolated DuckDB handles for query deadlines."""

import concurrent.futures
import copy
import threading

_WORKERS = 8
_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="noesis-query")
_SLOTS = threading.BoundedSemaphore(_WORKERS)


def _isolate(adapter):
    import duckdb
    connections = []

    def clone(value, depth=0):
        result = copy.copy(value)
        attributes = vars(value) if hasattr(value, "__dict__") else {}
        for name, item in attributes.items():
            if isinstance(item, duckdb.DuckDBPyConnection):
                databases = item.execute("PRAGMA database_list").fetchall()
                path = next((row[2] for row in databases if row[2]), None)
                if path:
                    read_only = str(item.execute("SELECT current_setting('access_mode')").fetchone()[0]).lower() == "read_only"
                    connection = duckdb.connect(path, read_only=read_only)
                else:
                    # In-memory callers own the parent lifetime; file-backed MCP
                    # calls get an independent handle that survives request close.
                    connection = item.cursor()
                connections.append(connection)
                setattr(result, name, connection)
            elif depth == 0 and name in {"store", "backing"}:
                setattr(result, name, clone(item, depth + 1))
        return result

    try:
        return clone(adapter), connections
    except Exception:
        for connection in connections:
            connection.close()
        raise


def submit(adapter, invoke):
    """No unbounded executor queue: saturated providers cause immediate backpressure."""
    if not _SLOTS.acquire(blocking=False):
        return None
    connections = []
    try:
        isolated, connections = _isolate(adapter)
        future = _POOL.submit(invoke, isolated)
    except Exception:
        for connection in connections:
            connection.close()
        _SLOTS.release()
        raise

    def finished(_):
        try:
            for connection in connections:
                connection.close()
        finally:
            _SLOTS.release()
    future.add_done_callback(finished)
    return future
