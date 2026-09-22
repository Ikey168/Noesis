"""Serialize retention guards with pin/hold/source mutations in the DuckDB writer."""
from functools import wraps
from threading import RLock

_LOCK=RLock()


def coordinated(operation):
    @wraps(operation)
    def run(*args,**kwargs):
        with _LOCK:
            return operation(*args,**kwargs)
    return run
