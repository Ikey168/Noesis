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


# ---------------------------------------------------------------------------
# On-demand captures through Save Page Now behind an explicit write scope (#2226, WA11).
# A side-effecting operation: it asks the Internet Archive to make a new capture.
# It needs the dedicated ``knowledge:citation:archive-request`` scope and the
# optional ``save-page-now`` feature (off by default). There is a per-namespace
# daily budget and one status poll per call. Refused URLs are recorded as refused
# and never retried through another archive or proxy. Nothing here runs in tests
# except against an injected fixture transport.

SAVE_PAGE_NOW_CONTRACT = "noesis-save-page-now-request-v1"
_SPN_DDL = (
    "CREATE TABLE IF NOT EXISTS wayback_save_requests (request_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, "
    "input_hash TEXT NOT NULL, requested_at_ms BIGINT NOT NULL, receipt TEXT NOT NULL)"
)


class SavePageNowError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def save_page_now_enabled(conn, bundle="osint"):
    """Whether the optional ``save-page-now`` feature is selected in the active composition plan (default off)."""
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name IN "
            "('composition_authority','composition_active','composition_generations','composition_plans')"
        ).fetchall()}
        if len(tables) < 4:
            return False
        managed = conn.execute("SELECT authority FROM composition_authority WHERE bundle=?", [bundle]).fetchone()
        if not managed or managed[0] != "composition":
            return False
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g "
            "ON g.generation_id=a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1"
        ).fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a write operation
        return False
    return "save-page-now" in ((plan.get("features") or {}).get(bundle) or [])


def _spn_request(*, url, params, headers, timeout, max_bytes, method="GET", data=None):
    """Live SPN2 transport (POST form or GET), no redirects followed. Never used by tests."""
    import urllib.error
    import urllib.parse
    import urllib.request

    body = urllib.parse.urlencode(data).encode() if data is not None else None
    target = url + ("?" + urllib.parse.urlencode(params) if params else "")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, response_headers, newurl):  # noqa: ARG002
            return None

    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(urllib.request.Request(target, data=body, headers=dict(headers), method=method),  # noqa: S310
                         timeout=timeout) as response:
            return {"status": response.status, "headers": dict(response.headers),
                    "content": response.read(max_bytes + 1)}
    except urllib.error.HTTPError as exc:
        try:
            return {"status": exc.code, "headers": dict(exc.headers or {}), "content": exc.read(65536)}
        finally:
            exc.close()


def _spn_credentials(credentials):
    import os

    access = (credentials or {}).get("access") or os.environ.get("NOESIS_IA_S3_ACCESS_KEY")
    secret = (credentials or {}).get("secret") or os.environ.get("NOESIS_IA_S3_SECRET_KEY")
    if not access or not secret:
        raise SavePageNowError("credentials_missing", "Save Page Now needs NOESIS_IA_S3_ACCESS_KEY and "
                                                      "NOESIS_IA_S3_SECRET_KEY")
    return {"Authorization": f"LOW {access}:{secret}"}


def _spn_json(response):
    raw = response.get("content", b"")
    raw = raw.encode() if isinstance(raw, str) else bytes(raw or b"")
    try:
        value = json.loads(raw or b"{}")
    except ValueError:
        value = {}
    return value if isinstance(value, dict) else {}


def _spn_apply_status(conn, receipt, payload, *, namespace, now):
    """Fold one SPN job status into the receipt; a success records the new capture."""
    from src.ingestion.memento import SAVE_PAGE_NOW
    from src.kb.citation_preservation import CAPTURE_SCOPE, CitationPreservationStore

    state = str(payload.get("status") or "")
    ext = payload.get("status_ext")
    if state == "success" and re.fullmatch(r"\d{14}", str(payload.get("timestamp") or "")):
        original = str(payload.get("original_url") or receipt["url"])
        uri_m = f"https://web.archive.org/web/{payload['timestamp']}/{original}"
        capture = CitationPreservationStore(conn, now=now).record_capture(
            namespace,
            {"archive_id": "internet-archive", "archive_kind": "memento-archive", "resolver": "save-page-now",
             "uri_r": receipt["url"], "uri_m": uri_m, "memento_datetime": payload["timestamp"],
             "receipt": {"request_id": receipt["request_id"], "adapter": "save-page-now-v1",
                         "retrieved_at_ms": now(), "evidence_origin": receipt["evidence_origin"]}},
            principal_id=receipt["requester"], scopes={CAPTURE_SCOPE})
        receipt.update(status="success", uri_m=uri_m, capture_id=capture["capture_id"],
                       memento_datetime=capture["memento_datetime"], original_url=original)
    elif state == "error" or (ext and str(ext).startswith("error:")):
        refused = ext in SAVE_PAGE_NOW["refusals"]
        receipt.update(status="refused" if refused else "failed", status_ext=ext,
                       message=str(payload.get("message") or "")[:500])
    elif state == "pending":
        receipt["status"] = "pending"
    receipt["checked_at_ms"] = now()
    return receipt


def request_save_page_now(conn, url, *, namespace, request_id, principal_id, scopes, feature_enabled,
                          citation_id=None, transport=None, credentials=None, evidence_origin=None, now=None):
    """Ask the Internet Archive for a new capture of a cited URL (a write operation).

    Requires the dedicated archive-request scope and the ``save-page-now``
    feature. It records requester, URL, time, job id, status and resulting
    URI-M, and the capture can then be pinned. While Save Page Now is
    ``unverified-live``, the real transport accepts only the WA01 verification
    URL set.
    """
    from src.ingestion.memento import BOUNDED_COVERAGE, LIVE_VERIFICATION, SAVE_PAGE_NOW, validate_url
    from src.kb.citation_preservation import ARCHIVE_REQUEST_SCOPE, CitationPreservationError, _require

    try:
        _require(set(scopes), ARCHIVE_REQUEST_SCOPE)
    except CitationPreservationError as exc:
        raise SavePageNowError("unauthorized", exc.message) from exc
    if not feature_enabled:
        raise SavePageNowError("feature_disabled", "the optional save-page-now feature is off")
    url = validate_url(url)
    if not request_id or not namespace:
        raise SavePageNowError("invalid_request", "namespace and request id are required")
    if transport is None and LIVE_VERIFICATION["save-page-now"]["status"] != "verified-live" \
            and url not in BOUNDED_COVERAGE["verification_urls"]:
        raise SavePageNowError("not_in_verification_set", "until live verification, live Save Page Now requests "
                                                          "run only against the WA01 verification URL set")
    now = now or (lambda: int(time.time() * 1000))
    conn.execute(_SPN_DDL)
    key = hashlib.sha256(json.dumps([namespace, url, citation_id]).encode()).hexdigest()
    prior = conn.execute("SELECT input_hash, receipt FROM wayback_save_requests WHERE request_id=?",
                         [request_id]).fetchone()
    if prior:
        if prior[0] != key:
            raise SavePageNowError("request_id_conflict", "request id already used with different inputs")
        return {**json.loads(prior[1]), "replayed": True}
    requested = now()
    day_start = requested - requested % 86_400_000
    used = conn.execute("SELECT count(*) FROM wayback_save_requests WHERE namespace=? AND requested_at_ms>=?",
                        [namespace, day_start]).fetchone()[0]
    if int(used) >= SAVE_PAGE_NOW["budget_per_namespace_per_day"]:
        raise SavePageNowError("budget_exhausted", "the namespace's daily Save Page Now budget is used")
    auth = _spn_credentials(credentials)
    fetch = transport or _spn_request
    receipt = {"contract": SAVE_PAGE_NOW_CONTRACT, "request_id": request_id, "namespace": namespace,
               "requester": principal_id, "url": url, "citation_id": citation_id, "requested_at_ms": requested,
               "job_id": None, "status": "submitted", "status_ext": None, "uri_m": None, "capture_id": None,
               "retry_elsewhere": False, "scope": ARCHIVE_REQUEST_SCOPE,
               "evidence_origin": evidence_origin or ("live" if transport is None else "injected")}
    try:
        response = fetch(url=SAVE_PAGE_NOW["endpoint"], params={}, method="POST", data={"url": url},
                         headers={"Accept": "application/json", "User-Agent": "Noesis/0.1", **auth},
                         timeout=30, max_bytes=100_000)
        status = int(response.get("status", 0))
        payload = _spn_json(response)
        if status == 429:
            receipt.update(status="rate_limited", status_ext="http:429")
        elif status >= 400 and not payload:
            receipt.update(status="failed", status_ext=f"http:{status}")
        elif payload.get("job_id"):
            receipt["job_id"] = str(payload["job_id"])
            poll = fetch(url=SAVE_PAGE_NOW["status_endpoint"] + receipt["job_id"], params={}, method="GET",
                         headers={"Accept": "application/json", "User-Agent": "Noesis/0.1", **auth},
                         timeout=30, max_bytes=100_000)
            _spn_apply_status(conn, receipt, _spn_json(poll), namespace=namespace, now=now)
        else:
            _spn_apply_status(conn, receipt, payload, namespace=namespace, now=now)
    except SavePageNowError:
        raise
    except Exception as exc:  # noqa: BLE001 - a failed request is recorded, never retried elsewhere
        receipt.update(status="failed", status_ext="transport:" + type(exc).__name__)
    conn.execute("INSERT INTO wayback_save_requests VALUES (?,?,?,?,?)",
                 [request_id, namespace, key, requested, json.dumps(receipt, sort_keys=True)])
    return {**receipt, "replayed": False}


def check_save_page_now(conn, request_id, *, namespace, scopes, transport=None, credentials=None, now=None):
    """One status poll for a pending Save Page Now job; the receipt is updated in place."""
    from src.ingestion.memento import SAVE_PAGE_NOW
    from src.kb.citation_preservation import ARCHIVE_REQUEST_SCOPE, CitationPreservationError, _require

    try:
        _require(set(scopes), ARCHIVE_REQUEST_SCOPE)
    except CitationPreservationError as exc:
        raise SavePageNowError("unauthorized", exc.message) from exc
    conn.execute(_SPN_DDL)
    row = conn.execute("SELECT receipt FROM wayback_save_requests WHERE request_id=? AND namespace=?",
                       [request_id, namespace]).fetchone()
    if not row:
        raise SavePageNowError("request_not_found", "Save Page Now request was not found")
    receipt = json.loads(row[0])
    if receipt["status"] not in {"pending", "submitted"} or not receipt.get("job_id"):
        return {**receipt, "polled": False}
    now = now or (lambda: int(time.time() * 1000))
    fetch = transport or _spn_request
    try:
        poll = fetch(url=SAVE_PAGE_NOW["status_endpoint"] + receipt["job_id"], params={}, method="GET",
                     headers={"Accept": "application/json", "User-Agent": "Noesis/0.1",
                              **_spn_credentials(credentials)}, timeout=30, max_bytes=100_000)
        _spn_apply_status(conn, receipt, _spn_json(poll), namespace=namespace, now=now)
    except SavePageNowError:
        raise
    except Exception as exc:  # noqa: BLE001 - recorded, not retried
        receipt["last_poll_failure"] = type(exc).__name__
    conn.execute("UPDATE wayback_save_requests SET receipt=? WHERE request_id=?",
                 [json.dumps(receipt, sort_keys=True), request_id])
    return {**receipt, "polled": True}


def save_page_now_requests(conn, namespace, *, scopes, limit=100):
    from src.kb.citation_preservation import READ_SCOPE, _require

    _require(set(scopes), READ_SCOPE)
    if not conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='wayback_save_requests'"
                        ).fetchone():
        return []
    return [json.loads(r[0]) for r in conn.execute(
        "SELECT receipt FROM wayback_save_requests WHERE namespace=? ORDER BY requested_at_ms DESC LIMIT ?",
        [namespace, min(max(int(limit), 1), 500)]).fetchall()]
