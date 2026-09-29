"""Predeclared coverage and provenance checks for typed decision benchmarks."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

CONTENT_TYPES = ("news", "blog", "paper", "transcript", "book", "note")
CASE_KINDS = ("missing_evidence", "quotation", "negation", "embedded_instruction")
LENGTH_BUCKETS = ("short", "medium", "long")


def validate_manifest(value: Mapping[str, Any]) -> dict:
    if not isinstance(value, Mapping) or set(value) != {
        "content_types",
        "languages",
        "length_buckets",
        "case_kinds",
        "min_label_support",
    }:
        raise ValueError(
            "explicit content, language, length, scenario and rare-label requirements required"
        )
    manifest = dict(value)
    for key, allowed in (
        ("content_types", CONTENT_TYPES),
        ("length_buckets", LENGTH_BUCKETS),
        ("case_kinds", CASE_KINDS),
    ):
        items = manifest[key]
        if (
            not isinstance(items, list)
            or not items
            or len(items) != len(set(items))
            or not set(items) <= set(allowed)
        ):
            raise ValueError(f"invalid {key} coverage requirement")
    languages = manifest["languages"]
    if (
        not isinstance(languages, list)
        or not languages
        or len(languages) != len(set(languages))
        or any(not isinstance(item, str) or not item for item in languages)
    ):
        raise ValueError("explicit bounded language coverage required")
    support = manifest["min_label_support"]
    if type(support) is not int or not 1 <= support <= 10000:
        raise ValueError("positive minimum rare-label support required")
    return manifest


def validate_case_provenance(
    row: Mapping[str, Any], *, model_version: str, rubric_version: str
) -> None:
    if (
        row.get("model_version") != model_version
        or row.get("rubric_version") != rubric_version
    ):
        raise ValueError("case model or rubric differs from frozen benchmark")
    if (
        row.get("length_bucket") not in LENGTH_BUCKETS
        or row.get("case_kind") not in CASE_KINDS
    ):
        raise ValueError("case length and scenario identity required")
    bindings = row.get("source_binding")
    if not isinstance(bindings, list) or not 1 <= len(bindings) <= 20:
        raise ValueError("one to 20 exact source bindings required")
    for binding in bindings:
        if (
            not isinstance(binding, Mapping)
            or not isinstance(binding.get("content_hash"), str)
            or not binding["content_hash"]
        ):
            raise ValueError("source binding needs an exact content hash")
        kind = binding.get("kind")
        if kind == "document_revision":
            identity = (binding.get("document_id"), binding.get("revision_id"))
        elif kind == "inbox_item_version":
            identity = (binding.get("item_id"), binding.get("source_version"))
        elif kind == "user_input_version":
            identity = (binding.get("input_id"), binding.get("version"))
        else:
            raise ValueError("unsupported exact source binding kind")
        if any(value is None or value == "" for value in identity):
            raise ValueError("source binding needs pinned identity and version")


def coverage_report(
    rows: Sequence[Mapping[str, Any]],
    labels: Sequence[str],
    manifest: Mapping[str, Any],
) -> dict:
    requirements = validate_manifest(manifest)
    observed = {
        "content_types": Counter(row["content_type"] for row in rows),
        "languages": Counter(row["language"] for row in rows),
        "length_buckets": Counter(row["length_bucket"] for row in rows),
        "case_kinds": Counter(row["case_kind"] for row in rows),
        "labels": Counter(row["truth"] for row in rows),
    }
    missing = {
        key: [value for value in requirements[key] if observed[key][value] == 0]
        for key in ("content_types", "languages", "length_buckets", "case_kinds")
    }
    missing["rare_labels"] = [
        label
        for label in labels
        if observed["labels"][label] < requirements["min_label_support"]
    ]
    return {
        "required": requirements,
        "observed": {
            key: dict(sorted(counts.items())) for key, counts in observed.items()
        },
        "missing": missing,
        "complete": not any(missing.values()),
    }
