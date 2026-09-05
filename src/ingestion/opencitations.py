"""Bounded OpenCitations v2 snapshots and resumable local citation-graph import.

The documented API has no cursor pagination. Resume over an immutable captured
response, never invent upstream limit/offset parameters.
"""

import json
import re
import time
from urllib.parse import quote

from src.ingestion.source_pack_runtime import HTTPSPageAdapter
from src.integrations.common import IntegrationError, digest
from src.knowledge_graph.foundation import (
    EntityType,
    Node,
    Provenance,
    RelationType,
    Triple,
)


def normalize_identifier(value):
    value = str(value).strip().removeprefix("https://doi.org/")
    if value.startswith("10."):
        value = "doi:" + value
    if len(value) > 2048:
        raise IntegrationError("invalid_identifier", "Identifier exceeds length budget")
    prefix, separator, suffix = value.partition(":")
    if (
        not separator
        or prefix not in {"doi", "pmid", "omid"}
        or not suffix
        or any(c.isspace() for c in suffix)
    ):
        raise IntegrationError(
            "invalid_identifier", "An explicit DOI, PMID or OMID is required"
        )
    if prefix == "doi" and (not suffix.startswith("10.") or "/" not in suffix):
        raise IntegrationError("invalid_identifier", "Invalid DOI")
    if prefix == "pmid" and not suffix.isdigit():
        raise IntegrationError("invalid_identifier", "Invalid PMID")
    if prefix == "omid" and not re.fullmatch(r"br/[0-9]+", suffix):
        raise IntegrationError("invalid_identifier", "Invalid OMID")
    return prefix + ":" + (suffix.lower() if prefix == "doi" else suffix)


def _identity(values):
    identifiers = [
        normalize_identifier(v)
        for v in str(values).split()
        if v.startswith(("doi:", "pmid:", "omid:"))
    ]
    if not identifiers:
        raise IntegrationError(
            "missing_identifier", "Citation endpoint lacks a supported identifier"
        )
    return next((v for v in identifiers if v.startswith("doi:")), identifiers[0])


class OpenCitationsClient:
    def __init__(
        self, *, transport=None, token=None, max_bytes=5_000_000, max_edges=10000
    ):
        if not 1 <= max_bytes <= 20_000_000 or not 1 <= max_edges <= 10000:
            raise ValueError("Invalid citation bounds")
        if token is not None and (
            not isinstance(token, str)
            or not token
            or len(token) > 8192
            or any(c.isspace() for c in token)
        ):
            raise IntegrationError(
                "invalid_credential", "Invalid OpenCitations token format"
            )
        self.transport = transport or HTTPSPageAdapter._request
        self.token, self.max_bytes, self.max_edges = token, max_bytes, max_edges

    def snapshot(self, identifier, *, direction="references", observed_at_ms=None):
        identifier = normalize_identifier(identifier)
        if direction not in {"references", "citations"}:
            raise ValueError("Choose references or citations")
        headers = {"Accept": "application/json"}
        if self.token:
            headers["authorization"] = self.token
        url = (
            "https://api.opencitations.net/index/v2/"
            + direction
            + "/"
            + quote(identifier, safe=":")
        )
        response = self.transport(
            url=url, params={}, headers=headers, timeout=20, max_bytes=self.max_bytes
        )
        if response.get("status", 200) != 200:
            raise IntegrationError("source_unavailable", "OpenCitations request failed")
        content = response["content"]
        content = content.encode() if isinstance(content, str) else content
        if len(content) > self.max_bytes:
            raise IntegrationError(
                "input_limit", "Citation snapshot exceeds byte budget"
            )
        records = json.loads(content)
        if not isinstance(records, list) or len(records) > self.max_edges:
            raise IntegrationError(
                "input_limit", "Citation response exceeds edge budget"
            )
        for record in records:
            if not re.fullmatch(r"[0-9]+-[0-9]+", record.get("oci", "")):
                raise IntegrationError("invalid_citation", "Citation lacks an OCI")
            _identity(record.get("citing"))
            _identity(record.get("cited"))
            field = record["citing"] if direction == "references" else record["cited"]
            if identifier not in [
                normalize_identifier(v)
                for v in field.split()
                if v.startswith(("doi:", "pmid:", "omid:"))
            ]:
                raise IntegrationError(
                    "identity_mismatch",
                    "Citation does not involve the requested identifier",
                )
        snapshot = {
            "provider": "opencitations",
            "api_version": "2.2.0",
            "identifier": identifier,
            "direction": direction,
            "url": url,
            "records": records,
            "observed_at_ms": observed_at_ms
            if observed_at_ms is not None
            else int(time.time() * 1000),
            "pagination": "bounded whole upstream response; resume over captured snapshot",
        }
        return {**snapshot, "sha256": digest(snapshot)}

    def ingest_snapshot(self, snapshot, store, *, cursor=None, page_size=100):
        if not 1 <= page_size <= 1000:
            raise ValueError("Invalid import page size")
        expected = digest({k: v for k, v in snapshot.items() if k != "sha256"})
        if expected != snapshot.get("sha256"):
            raise IntegrationError("changed_snapshot", "Citation snapshot was modified")
        start = 0
        if cursor:
            if (
                cursor.get("snapshot_sha256") != expected
                or type(cursor.get("index")) is not int
            ):
                raise IntegrationError(
                    "invalid_cursor", "Cursor belongs to another snapshot"
                )
            start = cursor["index"]
        records = snapshot["records"]
        if not 0 <= start <= len(records):
            raise IntegrationError("invalid_cursor", "Cursor is outside snapshot")
        imported = 0
        for record in records[start : start + page_size]:
            citing, cited = _identity(record["citing"]), _identity(record["cited"])
            for identity in (citing, cited):
                store.add_node(Node(EntityType.DOCUMENT, identity, node_id=identity))
            provenance = Provenance(
                source_doc="https://opencitations.net/oci/" + record["oci"],
                extractor="opencitations-v2",
                chunk_id=expected,
                confidence=1.0,
            )
            triple = Triple(
                citing,
                RelationType.CITES,
                cited,
                provenance=provenance,
                properties={
                    "opencitations": {
                        "oci": record["oci"],
                        "observed_at_ms": snapshot["observed_at_ms"],
                        "snapshot_sha256": expected,
                        "native_record": record,
                    },
                    "corroboration_semantics": "citation relation; mirrored provider records are not independent evidence",
                },
            )
            if not any(
                p.source_doc == provenance.source_doc
                and p.extractor == provenance.extractor
                and p.chunk_id == expected
                for p in store.provenance_for(triple)
            ):
                store.add_triple(triple)
                imported += 1
        next_index = min(len(records), start + page_size)
        return {
            "imported": imported,
            "processed": next_index - start,
            "snapshot_sha256": expected,
            "next_cursor": {"snapshot_sha256": expected, "index": next_index}
            if next_index < len(records)
            else None,
        }


class CitationAcquisitionStore:
    """Persist captured provider responses and atomically resume bounded imports."""

    def __init__(self, conn):
        self.conn = conn
        conn.execute("""CREATE TABLE IF NOT EXISTS citation_provider_snapshots(
            snapshot_sha256 TEXT PRIMARY KEY, snapshot_json TEXT NOT NULL,
            observed_at_ms BIGINT NOT NULL)""")

    def acquire(
        self,
        identifier,
        *,
        direction="references",
        snapshot_sha256=None,
        cursor=None,
        page_size=100,
        client=None,
    ):
        from src.knowledge_graph.foundation import DuckDBKnowledgeGraphStore

        client = client or OpenCitationsClient()
        normalized = normalize_identifier(identifier)
        if not 1 <= page_size <= 1000:
            raise IntegrationError("input_limit", "Import page size must be 1..1000")
        if snapshot_sha256:
            row = self.conn.execute(
                "SELECT snapshot_json FROM citation_provider_snapshots WHERE snapshot_sha256=?",
                [snapshot_sha256],
            ).fetchone()
            if row is None:
                raise IntegrationError(
                    "snapshot_not_found", "Captured citation snapshot not found"
                )
            snapshot = json.loads(row[0])
            if (
                snapshot["identifier"] != normalized
                or snapshot["direction"] != direction
            ):
                raise IntegrationError(
                    "identity_mismatch", "Snapshot belongs to another traversal"
                )
        else:
            if cursor:
                raise IntegrationError(
                    "invalid_cursor", "Resume requires an explicit snapshot hash"
                )
            snapshot = client.snapshot(normalized, direction=direction)
        graph = DuckDBKnowledgeGraphStore(connection=self.conn)
        self.conn.execute("BEGIN")
        try:
            result = client.ingest_snapshot(
                snapshot, graph, cursor=cursor, page_size=page_size
            )
            self.conn.execute(
                "INSERT INTO citation_provider_snapshots VALUES (?,?,?) ON CONFLICT DO NOTHING",
                [
                    snapshot["sha256"],
                    json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                    snapshot["observed_at_ms"],
                ],
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {
            **result,
            "provider": "opencitations",
            "identifier": normalized,
            "direction": direction,
            "observed_at_ms": snapshot["observed_at_ms"],
            "captured_edge_count": len(snapshot["records"]),
        }


def traverse_citations(conn, identifier, *, direction="both", depth=1, limit=100):
    """Read bounded persistent citation edges; provider assertions aren't votes."""
    identifier = normalize_identifier(identifier)
    if (
        direction not in {"references", "citations", "both"}
        or type(depth) is not int
        or not 1 <= depth <= 3
    ):
        raise IntegrationError(
            "invalid_traversal", "Direction and depth (1..3) required"
        )
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise IntegrationError("input_limit", "Edge limit must be 1..1000")
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables"
        ).fetchall()
    }
    if "kg_triples" not in tables:
        return {
            "nodes": [],
            "edges": [],
            "node_count": 0,
            "edge_count": 0,
            "status": "no_citation_graph",
        }
    if (
        "kg_nodes" not in tables
        or conn.execute(
            "SELECT 1 FROM kg_nodes WHERE node_id=?", [identifier]
        ).fetchone()
        is None
    ):
        return {
            "nodes": [],
            "edges": [],
            "node_count": 0,
            "edge_count": 0,
            "status": "identifier_not_found",
        }
    frontier, visited, edges, node_ids = {identifier}, set(), {}, {identifier}
    bounded = False
    for _ in range(depth):
        next_frontier = set()
        for identity in sorted(frontier - visited):
            visited.add(identity)
            clauses, params = [], [RelationType.CITES.value]
            if direction in {"references", "both"}:
                clauses.append("subject=?")
                params.append(identity)
            if direction in {"citations", "both"}:
                clauses.append("object=?")
                params.append(identity)
            rows = conn.execute(
                "SELECT subject, object, properties FROM kg_triples WHERE predicate=? AND ("
                + " OR ".join(clauses)
                + ") ORDER BY subject,object LIMIT ?",
                [*params, limit + 1],
            ).fetchall()
            for subject, target, properties in rows:
                key = (subject, target)
                if key in edges:
                    continue
                if len(edges) >= limit:
                    bounded = True
                    break
                provenance = []
                if "kg_provenance" in tables:
                    assertions = conn.execute(
                        "SELECT source_doc,extractor,chunk_id FROM kg_provenance WHERE subject=? AND predicate=? AND object=? ORDER BY source_doc,extractor,chunk_id LIMIT 21",
                        [subject, RelationType.CITES.value, target],
                    ).fetchall()
                    for source_doc, extractor, chunk_id in assertions[:20]:
                        observed = None
                        if "citation_provider_snapshots" in tables:
                            observation = conn.execute(
                                "SELECT observed_at_ms FROM citation_provider_snapshots WHERE snapshot_sha256=?",
                                [chunk_id],
                            ).fetchone()
                            observed = observation[0] if observation else None
                        provenance.append(
                            {
                                "source_doc": source_doc,
                                "extractor": extractor,
                                "snapshot_sha256": chunk_id,
                                "observed_at_ms": observed,
                            }
                        )
                    bounded = bounded or len(assertions) > 20
                native = json.loads(properties).get("opencitations", {})
                projected = {
                    key: native[key]
                    for key in ("oci", "observed_at_ms", "snapshot_sha256")
                    if key in native
                }
                edges[key] = {
                    "from": subject,
                    "to": target,
                    "opencitations": projected,
                    "provenance": provenance,
                    "independent_evidence_count": None,
                }
                node_ids.update(key)
                if direction in {"references", "both"} and subject == identity:
                    next_frontier.add(target)
                if direction in {"citations", "both"} and target == identity:
                    next_frontier.add(subject)
            if len(edges) >= limit:
                break
        frontier = next_frontier - visited
        if not frontier or len(edges) >= limit:
            bounded = bounded or bool(frontier)
            break
    return {
        "nodes": [{"id": identity} for identity in sorted(node_ids)],
        "edges": [edges[k] for k in sorted(edges)],
        "node_count": len(node_ids),
        "edge_count": len(edges),
        "bounded": bounded,
        "depth": depth,
        "identifier": identifier,
        "direction": direction,
        "corroboration_semantics": "Provider observations of the same citation are not independent factual evidence.",
    }
