"""Destatis GENESIS-Online tables as ``dataset-series-v1`` records (Track A dataset connector).

GENESIS-Online (REST API 2020) serves each statistical table in two calls: the
``metadata/table`` answer, whose ``Updated`` stamp (Stand) is the table's
publication vintage, and ``data/tablefile?format=ffcsv``, a flat-file CSV with
one row per combination of Merkmal values and one column per value code
(``<CODE>__<label>__<unit>``). This connector turns one declared table into
one :class:`SeriesRecord` per value code, geography code and remaining
Merkmal combination:

* ``as_of`` is the table's ``Updated`` stamp - never the retrieval time, so
  re-harvesting an unchanged table yields the same vintage;
* each observation keeps its period as published (``Zeit``); a GENESIS sign
  (``-``, ``.``, ``...``, ``x``, ``/``) is an observation without a value and the
  published sign travels in ``metadata["signs"]`` with its meaning;
* the value column's unit label and the declared measure travel in the header;
  a value code, unit or geography attribute the declaration does not name is
  refused as schema drift rather than read partially.

The CSV reading, ``Updated`` stamp and sign conventions are those the
demographics connector already implements
(:mod:`src.ingestion.demographic_sources`); this module shares them rather
than re-implementing GENESIS parsing. Authentication and HTTP transport are the
caller's: ``fetch`` receives a ``get(url) -> bytes`` callable, so the
source-pack runtime's policy-checked transport (same-host, byte ceiling,
timeout) is the only network path.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from typing import Any, List, Optional
from urllib.parse import urlencode

from services.ingest.common.series_model import Observation, SeriesRecord
from src.ingestion.connectors.dataset.base import DatasetConnector, RawSeries, SeriesRef

PROVIDER = "destatis"
LICENSE = "dl-de-by-2.0"
TABLE_PATTERN = re.compile(r"^\d{5}-\d{4}$")
_VALUE_COLUMN = re.compile(r"^([A-Z0-9]+)__(.+?)__(.+)$")
# Unit labels GENESIS prints in value columns, with the unit this connector records (verify per table).
UNIT_LABELS = {
    "Anzahl": "count",
    "1000 m2": "1000 m²",
    "1000 m²": "1000 m²",
    "1000 m3": "1000 m³",
    "1000 m³": "1000 m³",
    "1000 EUR": "1000 EUR",
    "m2": "m²",
}


class GenesisFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def table_urls(endpoint: str, table: str) -> dict[str, str]:
    """The metadata and flat-file URLs of one table on the declared GENESIS REST endpoint."""
    base = endpoint.rstrip("/")
    return {
        "metadata": f"{base}/metadata/table?"
        + urlencode({"name": table, "language": "de"}),
        "data": f"{base}/data/tablefile?"
        + urlencode(
            {
                "name": table,
                "area": "all",
                "compress": "false",
                "format": "ffcsv",
                "language": "de",
            }
        ),
    }


def series_id(
    table: str, value_code: str, geography: str, dimensions: Mapping[str, str]
) -> str:
    """Stable identity: provider, table, value code, geography code and every remaining Merkmal value."""
    dims = ",".join(f"{k}={v}" for k, v in sorted(dimensions.items()))
    return f"{PROVIDER}:{table}:{value_code}:{geography}" + (f":{dims}" if dims else "")


def _stamp_ms(stamp: str) -> int:
    return int(
        datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc).timestamp() * 1000
    )


class GenesisConnector(DatasetConnector):
    """Declared GENESIS tables (``31111-*`` building permits, ``31231-*`` completions...) as vintaged series."""

    provider = PROVIDER

    def __init__(
        self,
        tables: Iterable[Mapping[str, Any]],
        *,
        endpoint: str,
        get: Callable[[str], bytes] | None = None,
    ) -> None:
        self.tables = [dict(t) for t in tables]
        for table in self.tables:
            if not TABLE_PATTERN.fullmatch(str(table.get("table") or "")):
                raise GenesisFormatError(
                    "invalid_declaration", "a GENESIS table code is NNNNN-NNNN"
                )
            if not table.get("geography_attribute") or not dict(
                table.get("measures") or {}
            ):
                raise GenesisFormatError(
                    "invalid_declaration",
                    "a table declares its geography attribute and value-code measures",
                )
        self.endpoint = endpoint
        self.get = get

    def discover(self, query: Optional[Any] = None) -> Iterable[SeriesRef]:
        wanted = None if query is None else {str(q) for q in query}
        for table in self.tables:
            if wanted is None or table["table"] in wanted:
                yield SeriesRef(
                    locator=table["table"], title=table.get("label"), metadata=table
                )

    def fetch(self, ref: SeriesRef) -> RawSeries:
        if self.get is None:
            raise GenesisFormatError(
                "no_transport", "GENESIS fetches go through the caller's transport"
            )
        urls = table_urls(self.endpoint, ref.locator)
        metadata = self.get(urls["metadata"])
        data = self.get(urls["data"])
        return RawSeries(
            ref=ref,
            content=json.dumps(
                {
                    "metadata": metadata.decode("utf-8"),
                    "data": data.decode("utf-8-sig"),
                },
                ensure_ascii=False,
            ),
            content_type="application/json",
            source_url=urls["data"],
        )

    def parse(self, raw: RawSeries) -> List[SeriesRecord]:
        body = json.loads(raw.content)
        return self.parse_table(
            body["data"].encode("utf-8"),
            body["metadata"].encode("utf-8"),
            dict(raw.ref.metadata),
            source_url=raw.source_url,
        )

    @staticmethod
    def parse_table(
        data_raw: bytes,
        meta_raw: bytes,
        table: Mapping[str, Any],
        *,
        source_url: str | None = None,
    ) -> List[SeriesRecord]:
        """One flat-file table into series; the whole table is refused on any undeclared shape."""
        from src.ingestion.demographic_sources import (
            GENESIS_SIGNS,
            DemographicFormatError,
            _csv,
            _period,
            genesis_updated,
            parse_number,
        )

        code = str(table["table"])
        try:
            published_on, published_at = genesis_updated(meta_raw, table=code)
            rows = _csv(data_raw)
        except DemographicFormatError as exc:
            raise GenesisFormatError(exc.code, str(exc)) from exc
        if not rows:
            raise GenesisFormatError("schema_drift", "the table has no rows")
        columns = list(rows[0])
        for required in ("Statistik_Code", "Zeit"):
            if required not in columns:
                raise GenesisFormatError(
                    "schema_drift", f"column {required} is missing"
                )
        measures = {str(k): str(v) for k, v in dict(table["measures"]).items()}
        value_columns = {c: m for c in columns if (m := _VALUE_COLUMN.match(c))}
        stated = {m[1] for m in value_columns.values()}
        if not value_columns or stated != set(measures):
            raise GenesisFormatError(
                "schema_drift",
                f"value columns {sorted(stated)} are not the declared {sorted(measures)}",
            )
        units = {}
        for column, match in value_columns.items():
            unit = UNIT_LABELS.get(match[3])
            if unit is None:
                raise GenesisFormatError(
                    "schema_drift", f"unit {match[3]!r} of {column} is not declared"
                )
            units[match[1]] = {"published": match[3], "unit": unit, "label": match[2]}
        merkmale = sorted(
            int(m[1]) for c in columns if (m := re.fullmatch(r"(\d+)_Merkmal_Code", c))
        )
        geo_attribute = str(table["geography_attribute"])
        grouped: dict[tuple, dict[str, Any]] = {}
        for row in rows:
            if row["Statistik_Code"] != code.split("-")[0]:
                raise GenesisFormatError(
                    "schema_drift", "a row belongs to another statistic"
                )
            dims: dict[str, str] = {}
            geo = geo_label = None
            for n in merkmale:
                if row[f"{n}_Merkmal_Code"] == geo_attribute:
                    geo, geo_label = (
                        row[f"{n}_Auspraegung_Code"],
                        row.get(f"{n}_Auspraegung_Label"),
                    )
                else:
                    dims[row[f"{n}_Merkmal_Code"]] = row[f"{n}_Auspraegung_Code"]
            if not geo:
                raise GenesisFormatError(
                    "schema_drift", f"a row has no {geo_attribute} geography code"
                )
            try:
                period = _period(row["Zeit"])
            except DemographicFormatError as exc:
                raise GenesisFormatError(exc.code, str(exc)) from exc
            for column, match in value_columns.items():
                key = (match[1], geo, tuple(sorted(dims.items())))
                entry = grouped.setdefault(
                    key,
                    {"label": geo_label, "observations": {}, "texts": {}, "signs": {}},
                )
                if period in entry["observations"]:
                    raise GenesisFormatError(
                        "schema_drift", "a series states the same period twice"
                    )
                text = row[column].strip()
                if text in GENESIS_SIGNS:
                    value = GENESIS_SIGNS[text][0]
                    entry["signs"][period] = {
                        "sign": text,
                        "meaning": GENESIS_SIGNS[text][1],
                    }
                else:
                    try:
                        value = parse_number(text, "de") if text else None
                    except DemographicFormatError as exc:
                        raise GenesisFormatError(exc.code, str(exc)) from exc
                    if value is None:
                        entry["signs"][period] = {
                            "sign": "",
                            "meaning": "no value published",
                        }
                entry["observations"][period] = None if value is None else float(value)
                entry["texts"][period] = text
        as_of = _stamp_ms(published_at)
        records = []
        for (value_code, geo, dims), entry in sorted(grouped.items()):
            dimensions = dict(dims)
            records.append(
                SeriesRecord(
                    series_id=series_id(code, value_code, geo, dimensions),
                    provider=PROVIDER,
                    title=f"{table.get('label') or code}: {units[value_code]['label']} ({entry['label'] or geo})",
                    frequency=str(table.get("frequency") or "annual"),
                    as_of=as_of,
                    observations=[
                        Observation(period=p, value=v)
                        for p, v in sorted(entry["observations"].items())
                    ],
                    unit=units[value_code]["unit"],
                    geography=geo,
                    license=LICENSE,
                    source_url=source_url,
                    metadata={
                        "table": code,
                        "value_code": value_code,
                        "measure": measures[value_code],
                        "geography_attribute": geo_attribute,
                        "geography_label": entry["label"],
                        "dimensions": dimensions,
                        "published_unit": units[value_code]["published"],
                        "published_on": published_on,
                        "published_at": published_at,
                        "vintage_basis": "genesis_table_updated",
                        "value_texts": dict(sorted(entry["texts"].items())),
                        "signs": dict(sorted(entry["signs"].items())),
                    },
                )
            )
        return records
