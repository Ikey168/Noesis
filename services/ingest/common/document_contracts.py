"""
Producer-side validation middleware for the generalized document contract.

Mirrors :mod:`services.ingest.common.contracts` (which validates the news-only
``article-ingest-v1``) but validates against ``document-ingest-v1``. Part of M0
of the knowledge-engine pivot.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from fastavro import parse_schema, validate

from services.ingest.common.contracts import DataContractViolation, metrics

logger = logging.getLogger(__name__)


class DocumentIngestValidator:
    """Validates document payloads against the document-ingest-v1 schema."""

    def __init__(self, schema_path: Optional[str] = None, fail_on_error: bool = True):
        self.fail_on_error = fail_on_error
        self.schema = self._load_schema(schema_path)
        logger.info(f"DocumentIngestValidator initialized with fail_on_error={fail_on_error}")

    def _load_schema(self, schema_path: Optional[str] = None) -> Dict[str, Any]:
        if schema_path is None:
            schema_path = self._get_default_schema_path()
        try:
            with open(schema_path, "r") as f:
                schema_dict = json.load(f)
            parsed_schema = parse_schema(schema_dict)
            logger.info(f"Successfully loaded schema from {schema_path}")
            return parsed_schema
        except FileNotFoundError:
            logger.error(f"Schema file not found: {schema_path}")
            raise
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in schema file {schema_path}: {e}")
            raise

    def _get_default_schema_path(self) -> str:
        current_dir = Path(__file__).parent
        repo_root = current_dir.parent.parent.parent  # services/ingest/common -> repo root
        schema_path = repo_root / "contracts" / "schemas" / "avro" / "document-ingest-v1.avsc"
        if schema_path.exists():
            return str(schema_path)
        try:
            from importlib.resources import files

            packaged = files("contracts.schemas.avro").joinpath(
                "document-ingest-v1.avsc"
            )
            if packaged.is_file():
                return str(packaged)
        except (ImportError, ModuleNotFoundError):
            pass
        raise FileNotFoundError(f"Default schema not found at {schema_path}")

    def validate_document(self, payload: Dict[str, Any]) -> None:
        """Validate a document payload; raise DataContractViolation on failure."""
        doc_id = payload.get("document_id", "unknown")
        try:
            is_valid = validate(payload, self.schema, raise_errors=False)
            if is_valid:
                metrics.increment_success()
                logger.debug(f"Document validation successful for document_id: {doc_id}")
                return

            error_msg = f"Data contract validation failed for document: {doc_id}"
            metrics.increment_failure()
            if self.fail_on_error:
                logger.error(error_msg)
                raise DataContractViolation(error_msg)
            logger.warning(f"{error_msg} - sending to DLQ")
            metrics.increment_dlq()
        except DataContractViolation:
            raise
        except Exception as e:
            error_msg = f"Validation error for document {doc_id}: {str(e)}"
            metrics.increment_failure()
            if self.fail_on_error:
                logger.error(error_msg)
                raise DataContractViolation(error_msg) from e
            logger.warning(f"{error_msg} - sending to DLQ")
            metrics.increment_dlq()


_default_validator: Optional[DocumentIngestValidator] = None


def get_default_validator() -> DocumentIngestValidator:
    """Get the default document validator (singleton)."""
    global _default_validator
    if _default_validator is None:
        _default_validator = DocumentIngestValidator()
    return _default_validator


def validate_document(payload: Dict[str, Any]) -> None:
    """Convenience function for validating a single document payload."""
    get_default_validator().validate_document(payload)
