"""Bulk-release types and the adapter interface (FA01).

A *release* is one published state of a bulk dataset (a snapshot, a baseline
year, a monthly extract). An adapter lists the release's files and turns each
file into records, keeping only what the caller's filter selects:

* ``rows`` adapters emit ``(table_name, row_dict)`` tuples for the dataset store;
* ``documents`` adapters emit :class:`Document` objects for the paper ingest.

Files are processed one at a time by :class:`~src.ingestion.bulk.runner.BulkRunner`
in one of three modes: ``download`` (to a scratch file first, e.g. ZIP archives
that need random access), ``stream`` (read while downloading, e.g. gzip text),
or ``remote`` (the adapter queries the file in place, e.g. DuckDB over S3).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple, Union

from services.ingest.common.document_model import Document

MODES = ("download", "stream", "remote")


@dataclass(frozen=True)
class ReleaseFile:
    name: str
    url: str
    size: Optional[int] = None
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    checksum: Optional[Tuple[str, str]] = None  # (algorithm, hex digest) published by the provider
    mode: str = "download"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def fingerprint(self) -> Tuple[Any, ...]:
        """What identifies an unchanged file between releases."""
        return (self.url, self.etag, self.last_modified, self.size,
                self.checksum[1] if self.checksum else None)


@dataclass
class Release:
    provider: str
    release_id: str
    files: List[ReleaseFile]
    licence: Mapping[str, Any]
    published_at: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TableSpec:
    name: str
    columns: Sequence[Mapping[str, Any]]  # {"name", "type", "unit"?, "description"?}
    primary_key: Sequence[str] = ()
    frequency: Optional[str] = None


@dataclass
class FileSource:
    """What the runner hands an adapter for one file."""
    file: ReleaseFile
    path: Optional[Path] = None                     # download mode
    open_stream: Optional[Callable[[], BinaryIO]] = None  # stream mode
    http: Any = None                                # remote mode (and for side requests)


Record = Union[Tuple[str, Dict[str, Any]], Document]


class BulkAdapter(ABC):
    """One bulk provider. Subclasses set the class attributes and implement two methods."""

    name: str = ""
    publisher: str = ""
    title: str = ""
    description: str = ""
    allowed_hosts: Tuple[str, ...] = ()
    output: str = "rows"                 # "rows" | "documents"
    tables: Dict[str, TableSpec] = {}
    # "file": unchanged files (same fingerprint as in an earlier release) are skipped
    # "release": a new release id means all of its files are processed
    incremental: str = "file"
    # Version of the table schema (not of the data). Bump it only when columns change;
    # each data release is registered separately with its own release id.
    schema_version: str = "1.0.0"

    @abstractmethod
    def list_release(self, http: Any, params: Mapping[str, Any]) -> Release:
        """Discover the current release and its files (no bulk download here)."""

    @abstractmethod
    def process(self, source: FileSource, params: Mapping[str, Any]) -> Iterator[Record]:
        """Yield the records of one file that match ``params``."""

    def validate_params(self, params: Mapping[str, Any]) -> Dict[str, Any]:
        """Normalise and check filter parameters; raise ValueError on bad input."""
        return dict(params)

    def dataset_spec(self, release: Release) -> Dict[str, Any]:
        """Catalog entry for the dataset store (rows adapters)."""
        return {
            "publisher_id": self.publisher,
            "native_id": self.name,
            "semantic_version": self.schema_version,
            "title": self.title or self.name,
            "description": self.description,
            "license": dict(release.licence),
            "tables": [{"name": t.name, "identity": t.name, "columns": list(t.columns),
                        "primary_key": list(t.primary_key), "frequency": t.frequency}
                       for t in self.tables.values()],
            "code_lists": [],
            "partitions": [{"name": "file"}, {"name": "chunk"}],
        }
