"""Zenodo metadata and explicitly selected, checksum-verified document artifacts."""

import hashlib
import json
import re
import time
from urllib.parse import urlsplit

from services.ingest.common.document_model import Document
from src.ingestion.connectors.upload.parsers import extract_text
from src.ingestion.connectors.upload.detectors import detect_format
from src.ingestion.source_pack_runtime import HTTPSPageAdapter
from src.integrations.common import IntegrationError


class ZenodoClient:
    def __init__(self, *, transport=None, max_bytes=20_000_000, max_files=100):
        if not 1 <= max_bytes <= 100_000_000 or not 1 <= max_files <= 100:
            raise ValueError("Invalid Zenodo bounds")
        self.transport = transport or HTTPSPageAdapter._request
        self.max_bytes, self.max_files = max_bytes, max_files

    def _get(self, url):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "zenodo.org" or parsed.fragment:
            raise IntegrationError(
                "invalid_endpoint", "Zenodo downloads must stay on the provider origin"
            )
        response = self.transport(
            url=url,
            params={},
            headers={"Accept": "application/json, */*"},
            timeout=20,
            max_bytes=self.max_bytes,
        )
        status = response.get("status", 200)
        if status in {401, 403}:
            raise IntegrationError(
                "restricted", "Zenodo resource is not publicly accessible"
            )
        if status != 200:
            raise IntegrationError(
                "source_unavailable", f"Zenodo returned HTTP {status}"
            )
        body = response["content"]
        body = body.encode() if isinstance(body, str) else body
        if len(body) > self.max_bytes:
            raise IntegrationError("input_limit", "Zenodo response exceeds byte budget")
        return body

    def record(self, record_id):
        record_id = str(record_id)
        if not re.fullmatch(r"[1-9][0-9]{0,19}", record_id):
            raise ValueError("Numeric Zenodo record ID required")
        content = self._get("https://zenodo.org/api/records/" + record_id)
        native = json.loads(content)
        if str(native.get("id")) != record_id:
            raise IntegrationError(
                "identity_mismatch", "Zenodo record identity changed"
            )
        files = native.get("files") or []
        if not isinstance(files, list) or len(files) > self.max_files:
            raise IntegrationError("input_limit", "Zenodo manifest exceeds file budget")
        keys = [f.get("key") for f in files]
        if any(not isinstance(k, str) or not k for k in keys) or len(set(keys)) != len(
            keys
        ):
            raise IntegrationError(
                "invalid_manifest", "Zenodo manifest requires distinct file keys"
            )
        metadata = native.get("metadata") or {}
        return {
            "record_id": record_id,
            "concept_id": str(native.get("conceptrecid") or ""),
            "doi": native.get("doi") or metadata.get("doi"),
            "concept_doi": native.get("conceptdoi"),
            "version": metadata.get("version"),
            "links": native.get("links") or {},
            "license": metadata.get("license"),
            "access_right": metadata.get("access_right"),
            "related_identifiers": metadata.get("related_identifiers") or [],
            "files": files,
            "native_record": native,
            "api_version": "records REST",
            "metadata_sha256": hashlib.sha256(content).hexdigest(),
        }

    def acquire(self, record_id, selected_files, store, *, languages):
        if not selected_files or len(set(selected_files)) != len(selected_files):
            raise ValueError("Select distinct file keys explicitly")
        if set(languages) != set(selected_files) or any(
            not isinstance(code, str) or not re.fullmatch(r"[a-z]{2}", code)
            for code in languages.values()
        ):
            raise ValueError(
                "Declare an ISO 639-1 language for every selected artifact"
            )
        manifest = self.record(record_id)
        if manifest["access_right"] not in {"open", None}:
            raise IntegrationError(
                "restricted", "Record files are restricted or embargoed"
            )
        available = {f["key"]: f for f in manifest["files"]}
        if set(selected_files) - set(available):
            raise IntegrationError(
                "missing_file", "Selected file is absent from this record version"
            )
        documents, total = [], 0
        for key in selected_files:
            item = available[key]
            size = item.get("size")
            if type(size) is not int or size < 0 or total + size > self.max_bytes:
                raise IntegrationError(
                    "input_limit", "Selected files exceed aggregate byte budget"
                )
            checksum = item.get("checksum") or ""
            if not re.fullmatch(
                r"(md5:[a-fA-F0-9]{32}|sha256:[a-fA-F0-9]{64})", checksum
            ):
                raise IntegrationError(
                    "unsupported_checksum",
                    "A documented MD5 or SHA256 checksum is required",
                )
            url = (item.get("links") or {}).get("self")
            if not isinstance(url, str):
                raise IntegrationError(
                    "restricted", "File has no accessible download link"
                )
            content = self._get(url)
            total += len(content)
            algorithm, expected = checksum.split(":")
            if (
                len(content) != size
                or hashlib.new(algorithm, content).hexdigest() != expected.lower()
            ):
                raise IntegrationError(
                    "checksum_mismatch",
                    "Downloaded file differs from the record manifest",
                )
            if total > self.max_bytes:
                raise IntegrationError(
                    "input_limit", "Downloaded files exceed aggregate budget"
                )
            text, parser_metadata = extract_text(content, detect_format(content, key))
            if not text.strip():
                raise IntegrationError(
                    "unsupported_artifact",
                    "Selected artifact has no supported document text",
                )
            documents.append(
                Document(
                    document_id="zenodo:"
                    + str(record_id)
                    + ":"
                    + hashlib.sha256(key.encode()).hexdigest(),
                    source_type="note",
                    language=languages[key],
                    title=key,
                    content=text,
                    url=url,
                    source_id="zenodo",
                    ingested_at=int(time.time() * 1000),
                    metadata={
                        "zenodo_json": json.dumps(
                            manifest, ensure_ascii=False, sort_keys=True
                        ),
                        "artifact_key": key,
                        "checksum": checksum,
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "parser_json": json.dumps(
                            parser_metadata, ensure_ascii=False, sort_keys=True
                        ),
                    },
                )
            )
        # Validate every selected download before handing a batch to existing storage.
        return store.upsert(documents)
