"""Small shared contracts for optional, reproducible backend execution."""

import hashlib
import importlib.metadata
import json
import math


class IntegrationError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def version(package):
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError as exc:
        raise IntegrationError(
            "backend_unavailable", f"Install the optional {package} dependency"
        ) from exc


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def finite(value, name, minimum=None, maximum=None):
    number = float(value)
    if (
        not math.isfinite(number)
        or (minimum is not None and number < minimum)
        or (maximum is not None and number > maximum)
    ):
        raise IntegrationError(
            "invalid_input", f"{name} is outside the finite supported range"
        )
    return number


def receipt(backend, package, request, result):
    producer = {"backend": backend, "version": version(package)}
    core = {"producer": producer, "request": request, "result": result}
    return {**core, "sha256": digest(core)}
