"""Bounded WARC response import and deterministic capture export."""

import hashlib
import io
from datetime import datetime
from pathlib import Path
from .common import IntegrationError, version


def read_warc(path, *, max_records=1000, max_bytes=20_000_000):
    from warcio.archiveiterator import ArchiveIterator

    if not 1 <= max_records <= 10000 or not 1 <= max_bytes <= 100_000_000:
        raise ValueError("invalid WARC bounds")
    if Path(path).stat().st_size > max_bytes:
        raise IntegrationError("input_limit", "Archive exceeds input byte limit")
    records = []
    consumed = 0
    visited = 0
    with Path(path).open("rb") as stream:
        for record in ArchiveIterator(stream):
            visited += 1
            if visited > max_records:
                raise IntegrationError(
                    "record_limit", "Archive exceeds record limit; nothing published"
                )
            payload = record.content_stream().read(max_bytes - consumed + 1)
            consumed += len(payload)
            if consumed > max_bytes:
                raise IntegrationError(
                    "expanded_limit", "Expanded archive exceeds byte limit"
                )
            if record.rec_type != "response":
                continue
            url = record.rec_headers.get_header("WARC-Target-URI")
            if not url or not url.startswith(("http://", "https://")):
                raise IntegrationError(
                    "invalid_capture", "Response lacks HTTP target URI"
                )
            records.append(
                {
                    "url": url,
                    "captured_at": record.rec_headers.get_header("WARC-Date"),
                    "record_id": record.rec_headers.get_header("WARC-Record-ID"),
                    "http_status": record.http_headers.statusline
                    if record.http_headers
                    else None,
                    "http_protocol": record.http_headers.protocol
                    if record.http_headers
                    else None,
                    "http_headers": list(record.http_headers.headers)
                    if record.http_headers
                    else [],
                    "content_type": record.http_headers.get_header("Content-Type")
                    if record.http_headers
                    else None,
                    "payload": payload,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "warcio_version": version("warcio"),
                }
            )
    return records


def write_warc(captures, path, *, max_bytes=20_000_000):
    from warcio.warcwriter import WARCWriter
    from warcio.statusandheaders import StatusAndHeaders

    if len(captures) > 10000:
        raise IntegrationError("record_limit", "Too many captures")
    output = io.BytesIO()
    writer = WARCWriter(output, gzip=False)
    total = 0
    for capture in captures:
        payload = capture["payload"]
        total += len(payload)
        if total > max_bytes:
            raise IntegrationError("input_limit", "Capture payloads exceed byte limit")
        headers = {"WARC-Date": capture["captured_at"]}
        if capture.get("record_id"):
            headers["WARC-Record-ID"] = capture["record_id"]
        # content_stream() decodes transfer/content encoding. Preserve response status
        # and semantic headers, but remove encodings and lengths of original bytes.
        http_headers = [
            (k, v)
            for k, v in capture.get("http_headers", [])
            if k.lower()
            not in {"content-length", "transfer-encoding", "content-encoding"}
        ]
        if not any(k.lower() == "content-type" for k, _ in http_headers):
            http_headers.append(
                (
                    "Content-Type",
                    capture.get("content_type") or "application/octet-stream",
                )
            )
        http = StatusAndHeaders(
            capture.get("http_status") or "200 OK",
            http_headers,
            protocol=capture.get("http_protocol") or "HTTP/1.1",
        )
        record = writer.create_warc_record(
            capture["url"],
            "response",
            payload=io.BytesIO(payload),
            http_headers=http,
            warc_headers_dict=headers,
        )
        writer.write_record(record)
        if output.tell() > max_bytes:
            raise IntegrationError("output_limit", "Archive exceeds byte limit")
    data = output.getvalue()
    with Path(path).open("xb") as stream:
        stream.write(data)
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "records": len(captures),
        "bytes": len(data),
        "version": version("warcio"),
    }


def ingest_warc(path, store, *, max_records=1000, max_bytes=20_000_000):
    from src.ingestion.extract import extract_article
    from services.ingest.common.document_model import Document

    records = read_warc(path, max_records=max_records, max_bytes=max_bytes)
    documents = []
    for capture in records:
        content_type = (capture["content_type"] or "").split(";")[0].lower()
        if content_type not in {"text/html", "text/plain"}:
            raise IntegrationError(
                "unsupported_capture",
                "Only HTML/plain text responses can be ingested as documents",
            )
        text = capture["payload"].decode("utf-8", errors="strict")
        if content_type == "text/html":
            extracted = extract_article(text, url=capture["url"])
            if extracted is None:
                raise IntegrationError(
                    "extraction_failed", "Archive response has no usable article text"
                )
            text = extracted.text
        captured = int(
            datetime.fromisoformat(
                capture["captured_at"].replace("Z", "+00:00")
            ).timestamp()
            * 1000
        )
        identity = "warc:" + hashlib.sha256(capture["url"].encode()).hexdigest()
        documents.append(
            Document(
                document_id=identity,
                source_type="news",
                title=capture["url"],
                content=text,
                url=capture["url"],
                source_id="warc-import",
                ingested_at=captured,
                language="unknown",
                metadata={
                    "archive": {k: v for k, v in capture.items() if k != "payload"},
                    "content_representation": "archived-extracted-text",
                },
            )
        )
    return store.upsert(documents)
