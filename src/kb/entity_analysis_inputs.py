"""Bind optional linkage fields to immutable authorized evidence, not caller text.

The choice of canonical entity and entity type is an operator's review hypothesis,
not a model-verified identity fact. This module creates no parallel source store.
"""

from __future__ import annotations

import copy
import json
import math
import re

from src.evaluation.entity_backends import _hash, _record, _safeguards
from src.evaluation.runtime_errors import BackendError
from src.kb.entity_history import EntityHistoryError, EntityHistoryStore

CONTRACT = "noesis-entity-source-binding-v1"
OPERATIONS = frozenset({"rapidfuzz", "splink"})


def _invalid():
    raise BackendError(
        "source_identity", "entity fields require exact captured source selectors"
    )


def _pointer(value, pointer):
    if (
        not isinstance(pointer, str)
        or len(pointer) > 2000
        or not pointer.startswith("/")
    ):
        _invalid()
    tokens = pointer[1:].split("/")
    if len(tokens) > 32:
        _invalid()
    try:
        for token in tokens:
            if re.search(r"~(?![01])", token):
                _invalid()
            key = token.replace("~1", "/").replace("~0", "~")
            if isinstance(value, list):
                if not re.fullmatch(r"0|[1-9][0-9]{0,8}", key):
                    _invalid()
                value = value[int(key)]
            elif isinstance(value, dict):
                value = value[key]
            else:
                _invalid()
    except (KeyError, IndexError):
        _invalid()
    return value


def _select(selector, sources):
    required = {"document_id", "revision_id", "pointer"}
    if (
        not isinstance(selector, dict)
        or not required <= selector.keys()
        or selector.keys() - required - {"start", "end", "json_pointer"}
    ):
        _invalid()
    key = (selector["document_id"], selector["revision_id"])
    if not all(isinstance(v, str) for v in key) or key not in sources:
        _invalid()
    # Evidence comes only from stored content/metadata, not ingest/run machinery.
    if not isinstance(selector["pointer"], str) or (
        selector["pointer"] != "/content"
        and not selector["pointer"].startswith("/metadata/")
    ):
        _invalid()
    value = _pointer(sources[key], selector["pointer"])
    if "json_pointer" in selector:
        if not isinstance(value, str) or len(value) > 1_000_000:
            _invalid()
        try:
            value = _pointer(json.loads(value), selector["json_pointer"])
        except (ValueError, RecursionError):
            _invalid()
    if not isinstance(value, str):
        _invalid()
    if "start" in selector or "end" in selector:
        start, end = selector.get("start"), selector.get("end")
        if (
            type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= len(value)
        ):
            _invalid()
        value = value[start:end]
    if (
        not value
        or len(value) > 1000
        or any(0xD800 <= ord(ch) <= 0xDFFF for ch in value)
    ):
        _invalid()
    return value


def _identity(history, namespace, identity):
    try:
        state = history._entity(namespace, identity)
    except EntityHistoryError as exc:
        raise BackendError(
            "source_identity", "candidate requires an existing canonical identity"
        ) from exc
    if state["status"] != "active":
        raise BackendError(
            "source_identity", "candidate requires an active canonical identity"
        )
    return _hash(state)


def prepare_inputs(conn, namespace, operation, payload, sources):
    """Resolve selectors before calling the native scorer and freeze its inputs."""
    history = EntityHistoryStore(conn)
    selections = (
        [payload.get("source"), *(payload.get("candidates") or [])]
        if operation == "rapidfuzz"
        else payload.get("records")
    )
    if not isinstance(selections, list) or not 2 <= len(selections) <= (
        1001 if operation == "rapidfuzz" else 200
    ):
        _invalid()
    records, identities = [], {}
    for selection in selections:
        if not isinstance(selection, dict) or set(selection) != {
            "id",
            "type",
            "field_sources",
        }:
            _invalid()
        identity, fields = selection["id"], selection["field_sources"]
        if (
            not isinstance(identity, str)
            or not identity
            or len(identity) > 1000
            or identity in identities
        ):
            _invalid()
        if (
            not isinstance(fields, dict)
            or "name" not in fields
            or fields.keys()
            - {"name", "aliases", "identifiers", "affiliation", "address"}
        ):
            _invalid()
        aliases, identifiers = fields.get("aliases", []), fields.get("identifiers", {})
        if (
            not isinstance(aliases, list)
            or len(aliases) > 20
            or not isinstance(identifiers, dict)
            or len(identifiers) > 20
        ):
            _invalid()
        record = {
            "id": identity,
            "type": selection["type"],
            "name": _select(fields["name"], sources),
            "aliases": [_select(v, sources) for v in aliases],
            "identifiers": {k: _select(v, sources) for k, v in identifiers.items()},
            "provenance": {
                "field_sources": copy.deepcopy(fields),
                "offset_unit": "unicode-codepoint",
            },
        }
        for field in ("affiliation", "address"):
            if field in fields:
                record[field] = _select(fields[field], sources)
        record["revision"] = "entity-input:" + _hash(record)
        _record(
            record
        )  # Share native bounds/type validation, not a permissive second schema.
        identities[identity] = _identity(history, namespace, identity)
        records.append(record)
    prepared = {**payload}
    if operation == "rapidfuzz":
        prepared.update(source=records[0], candidates=records[1:])
        input_hash = _hash([records[0], records[1:]])
    else:
        prepared["records"] = records
        input_hash = _hash(records)
    binding = {
        "contract": CONTRACT,
        "identity_assignment": "operator-selected-review-hypothesis",
        "type_assignment": "operator-declared-not-verified",
        "records": records,
        "canonical_state_hashes": identities,
        "input_sha256": input_hash,
    }
    binding["sha256"] = _hash(binding)
    return prepared, binding


def check_current(conn, namespace, binding):
    if (
        not isinstance(binding, dict)
        or binding.get("contract") != CONTRACT
        or binding.get("sha256")
        != _hash({k: v for k, v in binding.items() if k != "sha256"})
    ):
        raise BackendError(
            "source_identity", "candidate source binding is missing or changed"
        )
    history = EntityHistoryStore(conn)
    for identity, expected in binding["canonical_state_hashes"].items():
        try:
            current = _identity(history, namespace, identity)
        except BackendError as exc:
            raise BackendError(
                "source_changed", "canonical identity changed since scoring"
            ) from exc
        if current != expected:
            raise BackendError(
                "source_changed", "canonical identity changed since scoring"
            )


def bind_output(operation, output, binding):
    """Reject model-substituted identities/revisions and attach input provenance."""

    def invalid():
        raise BackendError(
            "invalid_model_output", "candidate output differs from its captured inputs"
        )

    records = binding["records"]
    by_id = {row["id"]: _record(row) for row in records}
    if (
        not isinstance(output, dict)
        or output.get("input_sha256") != binding["input_sha256"]
    ):
        invalid()
    rows = output.get("candidates")
    if not isinstance(rows, list) or len(rows) > len(records) * (len(records) - 1) // 2:
        invalid()
    if operation == "rapidfuzz" and output.get("source_id") != records[0]["id"]:
        invalid()
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            invalid()
        left_id = row.get("left_id") if operation == "splink" else output["source_id"]
        right_id = row.get("right_id") if operation == "splink" else row.get("id")
        if (
            not isinstance(left_id, str)
            or not isinstance(right_id, str)
            or left_id not in by_id
            or right_id not in by_id
            or left_id == right_id
        ):
            invalid()
        pair = tuple(sorted((left_id, right_id)))
        if pair in seen:
            invalid()
        seen.add(pair)
        left, right = by_id[left_id], by_id[right_id]
        if (
            row.get("left_revision" if operation == "splink" else "source_revision")
            != left["revision"]
            or row.get("right_revision" if operation == "splink" else "revision")
            != right["revision"]
        ):
            invalid()
        score = row.get("score")
        if (
            type(score) not in {int, float}
            or not math.isfinite(score)
            or not 0 <= score <= 1
        ):
            invalid()
        for key, value in _safeguards(left, right).items():
            if type(row.get(key)) is not type(value) or row[key] != value:
                invalid()
    return {**output, "source_binding": copy.deepcopy(binding)}
