"""Bounded IATI and World Bank acquisition into the development-finance record owner (#1932, D03 and D05).

Each run fetches one explicit selection through a
:class:`~src.ingestion.provider_execution.DurableHTTP` budget (exact hosts, no
redirects, durable replayable captures), keeps every raw page through the
existing :class:`~src.ingestion.document_store.DocumentStore` (one document per
activity or project, with the capture digest and the element's position as its
locator) and applies the parsed records to
:class:`~src.kb.development_finance.DevelopmentFinanceStore` together with the
selection's publisher coverage. A run that fails before any page is read is
recorded as stale coverage only; the last revisions stay current and nothing is
marked ended.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any

from services.ingest.common.document_model import Document
from src.ingestion.development_finance_sources import (
    IATIDatastoreClient,
    WorldBankProjectsClient,
    iati_selection,
    world_bank_selection,
)
from src.ingestion.document_store import DocumentStore
from src.ingestion.provider_execution import ProviderError, canonical, digest
from src.kb.development_finance import WRITE_SCOPE, DevelopmentFinanceStore, authorize


def _evidence(captures) -> dict[str, Any]:
    if any(
        hashlib.sha256(c.content).hexdigest() != c.receipt.get("digest")
        for c in captures
    ):
        raise ProviderError("source_changed", "capture digest mismatch")
    executions = sorted({c.receipt.get("execution") or "unknown" for c in captures})
    execution = executions[0] if len(executions) == 1 else "mixed"
    return {
        "capture_digests": [c.receipt["digest"] for c in captures],
        "observed_at_ms": max(int(c.receipt["observed_at_ms"]) for c in captures),
        # Only the network transport is live evidence; an injected transport is fixture evidence.
        "evidence_origin": "live" if execution == "network" else "fixture",
        "execution": execution,
        "source_url": captures[0].receipt["request"]["url"],
    }


def _documents(
    conn, namespace, provider, items, reuse_notice, observed_at_ms
) -> list[dict[str, Any]]:
    documents = []
    for key, title, url, body, locator, capture in items:
        documents.append(
            Document(
                document_id="devfin:"
                + digest([namespace, provider, key, digest(body)])[:32],
                source_type="web",
                source_id=f"{provider}:{key}",
                language="und",
                ingested_at=observed_at_ms,
                url=url,
                title=title or key,
                content=canonical(body),
                metadata={
                    "namespace": namespace,
                    "development_finance_provider": provider,
                    "record_key": key,
                    "locator": canonical(locator),
                    "native_capture_sha256": capture,
                    "source_license": reuse_notice,
                },
            )
        )
    if not documents:
        return []
    outcome = DocumentStore(conn).upsert(documents)
    if outcome.invalid:
        raise ProviderError(
            "document_validation",
            "development-finance evidence failed document validation",
        )
    return [
        {"document_id": c["document_id"], "revision_id": c["revision_id"]}
        for c in outcome.changes
    ]


def acquire_iati(
    client: IATIDatastoreClient,
    selection: Mapping[str, Any],
    *,
    namespace: str,
    scopes: Iterable[str],
    observation: str,
    reuse_notice: str,
    rows: int = 100,
    max_pages: int = 5,
    store: DevelopmentFinanceStore | None = None,
) -> dict[str, Any]:
    """One bounded IATI selection: every page, publisher coverage and version history."""
    scopes = set(scopes)
    authorize(namespace, scopes, WRITE_SCOPE, write=True)
    if not str(reuse_notice or "").strip():
        raise ValueError("explicit reuse notice required")
    store = store or DevelopmentFinanceStore(client.http.conn, now=client.http.now)
    clean = iati_selection(selection)
    collected = client.collect(clean, observation, rows=rows, max_pages=max_pages)
    if not collected["pages"]:
        failure = store.record_failure(
            namespace,
            "iati-datastore",
            clean,
            failure_code=collected["coverage"]["stop_reason"] or "no_page",
            observed_at_ms=client.http.now(),
            scopes=scopes,
        )
        return {"ok": False, "provider": "iati-datastore", "failure": failure}
    evidence = _evidence(collected["captures"])
    documents = _documents(
        client.http.conn,
        namespace,
        "iati-datastore",
        [
            (
                activity["iati_identifier"],
                activity.get("title"),
                evidence["source_url"],
                activity,
                {**activity["locator"], "capture_sha256": page["sha256"]},
                page["sha256"],
            )
            for page in collected["pages"]
            for activity in page["activities"]
        ],
        reuse_notice,
        evidence["observed_at_ms"],
    )
    applied = store.apply_iati(namespace, collected, dataset=evidence, scopes=scopes)
    return {
        "ok": True,
        "provider": "iati-datastore",
        **applied,
        "documents": documents,
        "evidence": {
            k: evidence[k] for k in ("capture_digests", "evidence_origin", "execution")
        },
    }


def acquire_world_bank(
    client: WorldBankProjectsClient,
    selection: Mapping[str, Any],
    *,
    namespace: str,
    scopes: Iterable[str],
    observation: str,
    reuse_notice: str,
    rows: int = 50,
    max_pages: int = 5,
    store: DevelopmentFinanceStore | None = None,
) -> dict[str, Any]:
    scopes = set(scopes)
    authorize(namespace, scopes, WRITE_SCOPE, write=True)
    if not str(reuse_notice or "").strip():
        raise ValueError("explicit reuse notice required")
    store = store or DevelopmentFinanceStore(client.http.conn, now=client.http.now)
    clean = world_bank_selection(selection)
    collected = client.collect(clean, observation, rows=rows, max_pages=max_pages)
    if not collected["pages"]:
        failure = store.record_failure(
            namespace,
            "world-bank-projects",
            clean,
            failure_code=collected["coverage"]["stop_reason"] or "no_page",
            observed_at_ms=client.http.now(),
            scopes=scopes,
        )
        return {"ok": False, "provider": "world-bank-projects", "failure": failure}
    evidence = _evidence(collected["captures"])
    documents = _documents(
        client.http.conn,
        namespace,
        "world-bank-projects",
        [
            (
                project["project_id"],
                project.get("name"),
                evidence["source_url"],
                project,
                {"page": number, "project_id": project["project_id"]},
                capture.receipt["digest"],
            )
            for number, (page, capture) in enumerate(
                zip(collected["pages"], collected["captures"])
            )
            for project in page["projects"]
        ],
        reuse_notice,
        evidence["observed_at_ms"],
    )
    applied = store.apply_world_bank(
        namespace, collected, dataset=evidence, scopes=scopes
    )
    return {
        "ok": True,
        "provider": "world-bank-projects",
        **applied,
        "documents": documents,
        "evidence": {
            k: evidence[k] for k in ("capture_digests", "evidence_origin", "execution")
        },
    }
