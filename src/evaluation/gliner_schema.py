"""Native combined GLiNER2 schemas with source-exact, bounded observations."""

from __future__ import annotations

import hashlib
import json
import math
import re

from src.evaluation.runtime_errors import BackendError
from src.evaluation.workflow_review import adapt_entity_spans


def _bad():
    raise BackendError(
        "invalid_model_output", "GLiNER schema output violates the source-span contract"
    )


def _name(value):
    return isinstance(value, str) and re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,63}", value)


def build_schema(model, specification):
    if (
        not isinstance(specification, dict)
        or not specification
        or specification.keys() - {"entities", "relations", "structures"}
    ):
        raise ValueError("bounded entities/relations/structures schema required")
    if len(json.dumps(specification, ensure_ascii=False).encode()) > 16384:
        raise ValueError("extraction schema exceeds 16 KiB")
    entities, relations, structures = (
        specification.get(key, {}) for key in ("entities", "relations", "structures")
    )
    if (
        any(not isinstance(value, dict) for value in (entities, relations, structures))
        or len(entities) + len(relations) > 64
        or len(structures) > 8
        or not (entities or relations or structures)
    ):
        raise ValueError("bounded nonempty task schemas required")
    if any(
        not _name(key) or not isinstance(value, str) or not 1 <= len(value) <= 500
        for rows in (entities, relations)
        for key, value in rows.items()
    ):
        raise ValueError("bounded labels and descriptions required")
    if structures.keys() & {"entities", "relation_extraction", *relations}:
        raise ValueError("structure names cannot overlap native result keys")
    count = 0
    for name, fields in structures.items():
        if (
            not _name(name)
            or not isinstance(fields, dict)
            or not fields
            or len(fields) > 32
        ):
            raise ValueError("bounded named structured fields required")
        count += len(fields)
        for field, policy in fields.items():
            if (
                not _name(field)
                or not isinstance(policy, dict)
                or set(policy) - {"dtype", "description"}
                or policy.get("dtype") not in {"str", "list"}
                or not isinstance(policy.get("description", ""), str)
                or len(policy.get("description", "")) > 500
            ):
                raise ValueError(
                    "only described extractive string/list fields are supported"
                )
    if count > 64:
        raise ValueError("at most 64 structured fields allowed")
    schema = model.create_schema()
    if entities:
        schema = schema.entities(entities)
    if relations:
        schema = schema.relations(relations)
    for name, fields in structures.items():
        schema = schema.structure(name)
        for field, policy in fields.items():
            schema = schema.field(
                field, dtype=policy["dtype"], description=policy.get("description")
            )
    return schema


def extract_schema(
    backend, text, specification, *, source_id, source_revision, language, threshold=0.5
):
    from src.evaluation.model_backends import bounded_texts

    bounded_texts([text], max_chars=backend.max_chars)
    if (
        not all(
            isinstance(v, str) and 0 < len(v) <= 1000
            for v in (source_id, source_revision, language)
        )
        or type(threshold) not in {int, float}
        or not math.isfinite(threshold)
        or not 0 < threshold < 1
    ):
        raise ValueError("source identity, language and finite threshold required")
    schema = build_schema(backend.model, specification)
    raw = backend.model.extract(
        text, schema, threshold=threshold, include_spans=True, include_confidence=True
    )
    entities, relations, structures = (
        specification.get(key, {}) for key in ("entities", "relations", "structures")
    )
    relation_aliases = {
        label + ": " + description: label for label, description in relations.items()
    }
    if not isinstance(raw, dict) or raw.keys() - {
        "entities",
        "relation_extraction",
        *structures,
        *relation_aliases,
    }:
        _bad()
    # Native GLiNER2 2.0.0 emits absent description-qualified relations at
    # the root. Only its observed empty-list shape may be ignored there;
    # populated relations still require endpoint validation below.
    for alias in raw.keys() & relation_aliases.keys():
        if not isinstance(raw[alias], list) or raw[alias]:
            _bad()
    emitted = 0

    def span(value):
        nonlocal emitted
        emitted += 1
        if emitted > 1024 or not isinstance(value, dict):
            _bad()
        start, end, confidence = (
            value.get("start"),
            value.get("end"),
            value.get("confidence"),
        )
        if (
            type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= len(text)
            or value.get("text") != text[start:end]
        ):
            _bad()
        if (
            type(confidence) not in {int, float}
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            _bad()
        return {
            "text": text[start:end],
            "start": start,
            "end": end,
            "confidence": confidence,
            "confidence_is_correctness": False,
        }

    def mapping(value, allowed):
        if not isinstance(value, dict) or value.keys() - set(allowed):
            _bad()
        return value

    def rows(value):
        if not isinstance(value, list) or len(value) > 1024:
            _bad()
        return value

    entity_spans = []
    for label, values in mapping(raw.get("entities", {}), entities).items():
        entity_spans.extend({**span(value), "label": label} for value in rows(values))
    result = adapt_entity_spans(
        source_id=source_id,
        source_revision=source_revision,
        text=text,
        model=backend.spec["model"],
        model_revision=backend.spec["revision"],
        language=language,
        entities=entity_spans,
        supported_labels=entities,
    )
    relation_rows, seen_relations = [], set()
    # GLiNER2 2.0.0 may return the exact description-qualified prompt label
    # alongside an empty canonical label. Resolve only aliases from this frozen
    # schema, never arbitrary strings split at a colon.
    relation_labels = {label: label for label in relations}
    relation_labels.update(relation_aliases)
    for native_label, values in mapping(
        raw.get("relation_extraction", {}), relation_labels
    ).items():
        label = relation_labels[native_label]
        for value in rows(values):
            if not isinstance(value, dict) or set(value) != {"head", "tail"}:
                _bad()
            head, tail = span(value["head"]), span(value["tail"])
            identity = (label, head["start"], head["end"], tail["start"], tail["end"])
            if identity in seen_relations:
                continue
            seen_relations.add(identity)
            relation_rows.append(
                {
                    "label": label,
                    "native_label": native_label,
                    "head": head,
                    "tail": tail,
                    "support_verified": False,
                }
            )
    structured, missing, missing_structures = {}, [], []
    for name, fields in structures.items():
        records = rows(raw.get(name, []))
        structured[name] = []
        if not records:
            missing_structures.append(name)
        for index, record in enumerate(records):
            mapping(record, fields)
            converted = {}
            for field, policy in fields.items():
                value = record.get(field)
                absent = value is None or value == "" or value == []
                if absent:
                    converted[field] = None if policy["dtype"] == "str" else []
                    missing.append({"structure": name, "record": index, "field": field})
                else:
                    converted[field] = (
                        span(value)
                        if policy["dtype"] == "str"
                        else [span(v) for v in rows(value)]
                    )
            structured[name].append(converted)
    result.update(
        relations=relation_rows,
        structures=structured,
        missing_fields=missing,
        missing_structures=missing_structures,
        threshold=threshold,
        support_verified=False,
        schema_sha256=hashlib.sha256(
            json.dumps(specification, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
        schema=specification,
        source_text_sha256=hashlib.sha256(text.encode()).hexdigest(),
        structured_contract="noesis-gliner-structured-spans-v1",
    )
    return result
