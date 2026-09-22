"""Public ORCID v3 author observations and reviewable identity candidates."""

import hashlib
import json
import re
import time

from services.ingest.common.document_model import Document
from src.ingestion.document_store import DocumentStore
from src.ingestion.snapshots import SnapshotStore
from src.ingestion.source_pack_runtime import HTTPSPageAdapter
from src.integrations.common import IntegrationError, digest
from src.knowledge_graph.foundation import EntityType, Node, make_node_id


def validate_orcid(value):
    value = value.removeprefix("https://orcid.org/")
    if not re.fullmatch(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", value):
        raise ValueError("invalid ORCID identifier")
    digits = value.replace("-", "")
    total = 0
    for digit in digits[:-1]:
        total = (total + int(digit)) * 2
    check = (12 - total % 11) % 11
    if digits[-1] != ("X" if check == 10 else str(check)):
        raise ValueError("ORCID checksum mismatch")
    return value


def normalize_record(payload, orcid, *, max_items=200):
    if payload.get("orcid-identifier", {}).get("path") != orcid:
        raise ValueError("ORCID response identifies a different researcher")
    person = payload.get("person") or {}
    name = person.get("name") or {}
    public_name = name if name.get("visibility") == "PUBLIC" else {}
    names = {
        field: (public_name.get(field) or {}).get("value")
        for field in ("given-names", "family-name", "credit-name")
    }
    works, affiliations = [], []
    activity = payload.get("activities-summary") or {}
    for group in (activity.get("works") or {}).get("group", []):
        for work in group.get("work-summary", []):
            if work.get("visibility") != "PUBLIC":
                continue
            works.append(
                {
                    k: work.get(k)
                    for k in (
                        "put-code",
                        "title",
                        "type",
                        "publication-date",
                        "external-ids",
                        "source",
                        "last-modified-date",
                        "path",
                    )
                }
            )
            if len(works) > max_items:
                raise ValueError("ORCID work limit exceeded")
    for plural, singular in (
        ("employments", "employment"),
        ("educations", "education"),
        ("qualifications", "qualification"),
    ):
        for group in (activity.get(plural) or {}).get("affiliation-group", []):
            for summary in group.get("summaries", []):
                item = summary.get(singular + "-summary") or {}
                if item.get("visibility") != "PUBLIC":
                    continue
                affiliations.append(
                    {
                        "kind": singular,
                        **{
                            k: item.get(k)
                            for k in (
                                "put-code",
                                "organization",
                                "department-name",
                                "role-title",
                                "start-date",
                                "end-date",
                                "source",
                                "last-modified-date",
                                "path",
                            )
                        },
                    }
                )
                if len(works) + len(affiliations) > max_items:
                    raise ValueError("ORCID activity limit exceeded")
    return {
        "orcid": orcid,
        "names": names,
        "works": works,
        "affiliations": affiliations,
        "name_status": "public" if public_name else "unavailable",
        "modified": payload.get("history", {}).get("last-modified-date"),
        "coverage": "public_record_summary",
        "source_url": "https://orcid.org/" + orcid,
    }


def acquire_author(
    conn,
    orcid,
    *,
    request_id,
    access_token,
    namespace,
    principal_id,
    scopes,
    candidates=(),
    language="en",
    transport=None,
    max_bytes=2_000_000,
    max_items=200,
):
    """Read public metadata with an externally supplied /read-public token.

    Candidates contain existing entity IDs and their explicit ORCID, if known.
    Exact identifiers are distinguished from name-only suggestions. Ambiguous
    suggestions become existing entity-history review decisions and inbox tasks.
    """
    from src.kb.entity_history import EntityHistoryStore
    from src.kb.review_inbox import ReviewInboxStore

    orcid = validate_orcid(orcid)
    if (
        not access_token
        or not request_id
        or not principal_id
        or not namespace
        or not re.fullmatch(r"[a-z]{2}", language)
        or not 1 <= max_bytes <= 20_000_000
        or not 1 <= max_items <= 1000
        or len(candidates) > 20
    ):
        raise ValueError("invalid ORCID acquisition controls")
    required = {
        "knowledge:entity-history:read",
        "knowledge:entity-history:review",
        "knowledge:inbox:write",
        "knowledge:inbox:read",
        f"namespace:{namespace}:write",
    }
    if "operator" not in scopes and not required <= scopes:
        raise PermissionError(
            "ORCID review acquisition requires current namespace and review scopes"
        )
    candidate_ids = [c["entity_id"] for c in candidates]
    if len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError("duplicate ORCID local candidates")
    exact = [
        c["entity_id"]
        for c in candidates
        if c.get("orcid") and validate_orcid(c["orcid"]) == orcid
    ]
    history, inbox = EntityHistoryStore(conn), ReviewInboxStore(conn)
    for identity in candidate_ids:
        history._entity(namespace, identity)
    key = hashlib.sha256(
        json.dumps(
            [
                orcid,
                namespace,
                principal_id,
                candidates,
                language,
                max_bytes,
                max_items,
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS orcid_acquisitions (request_id TEXT PRIMARY KEY, input_hash TEXT, receipt TEXT)"
    )
    previous = conn.execute(
        "SELECT input_hash,receipt FROM orcid_acquisitions WHERE request_id=?",
        [request_id],
    ).fetchone()
    if previous:
        if previous[0] != key:
            raise ValueError("ORCID request ID already used with different inputs")
        return json.loads(previous[1])
    endpoint = f"https://pub.orcid.org/v3.0/{orcid}/record"
    response = (transport or HTTPSPageAdapter._request)(
        url=endpoint,
        params={},
        headers={
            "Accept": "application/json",
            "Authorization": "Bearer " + access_token,
        },
        timeout=15,
        max_bytes=max_bytes,
    )
    status = int(response.get("status", 200))
    if status in {401, 403, 404}:
        return {"status": "unavailable", "orcid": orcid, "http_status": status}
    if status != 200:
        raise ValueError(f"ORCID returned HTTP {status}; no automatic retry")
    raw = response.get("content", b"")
    raw = raw.encode() if isinstance(raw, str) else bytes(raw)
    if len(raw) > max_bytes:
        raise ValueError("ORCID response exceeds byte budget")
    result = normalize_record(json.loads(raw), orcid, max_items=max_items)
    # Only the explicitly public normalized fields are stored, including their
    # source references. Never snapshot a transport's accidental private fields.
    public = json.dumps(result, ensure_ascii=False, sort_keys=True)
    now = int(time.time() * 1000)
    result.update(
        status="available",
        match_status="identifier_confirmed"
        if len(exact) == 1
        else "ambiguous"
        if len(candidate_ids) > 1
        else "unreviewed",
        identifier_confirmed_candidates=exact,
        candidate_entity_ids=candidate_ids,
        adapter_version="orcid-v3/noesis-v1",
        observed_at_ms=now,
    )
    store, snapshots = DocumentStore(conn), SnapshotStore(conn)
    conn.execute("BEGIN TRANSACTION")
    try:
        snapshot = snapshots.snapshot_bytes(
            endpoint,
            public.encode(),
            now,
            content_type="application/json",
            final_url=endpoint,
        )
        title = (
            result["names"].get("credit-name")
            or " ".join(
                v
                for v in (
                    result["names"].get("given-names"),
                    result["names"].get("family-name"),
                )
                if v
            )
            or orcid
        )
        doc = Document(
            document_id="orcid:" + orcid,
            source_type="web",
            language=language,
            ingested_at=now,
            source_id="orcid:" + orcid,
            url=result["source_url"],
            title=title,
            content=public,
            metadata={
                "provider": "orcid",
                "snapshot_digest": snapshot["digest"],
                "representation": "public-normalized-summary",
            },
        )
        outcome = store.upsert([doc])
        if outcome.invalid:
            raise ValueError("ORCID document failed validation")
        result.update(document_id=doc.document_id, snapshot_digest=snapshot["digest"])
        if candidate_ids and len(exact) != 1:
            decision = history.decide(
                namespace,
                "review",
                candidate_ids,
                {
                    "producer": {"name": "orcid-v3", "origin": "machine"},
                    "review_status": "proposed",
                    "observed_at_ms": now,
                    "orcid": orcid,
                    "document_id": doc.document_id,
                },
                reviewer_id="machine:orcid",
                principal_id=principal_id,
                scopes=scopes,
                event_key="orcid:" + orcid,
            )
            revision = conn.execute(
                "SELECT revision_id FROM document_revision_records WHERE document_id=? AND committed_watermark IS NOT NULL ORDER BY revision DESC LIMIT 1",
                [doc.document_id],
            ).fetchone()[0]
            task = inbox.create(
                namespace,
                {
                    "kind": "entity",
                    "namespace": namespace,
                    "id": decision["decision_id"],
                },
                sources=[{"document_id": doc.document_id, "revision_id": revision}],
                domain="research",
                impact=0.5,
                uncertainty=1,
                rationale="ORCID public metadata supplies candidate context; an independent identity review is required.",
                principal_id=principal_id,
                scopes=scopes,
            )
            result["review_task_id"] = task["task_id"]
        conn.execute(
            "INSERT INTO orcid_acquisitions VALUES (?,?,?)",
            [request_id, key, json.dumps(result)],
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return result


def identifier(value):
    value = str(value).removeprefix("https://orcid.org/")
    if not re.fullmatch(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", value):
        raise IntegrationError("invalid_identifier", "An ORCID identifier is required")
    digits = value.replace("-", "")
    total = 0
    for digit in digits[:-1]:
        total = (total + int(digit)) * 2
    remainder = (12 - total % 11) % 11
    if digits[-1] != ("X" if remainder == 10 else str(remainder)):
        raise IntegrationError("invalid_identifier", "ORCID checksum is invalid")
    return value


def normalize_public_record(native):
    orcid = identifier(native["orcid-identifier"]["path"])
    person = native.get("person") or {}
    name = person.get("name") or {}
    public_name = name if name.get("visibility") == "public" else None
    display = None
    if public_name:
        display = (name.get("credit-name") or {}).get("value") or " ".join(
            ((name.get(k) or {}).get("value") or "")
            for k in ("given-names", "family-name")
        ).strip()
    activities = native.get("activities-summary") or {}
    affiliations, works = [], []
    for section, singular in [
        ("employments", "employment"),
        ("educations", "education"),
    ]:
        for group in (activities.get(section) or {}).get("affiliation-group") or []:
            for summary in group.get("summaries") or []:
                value = summary.get(singular + "-summary") or {}
                if value.get("visibility") == "public":
                    affiliations.append({"kind": singular, "assertion": value})
    for group in (activities.get("works") or {}).get("group") or []:
        for work in group.get("work-summary") or []:
            if work.get("visibility") == "public":
                works.append(work)
    if len(affiliations) + len(works) > 5000:
        raise IntegrationError("input_limit", "Too many public record assertions")
    result = {
        "orcid": orcid,
        "name": display or None,
        "name_assertion": public_name,
        "affiliations": affiliations,
        "works": works,
        "last_modified": (native.get("history") or {}).get("last-modified-date"),
        "source_url": "https://pub.orcid.org/v3.0/" + orcid + "/record",
        "api_version": "3.0",
        "visibility": "public-only",
        "missingness": {
            "name": "available" if display else "unavailable-or-private",
            "affiliations": "public-assertions"
            if affiliations
            else "unavailable-or-private",
            "works": "public-assertions" if works else "unavailable-or-private",
        },
    }
    return {**result, "sha256": digest(result)}


class ORCIDClient:
    def __init__(self, *, token, transport=None):
        if not token:
            raise IntegrationError(
                "credential_unavailable", "Configure a /read-public ORCID OAuth token"
            )
        if (
            not isinstance(token, str)
            or len(token) > 8192
            or any(c.isspace() for c in token)
        ):
            raise IntegrationError("invalid_credential", "Invalid ORCID token format")
        self.token = token
        self.transport = transport or HTTPSPageAdapter._request

    def record(self, orcid):
        orcid = identifier(orcid)
        response = self.transport(
            url="https://pub.orcid.org/v3.0/" + orcid + "/record",
            params={},
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer " + self.token,
            },
            timeout=15,
            max_bytes=2_000_000,
        )
        if response.get("status", 200) != 200:
            raise IntegrationError(
                "source_unavailable", "ORCID public record is unavailable"
            )
        content = response["content"]
        if len(content) > 2_000_000:
            raise IntegrationError("input_limit", "ORCID response exceeds byte budget")
        record = normalize_public_record(json.loads(content))
        if record["orcid"] != orcid:
            raise IntegrationError(
                "identity_mismatch", "ORCID record differs from requested identity"
            )
        return record

    def enrich(self, orcid, store):
        record = self.record(orcid)
        # Match the foundation resolver's identifier-based ID construction;
        # never resolve a newly fetched identifier by name similarity.
        node_id = make_node_id(
            EntityType.PERSON,
            "identifiers:" + json.dumps({"orcid": record["orcid"]}, sort_keys=True),
        )
        existing = store.get_node(node_id)
        history = list(existing.properties.get("orcid_history", [])) if existing else []
        if not history or history[-1]["sha256"] != record["sha256"]:
            history.append(record)
        return store.add_node(
            Node(
                EntityType.PERSON,
                record["name"] or record["orcid"],
                node_id=node_id,
                aliases=[record["name"]] if record["name"] else [],
                properties={
                    "orcid": record["orcid"],
                    "orcid_record": record,
                    "orcid_history": history,
                    "resolution_status": "identifier-confirmed",
                    "assertion_semantics": "attributed public registry assertions; not independent verification",
                },
            )
        )
