"""Attributable Wikidata statement observations; canonical entities are not edited."""

import hashlib
import json
import re
import time

from services.ingest.common.document_model import Document
from src.ingestion.document_store import DocumentStore
from src.ingestion.snapshots import SnapshotStore
from src.ingestion.source_pack_runtime import HTTPSPageAdapter


def normalize_entity(payload, entity_id, *, properties, languages, max_statements=100):
    entities = payload.get("entities", {})
    entity = entities.get(entity_id)
    if not isinstance(entity, dict) or "missing" in entity:
        return {"status": "unavailable", "entity_id": entity_id, "statements": []}
    if entity.get("id") != entity_id or not isinstance(entity.get("lastrevid"), int):
        raise ValueError("Wikidata entity identity or revision is invalid")
    claims = entity.get("claims", {})
    if not isinstance(claims, dict):
        raise ValueError("Wikidata claims are invalid")  # noqa: TRY004 - provider schema failure
    selected, seen = [], set()
    for prop in properties:
        statements = claims.get(prop, [])
        if not isinstance(statements, list):
            raise ValueError("Wikidata property lacks statement collection")  # noqa: TRY004
        for statement in statements:
            if len(selected) >= max_statements:
                raise ValueError("Wikidata statement limit exceeded; narrow properties")
            sid = statement.get("id")
            if (
                not isinstance(sid, str)
                or not sid.casefold().startswith(entity_id.casefold() + "$")
                or sid.casefold() in seen
            ):
                raise ValueError("invalid or duplicate Wikidata statement identity")
            seen.add(sid.casefold())
            snak = statement.get("mainsnak", {})
            if snak.get("property") != prop or snak.get("snaktype") not in {
                "value",
                "novalue",
                "somevalue",
            }:
                raise ValueError("invalid Wikidata statement value")
            rank = statement.get("rank")
            if rank not in {"normal", "preferred", "deprecated"}:
                raise ValueError("invalid Wikidata rank")
            references = statement.get("references", [])
            if not isinstance(references, list):
                raise ValueError("invalid Wikidata references")  # noqa: TRY004
            selected.append(
                {
                    **statement,
                    "reference_status": "referenced" if references else "unreferenced",
                    "provider": "wikidata",
                    "entity_revision": entity["lastrevid"],
                }
            )
    labels, aliases = {}, {}
    for language in languages:
        label = entity.get("labels", {}).get(language, {}).get("value")
        if label is not None:
            labels[language] = label
        aliases[language] = [
            a["value"] for a in entity.get("aliases", {}).get(language, [])
        ]
    return {
        "status": "available",
        "entity_id": entity_id,
        "revision": entity["lastrevid"],
        "modified": entity.get("modified"),
        "labels": labels,
        "aliases": aliases,
        "statements": selected,
        "match_basis": "explicit_wikidata_identifier",
        "canonical_update": "review_required",
    }


def acquire_entity(
    conn,
    entity_id,
    *,
    properties,
    request_id,
    languages=("de", "en"),
    candidate_entity_ids=(),
    transport=None,
    max_bytes=5_000_000,
    max_statements=100,
    timeout_s=15,
    revision=None,
):
    """Acquire one explicit entity and bounded property set as source evidence.

    Potential local matches remain candidates. This function never calls the
    canonical entity resolver or executes identity merges. New request IDs make
    new observations; old IDs replay without network access.
    """
    if (
        not re.fullmatch(r"Q[1-9]\d*", entity_id)
        or not request_id
        or not 1 <= len(properties) <= 20
        or len(set(properties)) != len(properties)
        or any(not re.fullmatch(r"P[1-9]\d*", p) for p in properties)
        or not 1 <= len(languages) <= 5
        or any(not re.fullmatch(r"[a-z]{2}", l) for l in languages)
        or not 1 <= max_statements <= 1000
        or not 1 <= max_bytes <= 20_000_000
        or not 0 < timeout_s <= 60
        or len(candidate_entity_ids) > 20
        or (revision is not None and (type(revision) is not int or revision <= 0))
    ):
        raise ValueError("invalid Wikidata acquisition bounds")
    inputs = [
        entity_id,
        list(properties),
        list(languages),
        list(candidate_entity_ids),
        max_bytes,
        max_statements,
        timeout_s,
        revision,
    ]
    key = hashlib.sha256(json.dumps(inputs).encode()).hexdigest()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS wikidata_acquisitions "
        "(request_id TEXT PRIMARY KEY, input_hash TEXT, receipt TEXT)"
    )
    prior = conn.execute(
        "SELECT input_hash, receipt FROM wikidata_acquisitions WHERE request_id=?",
        [request_id],
    ).fetchone()
    if prior:
        if prior[0] != key:
            raise ValueError("Wikidata request ID already used with different inputs")
        return json.loads(prior[1])
    url = f"https://www.wikidata.org/wiki/Special:EntityData/{entity_id}.json"
    response = (transport or HTTPSPageAdapter._request)(
        url=url,
        params={"revision": revision} if revision is not None else {},
        headers={
            "Accept": "application/json",
            "User-Agent": "Noesis/0.1 (https://github.com/Ikey168/Noesis)",
        },
        timeout=timeout_s,
        max_bytes=max_bytes,
    )
    status = int(response.get("status", 200))
    if status not in {200, 404}:
        raise ValueError(f"Wikidata returned HTTP {status}; no automatic retry")
    raw = response.get("content", b"")
    raw = raw.encode() if isinstance(raw, str) else bytes(raw)
    if len(raw) > max_bytes:
        raise ValueError("Wikidata response exceeds byte budget")
    result = (
        normalize_entity(
            json.loads(raw),
            entity_id,
            properties=properties,
            languages=languages,
            max_statements=max_statements,
        )
        if status == 200
        else {"status": "unavailable", "entity_id": entity_id, "statements": []}
    )
    if revision is not None and result.get("revision", revision) != revision:
        raise ValueError("Wikidata returned a different revision")
    now = int(time.time() * 1000)
    result.update(
        request_id=request_id,
        retrieved_at_ms=now,
        candidate_entity_ids=list(candidate_entity_ids),
        local_match_status="ambiguous"
        if len(candidate_entity_ids) > 1
        else "unreviewed",
        adapter_version="wikidata-entity-json/noesis-v1",
    )
    store, snapshots = DocumentStore(conn), SnapshotStore(conn)
    conn.execute("BEGIN TRANSACTION")
    try:
        if result["status"] == "available":
            snapshot = snapshots.snapshot_bytes(
                url, raw, now, content_type="application/json", final_url=url
            )
            # Stable identity per selection: property scope changes cannot erase
            # evidence from an earlier independently requested selection.
            selection = hashlib.sha256(
                json.dumps([sorted(properties), list(languages)]).encode()
            ).hexdigest()[:12]
            doc = Document(
                document_id=f"wikidata:{entity_id}:{selection}",
                source_type="web",
                language=languages[0],
                ingested_at=now,
                source_id=f"wikidata:{entity_id}",
                url=f"https://www.wikidata.org/wiki/{entity_id}",
                title=result["labels"].get(languages[0], entity_id),
                content=json.dumps(
                    {
                        k: result[k]
                        for k in (
                            "entity_id",
                            "revision",
                            "labels",
                            "aliases",
                            "statements",
                        )
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                metadata={
                    "provider": "wikidata",
                    "provider_revision": result["revision"],
                    "snapshot_digest": snapshot["digest"],
                    "license": "CC0-1.0",
                    "match_status": result["local_match_status"],
                    "candidate_entity_ids": list(candidate_entity_ids),
                    "canonical_update": "review_required",
                },
            )
            outcome = store.upsert([doc])
            if outcome.invalid:
                raise ValueError("Wikidata evidence failed document validation")
            result.update(
                document_id=doc.document_id, snapshot_digest=snapshot["digest"]
            )
        conn.execute(
            "INSERT INTO wikidata_acquisitions VALUES (?,?,?)",
            [request_id, key, json.dumps(result)],
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return result
