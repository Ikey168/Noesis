"""Acquire search candidates through the shared extractor, snapshots and sink."""

import hashlib
import json
import re
import time
from itertools import islice

from services.ingest.common.document_model import Document
from src.ingestion.canonical import canonicalize_url
from src.ingestion.connectors.rest import _safe_base_url
from src.ingestion.document_store import DocumentStore
from src.ingestion.extract import extract_article
from src.ingestion.snapshots import SnapshotStore
from src.ingestion.source_pack_runtime import HTTPSPageAdapter, _validate_redirect


def acquire_candidates(
    conn,
    candidates,
    *,
    acquisition_id,
    allowed_hosts,
    language,
    max_candidates=20,
    max_bytes=2_000_000,
    timeout_s=15,
    transport=None,
    dns_resolver=None,
):
    """Single-writer bounded acquisition. Successful receipts replay without HTTP.

    Failed candidates remain explicit and can be retried with a new acquisition
    ID. A crash before receipt commit may repeat a fetch; source revisions and
    binary payloads still deduplicate through their existing stores.
    """
    if (
        not isinstance(acquisition_id, str)
        or not acquisition_id.strip()
        or not 1 <= max_candidates <= 200
        or not 1 <= max_bytes <= 20_000_000
        or not 0 < timeout_s <= 60
        or not re.fullmatch(r"[a-z]{2}", language)
    ):
        raise ValueError("invalid candidate acquisition bounds")
    refs = list(islice(candidates, max_candidates + 1))
    if len(refs) > max_candidates:
        raise ValueError("candidate limit exceeded")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS discovery_acquisitions "
        "(acquisition_id TEXT PRIMARY KEY, input_hash TEXT, receipt TEXT)"
    )
    identity = json.dumps(
        {
            "refs": [{"url": r.locator, "metadata": r.metadata} for r in refs],
            "hosts": sorted(allowed_hosts),
            "language": language,
            "max_bytes": max_bytes,
            "timeout_s": timeout_s,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    digest = hashlib.sha256(identity.encode()).hexdigest()
    old = conn.execute(
        "SELECT input_hash, receipt FROM discovery_acquisitions WHERE acquisition_id=?",
        [acquisition_id],
    ).fetchone()
    if old:
        if old[0] != digest:
            raise ValueError("acquisition ID already used with different inputs")
        return json.loads(old[1])
    store, snapshots = DocumentStore(conn), SnapshotStore(conn)
    fetch = transport or HTTPSPageAdapter._request
    results, seen = [], set()
    for ref in refs:
        canonical = canonicalize_url(ref.locator)
        if canonical in seen:
            results.append({"url": ref.locator, "status": "duplicate_candidate"})
            continue
        seen.add(canonical)
        try:
            _safe_base_url(ref.locator, set(allowed_hosts), dns_resolver)
            response = fetch(
                url=ref.locator,
                params={},
                headers={"Accept": "text/html"},
                timeout=timeout_s,
                max_bytes=max_bytes,
            )
            if int(response.get("status", 200)) != 200:
                raise ValueError("source returned non-success status")
            final_url = response.get("final_url") or ref.locator
            _validate_redirect(ref.locator, final_url, dns_resolver)
            raw = response.get("content", b"")
            raw = raw.encode() if isinstance(raw, str) else bytes(raw)
            if len(raw) > max_bytes:
                raise ValueError("source response exceeds byte budget")
            extracted = extract_article(raw, url=final_url)
            if not extracted:
                raise ValueError("source has no extractable article")
            now = int(time.time() * 1000)
            snapshot = snapshots.snapshot_bytes(
                ref.locator, raw, now, content_type="text/html", final_url=final_url
            )
            document_id = "web-" + hashlib.sha256(canonical.encode()).hexdigest()[:28]
            document = Document(
                document_id=document_id,
                source_type="web",
                language=language,
                ingested_at=now,
                source_id=canonical,
                url=ref.locator,
                title=extracted.title,
                content=extracted.text,
                metadata={
                    "discovery_json": json.dumps(ref.metadata, ensure_ascii=False),
                    "language_basis": "caller_declared",
                    "acquisition_url": final_url,
                    "snapshot_digest": snapshot["digest"],
                    "extraction_json": json.dumps(
                        extracted.metadata, ensure_ascii=False
                    ),
                    "score_semantics": extracted.score_semantics,
                },
            )
            outcome = store.upsert([document])
            if outcome.invalid:
                raise ValueError("acquired document failed validation")
            results.append(
                {
                    "url": ref.locator,
                    "status": "acquired",
                    "document_id": document_id,
                    "snapshot_digest": snapshot["digest"],
                }
            )
        except Exception as exc:  # noqa: BLE001 - isolate each acquisition failure in its receipt
            results.append(
                {
                    "url": ref.locator,
                    "status": "failed",
                    "failure_type": type(exc).__name__,
                }
            )
    receipt = {
        "acquisition_id": acquisition_id,
        "input_hash": digest,
        "results": results,
    }
    conn.execute(
        "INSERT INTO discovery_acquisitions VALUES (?,?,?)",
        [acquisition_id, digest, json.dumps(receipt, ensure_ascii=False)],
    )
    return receipt
