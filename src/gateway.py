"""Daily-driver runtime shared by the Noesis CLI and default MCP gateway."""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from urllib.parse import urlparse


class GatewayError(RuntimeError):
    """Stable error raised by the human/agent gateway surface."""

    def __init__(self, code: str, message: str, *, repair: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.repair = repair


def resolve_domain(config, requested: str | None = None) -> str:
    """Resolve an ergonomic default domain without hiding ambiguity."""

    from src.kb.registry import DomainConfigError, load_registry

    registry = load_registry(config.domains)
    if requested:
        try:
            registry.get(requested)
        except DomainConfigError as exc:
            raise GatewayError("unknown_domain", str(exc)) from exc
        return requested

    names = [definition.name for definition in registry.domains()]
    if "local" in names:
        return "local"
    if len(names) == 1:
        return names[0]
    raise GatewayError(
        "domain_required",
        "more than one knowledge domain is configured; specify domain",
    )


def _url_document(source: str, language: str):
    parsed = urlparse(source)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise GatewayError("bad_source", "URL ingestion supports only HTTP(S) URLs")

    from urllib.request import Request, urlopen

    try:
        with urlopen(
            Request(source, headers={"User-Agent": "Noesis/0.1"}),
            timeout=20,
        ) as response:
            content = response.read(16 * 1024 * 1024 + 1)
            content_type = response.headers.get_content_type()
    except Exception as exc:
        raise GatewayError(
            "fetch_failed",
            f"could not fetch URL: {type(exc).__name__}: {exc}",
        ) from exc
    if len(content) > 16 * 1024 * 1024:
        raise GatewayError(
            "source_too_large",
            "URL response exceeds the 16 MiB gateway limit",
        )

    from services.ingest.common.document_model import Document
    from src.ingestion.connectors.upload.detectors import detect_format
    from src.ingestion.connectors.upload.parsers import extract_text

    fmt = (
        "html"
        if content_type in {"text/html", "application/xhtml+xml"}
        else detect_format(content, parsed.path)
    )
    text, metadata = extract_text(content, fmt)
    if not text.strip():
        repair = metadata.get("error") if isinstance(metadata, dict) else None
        raise GatewayError(
            "parse_failed",
            "URL produced no extractable text",
            repair=repair,
        )
    title = str(
        metadata.get("title") or parsed.path.rsplit("/", 1)[-1] or parsed.netloc
    )
    return Document(
        document_id="web:" + hashlib.sha256(source.encode()).hexdigest()[:24],
        source_type="web",
        language=language,
        ingested_at=int(time.time() * 1000),
        source_id=parsed.netloc.casefold(),
        url=source,
        title=title,
        content=text,
        metadata={**metadata, "source_url": source},
    )


def _load_documents(source: str, language: str):
    if urlparse(source).scheme in {"http", "https"}:
        return [_url_document(source, language)]

    path = Path(source).expanduser().resolve()
    if not path.is_file():
        raise GatewayError("source_not_found", f"file not found: {path}")

    from src.ingestion.connectors.upload.connector import UploadConnector

    connector = UploadConnector(default_language=language)
    documents = []
    for ref in connector.discover(path):
        documents.extend(connector.parse(connector.fetch(ref)))
    if not documents:
        raise GatewayError(
            "parse_failed",
            f"{path} produced no extractable text",
            repair='install "noesis-evidence[media]" for PDF/media formats',
        )
    return documents



def ingest_documents(
    config,
    documents,
    *,
    domain: str | None = None,
    source_identity: str = "gateway",
    conn=None,
):
    """Run already-acquired Documents through the canonical production workflow."""
    from src.database.local_warehouse_seed import ensure_schema
    from src.kb.membership import run_membership_pass
    from src.kb.registry import load_registry
    from src.kb.workflows import (
        WorkflowStore,
        production_handlers,
        production_ingest_manifest,
    )
    from src.noesis_cli.config import open_warehouse

    selected_domain = resolve_domain(config, domain)
    registry = load_registry(config.domains)
    definition = registry.get(selected_domain)
    private = "private" in {tag.casefold() for tag in definition.tags}
    docs = list(documents)
    if not docs:
        raise GatewayError("empty_ingest", "at least one document is required")
    for document in docs:
        tags = list(document.metadata.get("tags") or [])
        document.metadata["tags"] = sorted(
            set([*tags, selected_domain] + (["private"] if private else []))
        )
        document.source_id = document.source_id or (
            "local-upload" if document.source_type == "note" else None
        )

    owned = conn is None
    connection = open_warehouse(config) if owned else conn
    try:
        ensure_schema(connection)
        handlers = production_handlers(
            connection,
            principal_id=config.principal,
            tolerate_extractor_unavailable=True,
        )
        base_ingest = handlers["ingest"]

        def ingest_with_membership(context, state):
            result = dict(base_ingest(context, state))
            result["membership"] = run_membership_pass(connection, registry)
            return result

        handlers["ingest"] = ingest_with_membership
        manifest = production_ingest_manifest(selected_domain, selected_domain)
        initial = {"documents": [document.to_dict() for document in docs]}
        run_key = "gateway-ingest:" + hashlib.sha256(
            f"{selected_domain}|{source_identity}".encode()
        ).hexdigest()[:24]

        try:
            from transformers.utils import logging as transformers_logging
            transformers_logging.disable_progress_bar()
        except ImportError:
            pass

        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            workflow = WorkflowStore(connection).execute(
                manifest, handlers, initial, run_key=run_key
            )
        state = workflow["state"]
        summary = dict(state.get("ingest") or {})
        membership = dict(state.get("membership") or {})
        extraction = dict(state.get("extraction") or {})
        claim_index = dict(state.get("argument_claims") or {})
        watermark = dict(workflow.get("watermark") or {})
    finally:
        if owned:
            connection.close()

    return {
        "domain": selected_domain,
        "source": source_identity,
        "documents": [document.document_id for document in docs],
        "upsert": summary,
        "membership": membership.get("domains", {}).get(selected_domain, {}),
        "processing": {
            "workflow_run_id": workflow["run_id"],
            "watermark": watermark.get("watermark"),
            "extraction": extraction.get("counts", {}),
            "claims_indexed": claim_index.get("claims_indexed", 0),
            "coverage": state.get("coverage", {}),
            "warnings": state.get("warnings", []),
        },
        "retry_safe": True,
    }


def add_source(config, source: str, *, domain: str | None = None, language: str = "en"):
    """Ingest one file or URL through the production ingest-to-index workflow."""
    documents = _load_documents(source, language)
    return ingest_documents(
        config,
        documents,
        domain=domain,
        source_identity=source,
    )
