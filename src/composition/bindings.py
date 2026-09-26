"""Allow-listed binding and readiness-probe registry.

A provider descriptor can bind a capability to two kinds of target:

* ``mcp-tool`` - a tool ID that the MCP catalog already registers
  (``<server>.<tool>``); validated against the generated catalog;
* ``registered`` - an in-process binding ID registered here by Noesis code.

Nothing in a manifest or descriptor can add an entry: registration happens only
in Python modules shipped with Noesis (see :mod:`src.composition.local_bindings`).
That is what "manifests cannot name arbitrary code to execute" means in
practice: descriptors reference IDs, and only IDs present in this registry or
in the catalog can ever be dispatched.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_ARTIFACT = REPO_ROOT / "contracts/generated/noesis-mcp-catalog-v1.json"


@dataclass(frozen=True)
class RegisteredBinding:
    """An allow-listed in-process operation.

    ``fn(ctx, arguments)`` performs it. ``arguments_schema`` validates the
    arguments; ``result_contract`` names the contract the result must carry;
    ``gate`` names a gate that must pass whichever pack reached the binding;
    ``lookup(ctx, idempotency_key)`` returns the owner's execution receipt for
    a prior call, or ``None`` when the owner recorded no effect.
    """

    binding_id: str
    fn: Callable[..., Mapping[str, Any]]
    description: str = ""
    arguments_schema: Mapping[str, Any] | None = None
    result_contract: str | None = None
    gate: str | None = None
    lookup: Callable[..., Mapping[str, Any] | None] | None = None
    result_path: str | None = None


@dataclass(frozen=True)
class RegisteredProbe:
    probe_id: str
    fn: Callable[..., Mapping[str, Any]]
    description: str = ""


_BINDINGS: dict[str, RegisteredBinding] = {}
_PROBES: dict[str, RegisteredProbe] = {}
_GATES: dict[str, Callable[..., bool]] = {}
_BUILTINS_LOADED = False


def register_binding(
    binding_id: str,
    description: str = "",
    *,
    arguments_schema: Mapping[str, Any] | None = None,
    result_contract: str | None = None,
    gate: str | None = None,
    lookup: Callable[..., Mapping[str, Any] | None] | None = None,
    result_path: str | None = None,
):
    """Decorator registering an in-process binding under ``binding_id``.

    ``result_path`` names the member of the result that carries
    ``result_contract`` when the binding wraps an owner document.
    """

    def decorate(fn):
        _BINDINGS[binding_id] = RegisteredBinding(
            binding_id, fn, description, arguments_schema, result_contract, gate, lookup, result_path)
        return fn

    return decorate


def register_gate(gate_id: str):
    """Decorator registering a gate predicate ``fn(ctx) -> bool``."""

    def decorate(fn):
        _GATES[gate_id] = fn
        return fn

    return decorate


def gate(gate_id: str) -> Callable[..., bool] | None:
    _load_builtins()
    return _GATES.get(gate_id)


def register_probe(probe_id: str, description: str = ""):
    """Decorator registering a readiness probe under ``probe_id``."""

    def decorate(fn):
        _PROBES[probe_id] = RegisteredProbe(probe_id, fn, description)
        return fn

    return decorate


def _load_builtins() -> None:
    global _BUILTINS_LOADED
    if not _BUILTINS_LOADED:
        _BUILTINS_LOADED = True
        import src.composition.local_bindings  # noqa: F401 - registers built-ins


def binding(binding_id: str) -> RegisteredBinding | None:
    _load_builtins()
    return _BINDINGS.get(binding_id)


def probe(probe_id: str) -> RegisteredProbe | None:
    _load_builtins()
    return _PROBES.get(probe_id)


def registered_binding_ids() -> frozenset[str]:
    _load_builtins()
    return frozenset(_BINDINGS)


def registered_probe_ids() -> frozenset[str]:
    _load_builtins()
    return frozenset(_PROBES)


@functools.lru_cache(maxsize=4)
def _catalog_tool_ids(path: str) -> frozenset[str]:
    catalog = json.loads(Path(path).read_text(encoding="utf-8"))
    return frozenset(tool["id"] for tool in catalog["tools"])


def catalog_tool_ids(path: str | Path = CATALOG_ARTIFACT) -> frozenset[str]:
    """Tool IDs the generated MCP catalog registers."""

    return _catalog_tool_ids(str(path))


__all__ = [
    "RegisteredBinding",
    "RegisteredProbe",
    "binding",
    "catalog_tool_ids",
    "gate",
    "probe",
    "register_binding",
    "register_gate",
    "register_probe",
    "registered_binding_ids",
    "registered_probe_ids",
]
