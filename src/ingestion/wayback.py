"""Read-only Wayback availability and capture acquisition with durable receipts."""

import hashlib
import json
import re
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

from services.ingest.common.document_model import Document
from src.ingestion.canonical import canonicalize_url
from src.ingestion.document_store import DocumentStore
from src.ingestion.extract import extract_article
from src.ingestion.snapshots import SnapshotStore
from src.ingestion.source_pack_runtime import HTTPSPageAdapter


def acquire_wayback(
    conn,
    url,
    *,
    timestamp,
    request_id,
    language,
    transport=None,
    max_bytes=2_000_000,
    timeout_s=15,
):
    """Acquire the closest available capture; timestamps are archive observations.

    At most two GET requests, no automatic retries or new-capture requests.
    Explicit new request IDs allow retrying failed or unavailable observations.
    Redirected captures are returned for review rather than silently relabelled.
    """
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or len(url) > 4096
        or not re.fullmatch(r"\d{4}(?:\d{2}){0,5}", timestamp)
        or not re.fullmatch(r"[a-z]{2}", language)
        or not request_id
        or not 1 <= max_bytes <= 20_000_000
        or not 0 < timeout_s <= 60
    ):
        raise ValueError("invalid Wayback acquisition controls")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS wayback_acquisitions "
        "(request_id TEXT PRIMARY KEY, input_hash TEXT, receipt TEXT)"
    )
    key = hashlib.sha256(
        json.dumps([url, timestamp, language, max_bytes, timeout_s]).encode()
    ).hexdigest()
    prior = conn.execute(
        "SELECT input_hash, receipt FROM wayback_acquisitions WHERE request_id=?",
        [request_id],
    ).fetchone()
    if prior:
        if prior[0] != key:
            raise ValueError("Wayback request ID already used with different inputs")
        return json.loads(prior[1])
    fetch = transport or HTTPSPageAdapter._request
    now = int(time.time() * 1000)
    receipt = {
        "request_id": request_id,
        "original_url": url,
        "requested_timestamp": timestamp,
        "retrieved_at_ms": now,
        "adapter_version": "wayback-availability-v1",
    }

    def get(target, params, limit):
        response = fetch(
            url=target,
            params=params,
            headers={"Accept": "*/*"},
            timeout=timeout_s,
            max_bytes=limit,
        )
        if int(response.get("status", 200)) != 200:
            raise ValueError("archive service unavailable")
        final = response.get("final_url")
        if final and urlsplit(final).netloc != urlsplit(target).netloc:
            raise ValueError("archive redirected outside provider")
        raw = response.get("content", b"")
        raw = raw.encode() if isinstance(raw, str) else bytes(raw)
        if len(raw) > limit:
            raise ValueError("archive response exceeds byte budget")
        return response, raw

    try:
        _, raw = get(
            "https://archive.org/wayback/available",
            {"url": url, "timestamp": timestamp},
            100_000,
        )
        envelope = json.loads(raw)
        if not isinstance(envelope, dict) or not isinstance(
            envelope.get("archived_snapshots"), dict
        ):
            raise ValueError("invalid availability response")  # noqa: TRY004 - provider schema failure
        capture = envelope["archived_snapshots"].get("closest")
        if not capture or capture.get("available") is not True:
            receipt["status"] = "unavailable"
        else:
            archived = urlsplit(capture["url"])
            stamp = capture["timestamp"]
            if (
                archived.hostname != "web.archive.org"
                or archived.scheme not in {"http", "https"}
                or archived.username
                or archived.password
                or archived.port
                or not re.fullmatch(r"\d{14}", stamp)
                or not archived.path.startswith("/web/" + stamp + "/")
            ):
                raise ValueError("invalid archive capture locator")
            captured_at = int(
                datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC).timestamp()
                * 1000
            )
            capture_original = archived.path[len("/web/" + stamp + "/") :]
            if archived.query:
                capture_original += "?" + archived.query
            receipt.update(
                archive_url=urlunsplit(archived._replace(scheme="https")),
                capture_original_url=capture_original,
                captured_at_ms=captured_at,
            )
            if str(capture.get("status")) != "200" or canonicalize_url(
                capture_original
            ) != canonicalize_url(url):
                receipt["status"] = "redirected_capture"
            else:
                # Preserve replayed HTML as archive evidence. The normal replay
                # may contain archive UI; no claim of original raw-byte fidelity.
                response, html = get(receipt["archive_url"], {}, max_bytes)
                final_url = response.get("final_url") or receipt["archive_url"]
                if final_url != receipt["archive_url"]:
                    receipt.update(
                        status="redirected_capture", final_archive_url=final_url
                    )
                else:
                    extracted = extract_article(html, url=url)
                    if not extracted:
                        raise ValueError("archived capture has no extractable article")
                    snapshots, store = SnapshotStore(conn), DocumentStore(conn)
                    document_id = (
                        "wayback:"
                        + hashlib.sha256(
                            (canonicalize_url(url) + stamp).encode()
                        ).hexdigest()[:28]
                    )
                    conn.execute("BEGIN TRANSACTION")
                    try:
                        snapshot = snapshots.snapshot_bytes(
                            url,
                            html,
                            now,
                            content_type="text/html",
                            final_url=receipt["archive_url"],
                        )
                        doc = Document(
                            document_id=document_id,
                            source_type="web",
                            language=language,
                            ingested_at=now,
                            source_id=canonicalize_url(url),
                            url=url,
                            title=extracted.title,
                            content=extracted.text,
                            metadata={
                                "archive_provider": "wayback",
                                "archive_url": receipt["archive_url"],
                                "archive_capture_at_ms": captured_at,
                                "archive_retrieved_at_ms": now,
                                "snapshot_digest": snapshot["digest"],
                                "language_basis": "caller_declared",
                                "archive_representation": "replayed-html",
                                "original_publication_date": None,
                            },
                        )
                        result = store.upsert([doc])
                        if result.invalid:
                            raise ValueError("archived document failed validation")
                        receipt.update(
                            status="acquired",
                            document_id=document_id,
                            snapshot_digest=snapshot["digest"],
                        )
                        conn.execute(
                            "INSERT INTO wayback_acquisitions VALUES (?,?,?)",
                            [request_id, key, json.dumps(receipt)],
                        )
                        conn.execute("COMMIT")
                        return receipt
                    except BaseException:
                        conn.execute("ROLLBACK")
                        raise
    except Exception as exc:  # noqa: BLE001 - persist bounded acquisition failure diagnostics
        receipt.update(status="failed", failure_type=type(exc).__name__)
    conn.execute(
        "INSERT INTO wayback_acquisitions VALUES (?,?,?)",
        [request_id, key, json.dumps(receipt)],
    )
    return receipt
