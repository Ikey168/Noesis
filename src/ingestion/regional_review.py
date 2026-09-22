"""Review captured registry links and provider matches in the existing inbox."""

from __future__ import annotations

import json
import re

from src.ingestion.document_store import DocumentStore
from src.ingestion.provider_execution import ProviderError, digest
from src.kb.entity_history import EntityHistoryStore
from src.kb.review_inbox import ReviewInboxStore


def _identifier(value):
    """Only explicit supported identifiers become external record identities."""
    if not isinstance(value, str):
        raise TypeError("a plain explicit registry/publication identifier is required")
    if re.fullmatch(r"DRKS[0-9]{8}", value):
        return "drks:" + value
    if re.fullmatch(r"[0-9]{4}-[0-9]{6}-[0-9]{2}(?:-[0-9]{2})?", value):
        return "ctis:" + value
    doi = re.sub(
        r"^(?:https://(?:dx\.)?doi\.org/|doi:)", "", value, flags=re.IGNORECASE
    )
    if len(doi) <= 2000 and re.fullmatch(r"10\.[0-9]{4,9}/[^\s]+", doi):
        return "doi:" + doi.lower()
    raise ValueError("unsupported explicit identifier; retain it as source metadata")


def queue_candidate(
    conn,
    *,
    namespace,
    principal_id,
    scopes,
    observation_id,
    record_index,
    domain,
    relationship_index=None,
    entity_id=None,
    entity_sources=None,
    impact=0.5,
    uncertainty=0.5,
):
    """Queue a selected source assertion; canonical assignment stays a hypothesis.

    A sanctions query ID is not treated as a local entity ID. The coordinator
    must explicitly select an existing entity and its captured source revisions.
    """
    required = {
        "knowledge:ingestion:execute",
        "knowledge:entity-history:write",
        "knowledge:entity-history:review",
        "knowledge:entity-history:read",
        "knowledge:inbox:write",
        "knowledge:inbox:read",
        f"namespace:{namespace}:write",
    }
    if not principal_id or "operator" not in scopes and not required <= scopes:
        raise ProviderError("unauthorized", "regional candidate review scopes required")
    row = conn.execute(
        "SELECT receipt_json FROM regional_observation_receipts WHERE observation_id=? AND namespace=?",
        [observation_id, namespace],
    ).fetchone()
    if row is None:
        raise ProviderError("source_unavailable", "regional observation unavailable")
    receipt = json.loads(row[0])
    refs = receipt.get("source_refs", [])
    if type(record_index) is not int or not 0 <= record_index < len(refs):
        raise ValueError("select a captured record with an exact revision reference")
    source = refs[record_index]
    documents, inbox = DocumentStore(conn), ReviewInboxStore(conn)
    source_refs = [source, *(entity_sources or [])]
    inbox._sources(source_refs, scopes)
    for ref in source_refs:
        current = documents.revisions.revision(ref["document_id"])
        if (
            not current
            or current["revision_id"] != ref["revision_id"]
            or current["lifecycle"] != "active"
        ):
            raise ProviderError(
                "source_changed", "review requires current active source revisions"
            )
    document = documents.get(source["document_id"])
    record = json.loads(document["metadata"]["provider_record_json"])
    history = EntityHistoryStore(conn)
    aliases = {}
    if record["provider"] in {"ctis", "drks"}:
        relationships = record["relationships"]
        if (
            type(relationship_index) is not int
            or not 0 <= relationship_index < len(relationships)
            or entity_id is not None
            or entity_sources
        ):
            raise ValueError("select one captured cross-registry relationship")
        candidate = relationships[relationship_index]
        if candidate["relation"] != "explicit-cross-registry-or-publication-id":
            raise ValueError("only explicit registry/publication links are supported")
        subjects = [
            _identifier(record["provider_id"]),
            _identifier(candidate["identifier"]),
        ]
        aliases[subjects[0]] = [record["title"]]
    elif record["provider"] == "opensanctions":
        if not entity_id or not entity_sources or relationship_index is not None:
            raise ValueError("select an existing local entity and its source revisions")
        history._entity(namespace, entity_id)
        candidate = {
            **record["fields"],
            "local_entity_assignment": "explicit-coordinator-review-hypothesis",
        }
        subjects = [entity_id, "opensanctions:" + candidate["entity_id"]]
        aliases[subjects[1]] = [record["title"]]
    else:
        raise ValueError("record does not contain a supported review candidate")
    if subjects[0] == subjects[1]:
        raise ValueError("a self-reference does not establish a match candidate")
    # Validate all inbox inputs before creating any identity/decision rows.
    import math

    if not isinstance(domain, str) or not domain.strip() or len(domain) > 10000:
        raise ValueError("a bounded review domain is required")
    if any(
        type(v) not in {int, float} or not math.isfinite(v) or not 0 <= v <= 1
        for v in (impact, uncertainty)
    ):
        raise ValueError(
            "impact and uncertainty must be finite values from zero to one"
        )
    key = digest(
        [
            namespace,
            principal_id,
            observation_id,
            record_index,
            candidate,
            subjects,
            source_refs,
            domain,
            impact,
            uncertainty,
        ]
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS regional_review_tasks(candidate_key TEXT PRIMARY KEY, task_id TEXT NOT NULL)"
    )
    previous = conn.execute(
        "SELECT task_id FROM regional_review_tasks WHERE candidate_key=?", [key]
    ).fetchone()
    if previous:
        return inbox.inspect(
            namespace, previous[0], principal_id=principal_id, scopes=scopes
        )
    conn.execute("BEGIN")
    try:
        for subject in subjects:
            history.register_entity(
                namespace,
                subject,
                aliases.get(subject, []),
                principal_id=principal_id,
                scopes=scopes,
            )
        decision = history.decide(
            namespace,
            "review",
            subjects,
            {
                "origin": "provider",
                "candidate": candidate,
                "sources": source_refs,
                "observation_id": observation_id,
                "automatic_merge": False,
                "observed_at_ms": receipt["native_capture"]["observed_at_ms"],
            },
            reviewer_id=principal_id,
            principal_id=principal_id,
            scopes=scopes,
            event_key="regional-candidate:" + key,
        )
        task = inbox.create(
            namespace,
            {"kind": "entity", "namespace": namespace, "id": decision["decision_id"]},
            sources=source_refs,
            domain=domain,
            impact=impact,
            uncertainty=uncertainty,
            rationale="Captured provider link or match; identity requires independent review.",
            principal_id=principal_id,
            scopes=scopes,
            related_groups=["regional-identity:" + subject for subject in subjects],
        )
        conn.execute(
            "INSERT INTO regional_review_tasks VALUES (?,?)", [key, task["task_id"]]
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return task
