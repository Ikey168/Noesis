"""
Tables as evidence (candidate track #781).

Reports and papers carry their numbers in tables. Extracting them into the
Track A observation store lets a claim be checked against a table in the very
document that made it — and against the official series, exposing when a
source's own numbers disagree with the record.

Tables are parsed from document text (Markdown/pipe tables and simple
whitespace/period-value tables) into ``dataset-series-v1`` ``SeriesRecord``s with
``provider = "document"`` and provenance to the parent document, so they join
the A4 resolver as candidate evidence ranked below official series.

Stdlib only. GROBID/PDF-figure extraction is out of scope; this operates on the
already-extracted document text.

See ``docs/architecture/BEYOND_TEXT_ROADMAP.md`` §4.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from services.ingest.common.series_model import Observation, SeriesRecord

_PERIOD_RE = re.compile(r"^(?:(?:19|20)\d{2}(?:[-\s]?Q[1-4]|[-/]\d{1,2})?)$", re.IGNORECASE)
_NUM_RE = re.compile(r"^-?\$?\d[\d,]*\.?\d*%?$")


def _is_period(token: str) -> bool:
    return bool(_PERIOD_RE.match(token.strip()))


def _to_number(token: str) -> Optional[float]:
    t = token.strip().replace(",", "").replace("$", "").replace("%", "")
    try:
        return float(t)
    except ValueError:
        return None


def _norm_period(token: str) -> str:
    t = token.strip().upper().replace(" ", "")
    m = re.match(r"^((?:19|20)\d{2})-?Q([1-4])$", t)
    if m:
        return f"{m.group(1)}-Q{m.group(2)}"
    m = re.match(r"^((?:19|20)\d{2})[-/](\d{1,2})$", t)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    return t


@dataclass
class ParsedTable:
    """A two-column period→value series parsed from a table."""

    label: str
    observations: List[Observation] = field(default_factory=list)
    unit: Optional[str] = None


def _split_pipe_row(line: str) -> List[str]:
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return [c for c in cells]


def _detect_unit(cells: List[str]) -> Optional[str]:
    joined = " ".join(cells).lower()
    if "%" in joined or "percent" in joined:
        return "percent"
    if "$" in joined or "usd" in joined:
        return "usd"
    return None


def extract_tables(text: Optional[str]) -> List[ParsedTable]:
    """Parse period→value tables from document text.

    Handles Markdown/pipe tables and simple two-token (period value) lines.
    Only tables with >=2 period/value rows are returned."""
    if not text:
        return []
    tables: List[ParsedTable] = []
    lines = text.splitlines()

    # --- pipe/markdown tables -------------------------------------------
    i = 0
    while i < len(lines):
        if "|" in lines[i]:
            block = []
            while i < len(lines) and "|" in lines[i]:
                block.append(lines[i])
                i += 1
            table = _parse_pipe_block(block)
            if table:
                tables.append(table)
        else:
            i += 1

    # --- whitespace period-value tables ---------------------------------
    ws = _parse_whitespace_rows(lines)
    if ws:
        tables.append(ws)

    return tables


def _parse_pipe_block(block: List[str]) -> Optional[ParsedTable]:
    rows = [_split_pipe_row(l) for l in block if l.strip()]
    rows = [r for r in rows if any(c for c in r)]
    if len(rows) < 2:
        return None
    # Drop a markdown separator row (---|---).
    rows = [r for r in rows if not all(re.fullmatch(r":?-{2,}:?", c or "-") for c in r)]
    if len(rows) < 2:
        return None
    header = rows[0]
    unit = _detect_unit(header)
    # Prefer a value-column header (skip period-ish headers like Year/Quarter).
    _PERIOD_HEADERS = {"year", "quarter", "date", "period", "month", "time"}
    label = next(
        (c for c in header if c and c.strip().lower() not in _PERIOD_HEADERS),
        header[0] if header and header[0] else "table",
    )
    obs: List[Observation] = []
    for r in rows[1:]:
        if len(r) < 2:
            continue
        # Find the period cell and the first numeric cell after it.
        period_cell = next((c for c in r if _is_period(c)), None)
        value_cell = next((c for c in r if _to_number(c) is not None and not _is_period(c)), None)
        if period_cell is None or value_cell is None:
            continue
        obs.append(Observation(period=_norm_period(period_cell), value=_to_number(value_cell)))
    if len(obs) < 2:
        return None
    return ParsedTable(label=label.strip() or "table", observations=obs, unit=unit or _detect_unit([r for row in rows for r in row]))


def _parse_whitespace_rows(lines: List[str]) -> Optional[ParsedTable]:
    obs: List[Observation] = []
    for line in lines:
        if "|" in line:
            continue
        tokens = line.split()
        if len(tokens) < 2:
            continue
        if _is_period(tokens[0]) and _to_number(tokens[1]) is not None:
            obs.append(Observation(period=_norm_period(tokens[0]), value=_to_number(tokens[1])))
    if len(obs) < 2:
        return None
    return ParsedTable(label="table", observations=obs)


def table_to_series(
    table: ParsedTable,
    document_id: str,
    subject: Optional[str] = None,
    geography: Optional[str] = None,
    as_of: int = 0,
    source_url: Optional[str] = None,
) -> SeriesRecord:
    """Convert a parsed table into a document-provenanced series record."""
    title = subject or table.label or "document table"
    slug = re.sub(r"[^a-z0-9]+", "-", (table.label or "table").lower()).strip("-") or "table"
    series_id = f"document:{document_id}:{slug}"
    # Infer frequency from the first period shape.
    freq = "annual"
    if table.observations:
        p = table.observations[0].period
        if "-Q" in p:
            freq = "quarterly"
        elif re.match(r"^\d{4}-\d{2}$", p):
            freq = "monthly"
    return SeriesRecord(
        series_id=series_id,
        provider="document",
        title=title,
        frequency=freq,
        as_of=as_of,
        observations=list(table.observations),
        unit=table.unit,
        geography=geography,
        license="from source document",
        source_url=source_url,
        metadata={"parent_document_id": document_id, "table_label": table.label},
    )


def document_series(
    document_id: str,
    text: Optional[str],
    subject: Optional[str] = None,
    geography: Optional[str] = None,
    as_of: int = 0,
    source_url: Optional[str] = None,
) -> List[SeriesRecord]:
    """Extract every table in a document as a document-provenanced series."""
    return [
        table_to_series(t, document_id, subject=subject, geography=geography, as_of=as_of, source_url=source_url)
        for t in extract_tables(text)
    ]
