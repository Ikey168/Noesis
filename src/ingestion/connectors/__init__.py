"""
Ingestion connector framework.

Connectors normalize arbitrary sources into document-ingest-v1 ``Document``
records behind a common ``discover -> fetch -> parse`` interface. Importing this
package registers the built-in connectors so they are resolvable by source type
via :func:`get_connector`.
"""

from src.ingestion.connectors.base import Connector, RawDocument, SourceRef
from src.ingestion.connectors.registry import (
    available_source_types,
    get_connector,
    is_registered,
    register_connector,
)

# Importing the modules triggers @register_connector for the built-in connectors.
from src.ingestion.connectors import news  # noqa: E402,F401
from src.ingestion.connectors import paper  # noqa: E402,F401
from src.ingestion.connectors import upload  # noqa: E402,F401
from src.ingestion.connectors import media  # noqa: E402,F401
from src.ingestion.connectors import book  # noqa: E402,F401
from src.ingestion.connectors import blog  # noqa: E402,F401
from src.ingestion.connectors import filings_connector  # noqa: E402,F401
from src.ingestion.connectors import legislative  # noqa: E402,F401
from src.ingestion.connectors import manifest  # noqa: E402,F401

__all__ = [
    "Connector",
    "RawDocument",
    "SourceRef",
    "available_source_types",
    "get_connector",
    "is_registered",
    "register_connector",
]
