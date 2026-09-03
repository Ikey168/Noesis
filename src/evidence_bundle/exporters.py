"""Export public Noesis results as portable evidence bundles.

The exporters accept already-authorized results or an injected read
connection.  They do not open a warehouse themselves, which keeps private
domain authorization with the calling application.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any

from src.osint import common
from src.osint.evidence import citation, document_citations

from .builder import EvidenceBundleBuilder, EvidenceBundleError
from .canonical import sha256_digest


def _is_locator(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and "document_id" in value
        and bool({"cited", "path", "url", "source"} & set(value))
    )


def _private_locator(value: Mapping[str, Any]) -> bool:
    topics = value.get("topics")
    return (
        value.get("private") is True
        or str(value.get("visibility") or value.get("access") or "").lower()
        == "private"
        or isinstance(topics, list)
        and "private" in {str(item).lower() for item in topics}
    )


def _require_private_permission(
    value: Any, *, include_private: bool, visibility: str = "public"
) -> None:
    if visibility not in {"public", "private"}:
        raise EvidenceBundleError("visibility must be public or private")
    contains_private = visibility == "private" or any(
        _private_locator(locator) for locator in _locators(value)
    )
    if contains_private and not include_private:
        raise EvidenceBundleError(
            "private evidence requires explicit include_private=True authorization"
        )


def _locators(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    def visit(item: Any) -> None:
        if _is_locator(item):
            locator = deepcopy(item)
            marker = sha256_digest(locator)
            if marker not in seen:
                seen.add(marker)
                found.append(locator)
        if isinstance(item, dict):
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return found


def _prediction_models(value: Any) -> list[str]:
    models: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            mode = item.get("prediction_mode")
            if isinstance(mode, str) and mode.startswith(("pretrained:", "zero-shot:")):
                model = mode.split(":", 1)[1].strip()
                if model and model not in models:
                    models.append(model)
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return models


def _add_model_pins(builder: EvidenceBundleBuilder, value: Any) -> list[str]:
    from src.argument_mining.model_registry import read_lock

    lock = read_lock()
    by_model = {
        entry.get("model"): {"kind": kind, **entry}
        for kind, entry in lock.items()
        if isinstance(entry, dict) and entry.get("model")
    }
    refs: list[str] = []
    for model in _prediction_models(value):
        entry = by_model.get(model)
        if entry is None:
            payload = {"model": model, "status": "unresolved"}
            builder.add_omission(f"immutable model pin unavailable for {model}")
        else:
            payload = {
                "model": model,
                "kind": entry.get("kind"),
                "requested_revision": entry.get("requested_revision"),
                "resolved_revision": entry.get("resolved_revision"),
                "status": "pinned",
            }
        marker = sha256_digest(payload).split(":", 1)[1][:24]
        refs.append(
            builder.add_object("model_pin", payload, object_id=f"model-pin:{marker}")
        )
    return refs


def _add_evidence(builder: EvidenceBundleBuilder, locator: Mapping[str, Any]) -> str:
    normalized = deepcopy(dict(locator))
    document_id = normalized.get("document_id")
    normalized.setdefault("cited", bool(document_id))
    normalized.setdefault("source", normalized.get("source_id") or "unknown")
    normalized.setdefault("url", None)
    normalized.setdefault("path", str(document_id) if document_id else None)
    marker = sha256_digest(normalized).split(":", 1)[1][:24]
    return builder.add_object(
        "evidence", {"locator": normalized}, object_id=f"evidence:{marker}"
    )


def _statement_lists(payload: dict[str, Any]) -> list[list[dict[str, Any]]]:
    candidates: list[Any] = [payload.get("statements")]
    data = payload.get("data")
    if isinstance(data, dict):
        candidates.append(data.get("statements"))
    return [
        value
        for value in candidates
        if isinstance(value, list) and all(isinstance(row, dict) for row in value)
    ]


def export_answer(
    answer: Mapping[str, Any],
    *,
    inputs: Mapping[str, Any] | None = None,
    created_at_ms: int | None = None,
    as_of_ms: int | None = None,
    omissions: Iterable[str] = (),
    include_private: bool = False,
) -> dict[str, Any]:
    """Export a structured Answer-v1-like response.

    Evidence locators found under each statement become bundle-local evidence
    objects and the statement receives ``evidence_refs``.  The original
    response remains intact otherwise.
    """
    payload = deepcopy(dict(answer))
    _require_private_permission(payload, include_private=include_private)
    statements = _statement_lists(payload)
    if not statements:
        raise EvidenceBundleError("answer export requires a structured statements list")
    builder = EvidenceBundleBuilder(
        "answer",
        {**dict(inputs or {}), "private_evidence_included": bool(include_private)},
        created_at_ms=created_at_ms,
        as_of_ms=as_of_ms,
    )
    all_refs: list[str] = []
    for rows in statements:
        for statement in rows:
            refs = [_add_evidence(builder, item) for item in _locators(statement)]
            statement["evidence_refs"] = sorted(dict.fromkeys(refs))
            all_refs.extend(refs)
    for item in _locators(payload):
        all_refs.append(_add_evidence(builder, item))
    all_refs.extend(_add_model_pins(builder, payload))
    for reason in omissions:
        builder.add_omission(str(reason))
    builder.add_object(
        "answer",
        payload,
        object_id="answer:root",
        references=all_refs,
        root=True,
    )
    return builder.build()


def _claim_row(conn: Any, claim_id: str) -> dict[str, Any]:
    if not common.table_exists(conn, "argument_claims"):
        raise EvidenceBundleError("claim layer is unavailable")
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info('argument_claims')").fetchall()
    }
    wanted = (
        "claim_id",
        "claim_text",
        "document_id",
        "source_type",
        "confidence",
        "prediction_mode",
        "factcheck_verdict",
        "extracted_at",
    )
    expressions = [name if name in columns else f"NULL AS {name}" for name in wanted]
    row = conn.execute(
        f"SELECT {', '.join(expressions)} FROM argument_claims WHERE claim_id = ?",
        [claim_id],
    ).fetchone()
    if row is None:
        raise EvidenceBundleError(f"claim {claim_id!r} not found")
    return dict(zip(wanted, row))


def _claim_evidence_document_ids(conn: Any, claim_id: str) -> list[str]:
    ids: list[str] = []
    if common.table_exists(conn, "claim_evidence"):
        ids.extend(
            row[0]
            for row in conn.execute(
                "SELECT evidence_document_id FROM claim_evidence WHERE claim_id = ?",
                [claim_id],
            ).fetchall()
            if row[0]
        )
    if common.table_exists(conn, "claim_conflicts"):
        other_ids = [
            row[0]
            for row in conn.execute(
                "SELECT CASE WHEN claim_id_a = ? THEN claim_id_b ELSE claim_id_a END "
                "FROM claim_conflicts WHERE claim_id_a = ? OR claim_id_b = ?",
                [claim_id, claim_id, claim_id],
            ).fetchall()
            if row[0]
        ]
        ids.extend(
            info.get("document_id")
            for info in common.claim_sources(conn, other_ids).values()
            if info.get("document_id")
        )
    return list(dict.fromkeys(ids))


def export_claim(
    conn: Any,
    claim_id: str,
    *,
    created_at_ms: int | None = None,
    as_of_ms: int | None = None,
    visibility: str = "public",
    include_private: bool = False,
) -> dict[str, Any]:
    """Export a claim plus its corroboration and citation closure."""
    from src.osint.corroboration import corroborate

    claim = _claim_row(conn, claim_id)
    _require_private_permission(
        claim, include_private=include_private, visibility=visibility
    )
    builder = EvidenceBundleBuilder(
        "claim",
        {"claim_id": claim_id, "private_evidence_included": bool(include_private)},
        created_at_ms=created_at_ms,
        as_of_ms=as_of_ms,
    )
    own = common.claim_sources(conn, [claim_id]).get(claim_id, {})
    own_locator = citation(
        own.get("document_id") or claim.get("document_id"),
        own.get("source"),
        own.get("url"),
        resolved=own.get("resolved", False),
    )
    document_ids = _claim_evidence_document_ids(conn, claim_id)
    locators = [own_locator, *document_citations(conn, document_ids).values()]
    refs = [_add_evidence(builder, item) for item in locators]
    payload = {
        **claim,
        "corroboration": corroborate(conn, claim_id),
        "evidence_refs": sorted(dict.fromkeys(refs)),
        "citation_state": "cited"
        if any(item.get("cited") for item in locators)
        else "uncited",
    }
    if payload["citation_state"] == "uncited":
        builder.add_omission(
            "claim source did not resolve to an ingested document",
            object_id=f"claim:{claim_id}",
        )
    refs.extend(_add_model_pins(builder, payload))
    builder.add_object(
        "claim", payload, object_id=f"claim:{claim_id}", references=refs, root=True
    )
    return builder.build()


def export_integrity(
    conn: Any,
    document_id: str,
    *,
    created_at_ms: int | None = None,
    as_of_ms: int | None = None,
    visibility: str = "public",
    include_private: bool = False,
) -> dict[str, Any]:
    """Export one document's complete integrity-ledger view."""
    from src.integrity.ledger import document_integrity

    payload = document_integrity(conn, document_id)
    if payload.get("status") == "not_found":
        raise EvidenceBundleError(f"document {document_id!r} not found")
    _require_private_permission(
        payload, include_private=include_private, visibility=visibility
    )
    builder = EvidenceBundleBuilder(
        "integrity",
        {
            "document_id": document_id,
            "private_evidence_included": bool(include_private),
        },
        created_at_ms=created_at_ms,
        as_of_ms=as_of_ms,
    )
    evidence_refs = [_add_evidence(builder, item) for item in _locators(payload)]
    refs = [*evidence_refs, *_add_model_pins(builder, payload)]
    payload = deepcopy(payload)
    payload["evidence_refs"] = sorted(dict.fromkeys(evidence_refs))
    builder.add_object(
        "integrity",
        payload,
        object_id=f"integrity:{document_id}",
        references=refs,
        root=True,
    )
    return builder.build()


def export_receipt(
    receipt: Mapping[str, Any],
    *,
    inputs: Mapping[str, Any] | None = None,
    created_at_ms: int | None = None,
    include_private: bool = False,
) -> dict[str, Any]:
    """Export an existing machine-checkable receipt, including the showcase."""
    payload = deepcopy(dict(receipt))
    _require_private_permission(payload, include_private=include_private)
    builder = EvidenceBundleBuilder(
        "receipt",
        {**dict(inputs or {}), "private_evidence_included": bool(include_private)},
        created_at_ms=created_at_ms,
    )
    refs = [_add_evidence(builder, item) for item in _locators(payload)]
    refs.extend(_add_model_pins(builder, payload))
    builder.add_object(
        "receipt", payload, object_id="receipt:root", references=refs, root=True
    )
    return builder.build()
