"""Optional SDMX-native statistical connector with bounded raw capture.

Two wire formats are read. SDMX-ML (generic data and structure messages) goes
through the optional ``sdmx1`` package. SDMX-CSV 1.0 (``format=SDMX-CSV`` on
the Eurostat dissemination API) is read with the standard library only
(:meth:`SDMXConnector.parse_csv`), so a deployment without ``sdmx1`` can still
acquire Eurostat data through this connector. Both keep every observation's
original value text and attributes (for Eurostat, the ``OBS_FLAG`` status
letters) and never infer a provider vintage from the retrieval time: the
SDMX-CSV ``LAST UPDATE`` column is carried as the provider's own update stamp
when the response has it.
"""

import csv
import hashlib
import io
import math
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from services.ingest.common.series_model import Observation, SeriesRecord
from src.integrations.common import IntegrationError, digest, version

from .base import DatasetConnector, RawSeries, SeriesRef


def _time_millis(value):
    if value is None:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def last_update_iso(value):
    """Eurostat's SDMX-CSV ``LAST UPDATE`` (``dd/mm/yy HH:MM:SS``, or ISO) as an ISO timestamp; else ``None``.

    The two-digit year is read as 20yy (verify the column format against a live response).
    """
    text = str(value or "").strip()
    if not text:
        return None
    short = re.fullmatch(r"(\d{2})/(\d{2})/(\d{2}) (\d{2}):(\d{2}):(\d{2})", text)
    if short:
        day, month, year, hour, minute, second = (int(v) for v in short.groups())
        try:
            return datetime(2000 + year, month, day, hour, minute, second).isoformat()
        except ValueError:
            return None
    for pattern in ("%d/%m/%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, pattern).isoformat()
        except ValueError:
            continue
    return None


def _seasonal_adjustment(dimensions, common_attributes):
    values = {
        str(key).upper(): str(value)
        for key, value in {**dimensions, **common_attributes}.items()
    }
    for key in ("S_ADJ", "SEAS_ADJ", "SEASONAL_ADJUSTMENT", "ADJUSTMENT"):
        if key not in values:
            continue
        token = values[key].casefold()
        code = values[key].upper()
        if "not seasonally adjusted" in token or code in {"NSA", "NNSA"}:
            return "not_adjusted"
        if "seasonally adjusted" in token or code in {"SA", "SCA", "SA_WDA", "SCA_WDA"}:
            return "adjusted"
        if key == "ADJUSTMENT" and code in _ECB_ADJUSTMENT:
            return _ECB_ADJUSTMENT[code]
    return "unknown"


# ECB CL_ADJUSTMENT codes: N = neither seasonally nor working-day adjusted,
# S = seasonally adjusted, Y = seasonally and working-day adjusted. Working-day
# or calendar-only adjustment (W, C) is not seasonal and stays unknown.
_ECB_ADJUSTMENT = {"N": "not_adjusted", "S": "adjusted", "Y": "adjusted"}


# SDMX-CSV 1.0 data URLs (verify against the provider's current API documentation before a live run).
_CSV_ENDPOINTS = {
    "ESTAT": ("https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/{flow}/{key}", {"format": "SDMX-CSV"}),
    # OECD SDMX REST API (.Stat Suite): ``format=csvfile`` returns SDMX-CSV with a DATAFLOW column (verify).
    "OECD": ("https://sdmx.oecd.org/public/rest/data/{flow}/{key}", {"format": "csvfile"}),
    # ILOSTAT SDMX REST API (Fusion Registry): ``format=csv`` returns SDMX-CSV 1.0 with a DATAFLOW column (verify).
    "ILO": ("https://sdmx.ilo.org/rest/data/{flow}/{key}", {"format": "csv"}),
}
# Dataflow references: Eurostat uses bare ids; the OECD uses ``AGENCY,DSD@DATAFLOW,VERSION``.
_CSV_FLOWS = {
    "ESTAT": re.compile(r"[A-Za-z0-9_.-]+"),
    "OECD": re.compile(r"[A-Za-z0-9_.]+,[A-Za-z0-9_.@]+,[0-9]+(\.[0-9]+)*"),
    "ILO": re.compile(r"ILO,DF_[A-Za-z0-9_]+,[0-9]+(\.[0-9]+)*"),
}
_PROVIDER_HOSTS = {
    "ECB": "data-api.ecb.europa.eu",
    "ESTAT": "ec.europa.eu",
    "BBK": "api.statistiken.bundesbank.de",
    "OECD": "sdmx.oecd.org",
    "ILO": "sdmx.ilo.org",
}
_CSV_FIXED_COLUMNS = {"DATAFLOW", "LAST UPDATE", "TIME_PERIOD", "OBS_VALUE"}
_CSV_MISSING = {"", ":"}
_FREQUENCIES = {"A": "annual", "Q": "quarterly", "M": "monthly", "W": "weekly", "D": "daily"}


class SDMXConnector(DatasetConnector):
    def __init__(
        self,
        provider="ECB",
        *,
        transport=None,
        max_bytes=8_000_000,
        max_observations=10000,
    ):
        if provider not in _PROVIDER_HOSTS:
            raise ValueError("Supported SDMX providers: ECB, ESTAT, BBK, OECD, ILO")
        if not 1 <= max_bytes <= 20_000_000 or not 1 <= max_observations <= 100000:
            raise ValueError("invalid SDMX bounds")
        self.provider = provider.lower()
        self.source = provider
        self.transport = transport
        self.max_bytes = max_bytes
        self.max_observations = max_observations

    def discover(self, query=None):
        if query is None:
            return
        if (
            not isinstance(query, dict)
            or set(query)
            - {"flow", "key", "startPeriod", "endPeriod", "lastNObservations"}
            or not query.get("flow")
        ):
            raise ValueError(
                "SDMX requires flow and optional key/startPeriod/endPeriod"
            )
        if "lastNObservations" in query and (
            type(query["lastNObservations"]) is not int
            or not 1 <= query["lastNObservations"] <= self.max_observations
        ):
            raise ValueError("lastNObservations exceeds the observation budget")
        yield SeriesRef(
            locator=str(query["flow"]) + "/" + str(query.get("key", "")),
            metadata=dict(query),
        )

    def fetch(self, ref):
        import sdmx

        client = sdmx.Client(self.source)
        request = client.get(
            "data",
            ref.metadata["flow"],
            key=ref.metadata.get("key", ""),
            params={
                k: ref.metadata[k]
                for k in ("startPeriod", "endPeriod", "lastNObservations")
                if k in ref.metadata
            },
            dry_run=True,
        )
        return self._fetch_request(
            ref, request, "application/vnd.sdmx.genericdata+xml;version=2.1"
        )

    def fetch_structure(self, resource, resource_id):
        """Fetch one bounded native dataflow, data structure or code list."""
        import sdmx

        if resource not in {"dataflow", "datastructure", "codelist"}:
            raise ValueError("Unsupported SDMX structure resource")
        request = sdmx.Client(self.source).get(resource, resource_id, dry_run=True)
        ref = SeriesRef(
            resource + "/" + resource_id,
            metadata={"resource": resource, "resource_id": resource_id},
        )
        return self._fetch_request(
            ref, request, "application/vnd.sdmx.structure+xml;version=2.1"
        )

    def parse_structure(self, raw):
        """Map structure identities, dimensions and multilingual code labels.

        The caller retains RawSeries for unmodeled SDMX annotations; the mapping
        references its exact bytes and does not claim to be a full SDMX serializer.
        """
        import sdmx

        content = raw.content.encode() if isinstance(raw.content, str) else raw.content
        if (
            len(content) > self.max_bytes
            or b"<!DOCTYPE" in content.upper()
            or b"<!ENTITY" in content.upper()
        ):
            raise IntegrationError(
                "invalid_structure",
                "Structure exceeds byte limit or declares XML entities",
            )
        message = sdmx.read_sdmx(io.BytesIO(content))
        result = {
            "source_url": raw.source_url,
            "raw_sha256": __import__("hashlib").sha256(content).hexdigest(),
            "retrieved_at_ms": raw.fetched_at,
            "provider": self.provider,
            "sdmx_version": version("sdmx1"),
            "dataflows": {},
            "structures": {},
            "codelists": {},
            "mapping_coverage": "identities, dimensions, code labels; other structural annotations remain in RawSeries",
        }
        for key, flow in message.dataflow.items():
            result["dataflows"][key] = {
                "id": flow.id,
                "version": flow.version,
                "names": dict(flow.name.localizations),
                "structure_id": flow.structure.id if flow.structure else None,
            }
        for key, structure in message.structure.items():
            result["structures"][key] = {
                "id": structure.id,
                "version": structure.version,
                "names": dict(structure.name.localizations),
                "dimensions": {
                    dimension.id: getattr(
                        getattr(dimension.local_representation, "enumerated", None),
                        "id",
                        None,
                    )
                    for dimension in structure.dimensions.components
                },
            }
        count = 0
        for key, codelist in message.codelist.items():
            count += len(codelist.items)
            if count > 100000:
                raise IntegrationError(
                    "structure_limit", "SDMX structure exceeds code budget"
                )
            result["codelists"][key] = {
                "id": codelist.id,
                "version": codelist.version,
                "names": dict(codelist.name.localizations),
                "codes": {
                    code.id: dict(code.name.localizations)
                    for code in codelist.items.values()
                },
            }
        return result

    def ingest(self, query, store, *, structure=None):
        """Archive native bytes and publish observations in one transaction."""
        from src.ingestion.snapshots import SnapshotStore

        snapshots = SnapshotStore(store._conn)
        results = []
        for ref in self.discover(query):
            raw = self.fetch(ref)
            records = self.parse(raw, structure=structure)
            store._conn.execute("BEGIN")
            try:
                captured = snapshots.snapshot_bytes(
                    raw.source_url,
                    raw.content,
                    raw.fetched_at,
                    content_type=raw.content_type,
                    final_url=raw.source_url,
                )
                for record in records:
                    record.metadata["native_snapshot"] = captured
                written = store.upsert_many(records)
                store._conn.execute("COMMIT")
            except BaseException:
                store._conn.execute("ROLLBACK")
                raise
            results.append(
                {
                    "snapshot": captured,
                    "series_ids": [record.series_id for record in records],
                    "observations_written": written,
                }
            )
        return results

    # ------------------------------------------------------------ SDMX-CSV

    def csv_url(self, flow, key="", params=None):
        """The SDMX-CSV data URL of one flow and series key (no network access)."""
        if self.source not in _CSV_ENDPOINTS:
            raise ValueError(f"SDMX-CSV is not declared for {self.source}")
        if not flow or not _CSV_FLOWS[self.source].fullmatch(str(flow)):
            raise ValueError(f"not a {self.source} SDMX dataflow reference")
        if any(c in str(key) for c in "/?#& "):
            raise ValueError("SDMX series keys use dimension codes separated by '.' and '+'")
        allowed = {"startPeriod", "endPeriod", "lastNObservations"}
        extra = set(params or {}) - allowed
        if extra:
            raise ValueError(f"unsupported SDMX-CSV parameters: {sorted(extra)}")
        template, fixed = _CSV_ENDPOINTS[self.source]
        url = template.format(flow=flow, key=key)
        query = {**fixed, **{k: str(v) for k, v in sorted(dict(params or {}).items())}}
        return url, query

    def parse_csv(self, raw):
        """SDMX-CSV 1.0 rows into one series per dimension combination (standard library only).

        Columns between ``DATAFLOW`` (and Eurostat's ``LAST UPDATE``) and
        ``TIME_PERIOD`` are dimensions; columns after ``OBS_VALUE`` are
        observation attributes. A missing value (empty or ``:``) stays ``None``
        with its text and attributes kept. A repeated period, a missing column,
        a row of another shape or an over-budget response is refused rather than
        read partially.
        """
        content = raw.content.encode() if isinstance(raw.content, str) else raw.content
        if len(content) > self.max_bytes:
            raise IntegrationError("response_limit", "SDMX response exceeds budget")
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise IntegrationError("invalid_csv", "SDMX-CSV is not UTF-8") from exc
        reader = csv.reader(io.StringIO(text))
        header = next(reader, None)
        if not header or header[0].strip().upper() != "DATAFLOW":
            raise IntegrationError("invalid_csv", "SDMX-CSV starts with a DATAFLOW column")
        columns = [c.strip() for c in header]
        upper = [c.upper() for c in columns]
        for required in ("TIME_PERIOD", "OBS_VALUE"):
            if required not in upper:
                raise IntegrationError("invalid_csv", f"SDMX-CSV lacks the {required} column")
        if len(set(upper)) != len(upper):
            raise IntegrationError("invalid_csv", "SDMX-CSV repeats a column")
        time_at, value_at = upper.index("TIME_PERIOD"), upper.index("OBS_VALUE")
        start = 2 if len(upper) > 1 and upper[1] == "LAST UPDATE" else 1
        if not start < time_at < value_at:
            raise IntegrationError("invalid_csv", "SDMX-CSV column order is DATAFLOW, dimensions, TIME_PERIOD, OBS_VALUE")
        dimension_columns = list(range(start, time_at))
        attribute_columns = list(range(value_at + 1, len(columns)))
        groups = {}
        flows, updates = set(), set()
        count = 0
        for line, row in enumerate(reader, start=2):
            if not row or not any(cell.strip() for cell in row):
                continue
            if len(row) != len(columns):
                raise IntegrationError("invalid_csv", f"SDMX-CSV row {line} has {len(row)} cells, not {len(columns)}")
            count += 1
            if count > self.max_observations:
                raise IntegrationError("observation_limit", "No truncated SDMX series published")
            flows.add(row[0].strip())
            if start == 2 and row[1].strip():
                updates.add(row[1].strip())
            dimensions = {columns[i]: row[i].strip() for i in dimension_columns}
            period = row[time_at].strip()
            if not period:
                raise IntegrationError("missing_period", f"SDMX-CSV row {line} lacks a time period")
            value_text = row[value_at].strip()
            attributes = {columns[i]: row[i].strip() for i in attribute_columns if row[i].strip()}
            entry = groups.setdefault(digest(dimensions), {
                "dimensions": dimensions, "observations": [], "attributes": {}, "raw_values": {}, "lines": {}})
            if period in entry["raw_values"]:
                raise IntegrationError("duplicate_period", "Repeated period within SDMX series")
            value = None
            if value_text not in _CSV_MISSING:
                try:
                    value = float(value_text)
                except ValueError as exc:
                    raise IntegrationError("invalid_value", f"SDMX-CSV row {line} has a non-numeric value") from exc
                if not math.isfinite(value):
                    value = None
            entry["observations"].append(Observation(period, value))
            entry["raw_values"][period] = value_text
            entry["attributes"][period] = attributes
            entry["lines"][period] = line
        if len(flows) > 1:
            raise IntegrationError("invalid_csv", "SDMX-CSV response mixes dataflows")
        if len(updates) > 1:
            # One dataset has one update stamp; several would make the vintage ambiguous.
            raise IntegrationError("invalid_csv", "SDMX-CSV response states more than one LAST UPDATE")
        last_update = next(iter(updates), None)
        raw_sha256 = hashlib.sha256(content).hexdigest()
        records = []
        for key, entry in sorted(groups.items()):
            dimensions = entry["dimensions"]
            normalized = {k.upper(): v for k, v in dimensions.items()}
            frequency = _FREQUENCIES.get(normalized.get("FREQ"), "irregular")
            records.append(SeriesRecord(
                series_id=self.provider + ":" + raw.ref.locator + ":" + key[:24],
                provider=self.provider,
                title=raw.ref.title or raw.ref.locator,
                frequency=frequency,
                as_of=raw.fetched_at,
                observations=sorted(entry["observations"], key=lambda o: o.period),
                unit=normalized.get("UNIT"),
                geography=normalized.get("GEO") or normalized.get("REF_AREA"),
                source_url=raw.source_url,
                metadata={
                    "format": "SDMX-CSV",
                    "dataflow": next(iter(flows), None),
                    "dataflow_id": raw.ref.metadata.get("flow") or raw.ref.locator.split("/", 1)[0],
                    "dimensions": dimensions,
                    "original_values": entry["raw_values"],
                    "observation_attributes": entry["attributes"],
                    "row_lines": entry["lines"],
                    "provider_last_update": last_update,
                    "provider_last_update_at": last_update_iso(last_update),
                    "provider_last_update_ms": _time_millis(last_update_iso(last_update)),
                    "vintage_basis": "provider_last_update" if last_update_iso(last_update)
                    else "retrieval_time_current_response",
                    "vintage_semantics": "the provider's LAST UPDATE stamp when stated; otherwise retrieval time, "
                                         "and no historical provider vintage is inferred",
                    "acquired_at_ms": raw.fetched_at,
                    "raw_sha256": raw_sha256,
                },
            ))
        return records

    def _fetch_request(self, ref, request, accept):
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        parts = urlsplit(request.url)
        # The SDK defines provider URLs; user input never supplies a host.
        if parts.scheme != "https" or parts.hostname != _PROVIDER_HOSTS[self.source]:
            raise IntegrationError(
                "provider_endpoint_changed",
                "Review the SDMX provider endpoint before use",
            )
        base = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        response = (self.transport or HTTPSPageAdapter._request)(
            url=base,
            params=dict(parse_qsl(parts.query)),
            headers={
                "Accept": accept,
                "User-Agent": "Noesis/1.0 (+https://github.com/Ikey168/Noesis)",
            },
            timeout=20,
            max_bytes=self.max_bytes,
        )
        if int(response.get("status", 200)) != 200:
            status = int(response.get("status", 200))
            raise IntegrationError(
                "source_http_" + str(status),
                f"SDMX {self.source} returned HTTP {status}; check key, frequency and period bounds",
            )
        content = response["content"]
        content = content.encode() if isinstance(content, str) else content
        if len(content) > self.max_bytes:
            raise IntegrationError("response_limit", "SDMX response exceeds budget")
        return RawSeries(
            ref, content, content_type="application/xml", source_url=request.url
        )

    @staticmethod
    def _structure_projection(structure, dimensions):
        if structure is None:
            return {"status": "not_requested"}
        projections = []
        for definition in structure["structures"].values():
            labels = {}
            for dimension, code in dimensions.items():
                codelist_id = definition["dimensions"].get(dimension)
                codelist = structure["codelists"].get(codelist_id, {})
                labels[dimension] = {
                    "code": code,
                    "codelist_id": codelist_id,
                    "codelist_version": codelist.get("version"),
                    "labels": codelist.get("codes", {}).get(code, {}),
                }
            projections.append(
                {
                    "id": definition["id"],
                    "version": definition["version"],
                    "dimensions": labels,
                }
            )
        return {
            "source_url": structure["source_url"],
            "raw_sha256": structure["raw_sha256"],
            "definitions": projections,
            "status": "mapped" if projections else "no_data_structure",
        }

    def parse(self, raw, *, structure=None):
        import sdmx

        content = raw.content.encode() if isinstance(raw.content, str) else raw.content
        if len(content) > self.max_bytes:
            raise IntegrationError("response_limit", "SDMX response exceeds budget")
        # Reject external/internal entity declarations before the SDK XML reader.
        if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
            raise IntegrationError("invalid_xml", "XML entities are forbidden")
        message = sdmx.read_sdmx(io.BytesIO(content))
        if structure is not None and any(
            dataset.structured_by.id not in structure["structures"]
            for dataset in message.data
        ):
            raise IntegrationError(
                "structure_mismatch",
                "SDMX data structure does not match the supplied definitions",
            )
        groups = {}
        count = 0
        for dataset in message.data:
            for observation in dataset.obs:
                count += 1
                if count > self.max_observations:
                    raise IntegrationError(
                        "observation_limit", "No truncated SDMX series published"
                    )
                dimensions = {
                    k: str(v.value) for k, v in observation.key.values.items()
                }
                period = dimensions.pop("TIME_PERIOD", dimensions.pop("TIME", None))
                if not period:
                    raise IntegrationError(
                        "missing_period", "Observation lacks a time dimension"
                    )
                attributes = {k: str(v.value) for k, v in observation.attrib.items()}
                key = digest(dimensions)
                entry = groups.setdefault(
                    key,
                    {
                        "dimensions": dimensions,
                        "observations": [],
                        "attributes": {},
                        "raw_values": {},
                        "dataset_metadata": [],
                    },
                )
                dataset_metadata = {
                    "action": str(dataset.action.value) if dataset.action else None,
                    "valid_from": str(dataset.valid_from)
                    if dataset.valid_from
                    else None,
                    "structure_id": dataset.structured_by.id
                    if dataset.structured_by
                    else None,
                }
                if dataset_metadata not in entry["dataset_metadata"]:
                    entry["dataset_metadata"].append(dataset_metadata)
                value = (
                    float(observation.value) if observation.value is not None else None
                )
                if value is not None and not math.isfinite(value):
                    value = None
                if period in entry["attributes"]:
                    raise IntegrationError(
                        "duplicate_period", "Repeated period within SDMX series"
                    )
                entry["observations"].append(Observation(period, value))
                entry["attributes"][period] = attributes
                entry["raw_values"][period] = (
                    str(observation.value) if observation.value is not None else None
                )
        records = []
        for key, entry in sorted(groups.items()):
            dimensions = entry["dimensions"]
            normalized_dimensions = {k.upper(): v for k, v in dimensions.items()}
            common_attributes = {}
            if entry["attributes"]:
                first = next(iter(entry["attributes"].values()))
                common_attributes = {
                    k: v
                    for k, v in first.items()
                    if all(attrs.get(k) == v for attrs in entry["attributes"].values())
                }
            prepared_at = str(message.header.prepared) if message.header.prepared else None
            extracted_at = str(message.header.extracted) if message.header.extracted else None
            frequency_value = (
                normalized_dimensions.get("FREQ")
                or normalized_dimensions.get("BBK_STD_FREQ")
                or common_attributes.get("FREQ")
            )
            frequency = {
                "A": "annual",
                "Q": "quarterly",
                "M": "monthly",
                "W": "weekly",
                "D": "daily",
            }.get(
                frequency_value,
                "irregular",
            )
            release_clock_status = (
                "SDMX header timestamps preserved; they are not asserted as release time"
            )
            vintage_id = f"{raw.ref.locator}@{raw.fetched_at}"
            records.append(
                SeriesRecord(
                    series_id=self.provider + ":" + raw.ref.locator + ":" + key[:24],
                    provider=self.provider,
                    title=raw.ref.title or raw.ref.locator,
                    frequency=frequency,
                    as_of=raw.fetched_at,
                    observations=sorted(entry["observations"], key=lambda o: o.period),
                    unit=normalized_dimensions.get("UNIT")
                    or common_attributes.get("UNIT")
                    or common_attributes.get("BBK_UNIT"),
                    geography=normalized_dimensions.get("REF_AREA")
                    or normalized_dimensions.get("GEO"),
                    source_url=raw.source_url,
                    metadata={
                        "dimensions": dimensions,
                        "common_attributes": common_attributes,
                        "original_values": entry["raw_values"],
                        "provider_prepared_at": str(message.header.prepared)
                        if message.header.prepared
                        else None,
                        "provider_prepared_at_ms": _time_millis(prepared_at),
                        "provider_release_at": None,
                        "provider_release_at_ms": None,
                        "provider_release_time_status": release_clock_status,
                        "dataset_metadata": entry["dataset_metadata"],
                        "provider_extracted_at": str(message.header.extracted)
                        if message.header.extracted
                        else None,
                        "provider_extracted_at_ms": _time_millis(extracted_at),
                        "provider_vintage_ms": raw.fetched_at,
                        "vintage_id": vintage_id,
                        "vintage_basis": "retrieval_time_current_response",
                        "acquired_at_ms": raw.fetched_at,
                        "seasonal_adjustment": _seasonal_adjustment(
                            normalized_dimensions, common_attributes
                        ),
                        "dataflow_id": raw.ref.metadata.get("flow")
                        or raw.ref.locator.split("/", 1)[0],
                        "structure": self._structure_projection(structure, dimensions),
                        "observation_attributes": entry["attributes"],
                        "raw_sha256": __import__("hashlib").sha256(content).hexdigest(),
                        "sdmx_version": version("sdmx1"),
                        "vintage_semantics": "retrieval time; historical provider vintage not inferred",
                    },
                )
            )
        return records
