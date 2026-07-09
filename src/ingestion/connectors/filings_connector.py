"""
Filings connector — SEC EDGAR filings as document-ingest-v1 records (#906).

The ``edgar`` fetch layer and the ``filings`` mapper existed as libraries but
were never assembled into a registered :class:`Connector`, so no harvester
could reach them. This is that assembly: a connector that discovers filers,
fetches each as a normalized :class:`~src.ingestion.connectors.filings.Filing`
via EDGAR, and parses it into a ``source_type="note"`` ``Document``.

It registers under the name ``"filings"`` (distinct from its ``source_type``,
which is ``"note"`` — shared with the ``upload`` connector), and follows the
adaptive-layer discipline: with no ``NOESIS_EDGAR_USER_AGENT`` configured, or
for an unresolvable filer, ``fetch`` raises :class:`PermanentFetchError` so the
source is skipped rather than retried.

Filers are discovered from the ``query`` argument (an iterable of tickers/CIKs),
the connector's constructor, or the ``NOESIS_EDGAR_FILERS`` env var
(comma-separated). The HTTP getter is injectable via a supplied
:class:`~src.ingestion.connectors.edgar.EdgarClient`, so the connector is fully
offline-testable.
"""

from __future__ import annotations

import dataclasses
import json
import os
from typing import Any, Iterable, List, Optional

from services.ingest.common.document_model import Document
from src.ingestion.connectors.base import (
    Connector,
    PermanentFetchError,
    RawDocument,
    SourceRef,
)
from src.ingestion.connectors.edgar import EdgarClient, harvest_filing
from src.ingestion.connectors.filings import Filing, FilingFact, filing_to_document
from src.ingestion.connectors.registry import register_connector

FILERS_ENV = "NOESIS_EDGAR_FILERS"


def _env_filers() -> List[str]:
    raw = os.getenv(FILERS_ENV, "")
    return [tok.strip() for tok in raw.split(",") if tok.strip()]


@register_connector
class FilingsConnector(Connector):
    """SEC EDGAR filings connector (registry name ``"filings"``)."""

    name = "filings"
    source_type = "note"

    def __init__(
        self,
        client: Optional[EdgarClient] = None,
        filers: Optional[Iterable[str]] = None,
    ):
        self._client = client
        self._filers = list(filers) if filers is not None else None

    def _get_client(self) -> EdgarClient:
        return self._client if self._client is not None else EdgarClient()

    def discover(self, query: Optional[Any] = None) -> Iterable[SourceRef]:
        filers = (
            list(query) if query is not None
            else self._filers if self._filers is not None
            else _env_filers()
        )
        for filer in filers:
            ident = str(filer).strip()
            if ident:
                yield SourceRef(locator=ident, metadata={"source_id": f"edgar:{ident}"})

    def fetch(self, ref: SourceRef) -> RawDocument:
        client = self._get_client()
        if not client.configured:
            raise PermanentFetchError(
                f"{FILERS_ENV.replace('FILERS', 'USER_AGENT')} not configured; skipping EDGAR"
            )
        filing = harvest_filing(ref.locator, client=client)
        if filing is None:
            raise PermanentFetchError(f"no EDGAR filing resolved for {ref.locator!r}")
        # Carry the normalized Filing to parse() as JSON (dataclass round-trip).
        return RawDocument(
            ref=ref,
            content=json.dumps(dataclasses.asdict(filing)),
            content_type="application/json",
        )

    def parse(self, raw: RawDocument) -> List[Document]:
        payload = json.loads(raw.content)
        facts = [FilingFact(**f) for f in payload.pop("facts", [])]
        filing = Filing(facts=facts, **payload)
        return [filing_to_document(filing, ingested_at=raw.fetched_at)]
