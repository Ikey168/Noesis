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


# ---------------------------------------------------------------------------
# Internet Archive Memento captures with CDX digests (#2226, WA04).
# Additive: ``acquire_wayback`` above is unchanged for its existing callers.

_WAYBACK_STAMP = re.compile(r"/web/(\d{14})(?:[a-z]{2}_)?/")
_SHA1_BASE32 = re.compile(r"[A-Z2-7]{32}")


def acquire_wayback_mementos(client, url, *, request_id, max_results=5000):
    """Map Internet Archive TimeMap and CDX responses to capture records with digests.

    ``client`` is a :class:`src.ingestion.memento.MementoClient`, which supplies the
    shared transport, per-archive budget, receipts and the citation-preservation
    store. The Memento TimeMap lists the captures and the CDX adds the published
    payload digest, HTTP status, mimetype and any redirect location. That is two
    requests in the WA01 budget of three. Excluded or robots-blocked URLs are
    recorded as ``excluded_by_archive`` and never retried through another path.
    Content is never downloaded here.
    """
    from src.ingestion.memento import (
        BOUNDED_COVERAGE,
        Budget,
        MementoError,
        access_condition,
        classify,
        parse_timemap,
        validate_url,
    )

    url = validate_url(url)
    spec = client.archives["internet-archive"]
    budget = Budget("internet-archive", client.transport)
    receipt = client._capture_receipt(request_id, adapter="wayback-memento-v1")
    limit = min(int(max_results), BOUNDED_COVERAGE["max_mementos_per_timemap"])

    def stop(outcome, detail):
        detail = {**detail, "retry_elsewhere": False}
        snap = client.snapshot(url, "internet-archive", resolver="internet-archive", outcome=outcome,
                               detail=detail, receipt=receipt)
        return {"outcome": outcome, "timemap_id": snap["timemap_id"], "detail": detail,
                "requests": budget.requests}

    try:
        status, headers, raw = budget.get(spec["timemap_link"] + url, hosts=spec["hosts"],
                                          headers={"Accept": "application/link-format"})
    except MementoError as exc:
        return stop("archive_unavailable", {"failure": exc.code})
    outcome = classify(status, raw) if status else "archive_unavailable"
    if outcome not in {"ok", "no_capture_on_record"}:
        return stop(outcome, {"http_status": status or None, "step": "timemap"})
    try:
        listed = parse_timemap(raw, headers.get("content-type", ""))["mementos"] if outcome == "ok" else []
    except (ValueError, KeyError, TypeError) as exc:
        return stop("archive_unavailable", {"failure": "unparseable_timemap:" + type(exc).__name__})
    cdx_status = None
    try:
        cdx_status, _, cdx_raw = budget.get(
            spec["cdx"], hosts=spec["hosts"],
            params={"url": url, "output": "json", "limit": limit,
                    "fl": "timestamp,original,mimetype,statuscode,digest,redirect"})
        cdx_outcome = classify(cdx_status, cdx_raw) if cdx_status else "archive_unavailable"
    except MementoError as exc:
        cdx_outcome, cdx_raw = "archive_unavailable:" + exc.code, b""
    if cdx_outcome == "excluded_by_archive":
        # The archive excludes the URL (robots or administrative exclusion): honoured as published.
        return stop("excluded_by_archive", {"http_status": cdx_status, "step": "cdx"})
    if cdx_outcome == "blocked_by_archive":
        return stop("blocked_by_archive", {"http_status": cdx_status, "step": "cdx"})
    rows = {}
    cdx_state = "unavailable"
    if cdx_outcome == "ok":
        try:
            table = json.loads(cdx_raw or b"[]")
            header = [str(h) for h in table[0]] if table else []
            for values in table[1:]:
                row = dict(zip(header, values, strict=False))
                if re.fullmatch(r"\d{14}", str(row.get("timestamp", ""))):
                    rows.setdefault(row["timestamp"], row)
            cdx_state = "read"
        except (ValueError, IndexError, TypeError):
            cdx_state = "unparseable"
    elif cdx_outcome == "no_capture_on_record":
        cdx_state = "empty"
    mementos = {}
    stamps_ms = {}
    for memento in listed:
        stamp = _WAYBACK_STAMP.search(urlsplit(memento["uri_m"]).path + "/")
        key = stamp.group(1) if stamp else memento["uri_m"]
        mementos.setdefault(key, memento["uri_m"])
        stamps_ms.setdefault(key, memento["datetime_ms"])
    for stamp, row in rows.items():
        # CDX rows the TimeMap did not list (collapsed or paged) are still captures on record.
        mementos.setdefault(stamp, f"https://web.archive.org/web/{stamp}/{row.get('original') or url}")
    ids = []
    for key, uri_m in sorted(mementos.items())[:limit]:
        row = rows.get(key, {})
        digest = str(row.get("digest") or "")
        status_code = str(row.get("statuscode") or "")
        redirect = row.get("redirect")
        capture = {
            "archive_id": "internet-archive",
            "archive_kind": "memento-archive",
            "resolver": "internet-archive",
            "uri_r": url,
            "uri_m": uri_m,
            "memento_at_ms": stamps_ms[key] if key in stamps_ms else None,
            "status": int(status_code) if status_code.isdigit() else None,
            "mimetype": row.get("mimetype") or None,
            "digests": [{"algorithm": "sha1-base32", "value": digest, "basis": "published"}]
            if _SHA1_BASE32.fullmatch(digest) else [],
            "access_condition": access_condition(spec, uri_m),
            "receipt": receipt,
        }
        if capture["memento_at_ms"] is None:
            del capture["memento_at_ms"]
            capture["memento_datetime"] = key
        if status_code.startswith("3"):
            capture["archive_redirect"] = {"status": int(status_code),
                                           "location": redirect if redirect not in (None, "", "-") else None}
        ids.append(client.record(capture)["capture_id"])
    truncated = len(mementos) > limit
    result_outcome = "captures" if ids else "no_capture_on_record"
    snap = client.snapshot(url, "internet-archive", resolver="internet-archive", outcome=result_outcome,
                           capture_ids=ids, truncated=truncated,
                           detail={"cdx": cdx_state, "listed_by_timemap": len(listed), "cdx_rows": len(rows)},
                           receipt=receipt)
    return {"outcome": result_outcome, "capture_ids": sorted(set(ids)), "timemap_id": snap["timemap_id"],
            "truncated": truncated, "cdx": cdx_state, "requests": budget.requests}
