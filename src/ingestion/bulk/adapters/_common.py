"""Small helpers shared by bulk adapters."""
from __future__ import annotations

import bz2
import csv
import gzip
import io
import zipfile
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import IO, Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence

from src.ingestion.bulk.base import ReleaseFile


def head_file(http, name: str, url: str, mode: str = "download", **metadata) -> ReleaseFile:
    """ReleaseFile with size/ETag/Last-Modified from a HEAD request."""
    head = http.head(url)
    size = head.get("content-length")
    return ReleaseFile(name=name, url=url, mode=mode, size=int(size) if size else None,
                       etag=head.get("etag"), last_modified=head.get("last-modified"), metadata=metadata)


def newest(files: Sequence[ReleaseFile]) -> Optional[datetime]:
    stamps = [parsedate_to_datetime(f.last_modified) for f in files if f.last_modified]
    return max(stamps) if stamps else None


def stamp(moment: Optional[datetime], fallback: str = "") -> str:
    return moment.strftime("%Y%m%dT%H%M%SZ") if moment else fallback


def iso(moment: Optional[datetime]) -> Optional[str]:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ") if moment else None


def csv_rows(handle: IO[bytes], encoding: str = "utf-8", delimiter: str = ",") -> Iterator[Dict[str, str]]:
    """DictReader over a binary handle, with header names stripped."""
    text = io.TextIOWrapper(handle, encoding=encoding, errors="replace", newline="")
    reader = csv.reader(text, delimiter=delimiter)
    header = [h.strip() for h in next(reader)]
    for values in reader:
        yield dict(zip(header, values))


def zip_members(path: Path, suffixes: Sequence[str]) -> Iterator[IO[bytes]]:
    with zipfile.ZipFile(path) as archive:
        for member in archive.namelist():
            if member.lower().endswith(tuple(suffixes)):
                with archive.open(member) as handle:
                    yield handle


def decompressed(handle: IO[bytes], name: str) -> IO[bytes]:
    if name.endswith(".bz2"):
        return bz2.BZ2File(handle)
    if name.endswith(".gz"):
        return gzip.GzipFile(fileobj=handle)
    return handle


def text_set(values: Any, upper: bool = False) -> List[str]:
    items = values if isinstance(values, (list, tuple, set)) else ([values] if values else [])
    out = {str(v).strip() for v in items if str(v).strip()}
    return sorted(v.upper() for v in out) if upper else sorted(out)


def number(value: Any) -> Optional[float]:
    try:
        return float(value) if value not in (None, "", "NA", "-") else None
    except (TypeError, ValueError):
        return None
